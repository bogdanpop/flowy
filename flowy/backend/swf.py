from __future__ import print_function

import itertools
import json
import logging
import os
import socket
import sys
import uuid

from boto.exception import SWFResponseError
from boto.swf.exceptions import SWFTypeAlreadyExistsError
from boto.swf.layer1 import Layer1
from boto.swf.layer1_decisions import Layer1Decisions

from flowy.base import Activity
from flowy.base import BoundProxy
from flowy.base import setup_default_logger
from flowy.base import Worker
from flowy.base import Workflow


__all__ = 'SWFWorkflow SWFWorkflowWorker SWFActivity SWFActivityWorker'.split()


logger = logging.getLogger(__name__)


_CHILD_POLICY = ['TERMINATE', 'REQUEST_CANCEL', 'ABANDON', None]
_INPUT_SIZE = _RESULT_SIZE = 32768
_IDENTITY_SIZE = _REASON_SIZE = 256


_serialize_input = lambda *args, **kwargs: json.dumps((args, kwargs))
_serialize_result = json.dumps
_deserialize_input = _deserialize_result = json.loads


class SWFConfigMixin(object):
    def register_remote(self, swf_layer1, domain):
        """Register the workflow config in Amazon SWF if it's missing.

        If the workflow registration fails because there is already another
        workflow with this name and version registered, check if all the
        defaults have the same values.

        A name should be set before calling this method or RuntimeError is
        raised.

        If the registration is unsuccessful, the registered version is
        incompatible with this one or in case of SWF communication errors raise
        RegistrationError. ValueError is raised if any configuration values
        can't be converted to the required types.
        """
        registered_as_new = self.try_register_remote(swf_layer1, domain)
        if not registered_as_new:
            self.check_compatible(swf_layer1, domain)  # raises if incompatible

    def set_alternate_name(self, name):
        """Set the name of this workflow if one is not already set.

        Returns a configuration instance with the new name or the existing
        instance if the name was not changed.

        It's useful to return a new instance because if this config lacks a
        name it can be used to register multiple factories and fallback to each
        factory __name__ value.
        """
        if self.name is not None:
            return self
        klass = self.__class__
        # Make a clone since this config can be used as a decorator on multiple
        # workflow factories and each has a different name.
        clone_args = {'name': name}
        for prop, val in self.__dict__.items():
            if (prop == 'version'
                    or prop.startswith('default')
                    or prop.startswith('serialize')
                    or prop.startswith('deserialize')):
                clone_args[prop] = val
        return klass(**clone_args)

    @property
    def key(self):
        """Use the name and the version to identify this config."""
        if self.name is None:
            raise RuntimeError('Name is not set.')
        return str(self.name), str(self.version)



class SWFWorkflow(SWFConfigMixin, Workflow):
    """A configuration object suited for Amazon SWF Workflows.

    Use conf_activity and conf_workflow to configure workflow implementation
    dependencies.
    """

    category = 'swf_workflow'  # venusian category used for this type of confs

    def __init__(self, version, name=None, default_task_list=None,
                 default_workflow_duration=None,
                 default_decision_duration=None,
                 default_child_policy=None, rate_limit=64,
                 deserialize_input=_deserialize_input,
                 serialize_result=_serialize_result,
                 serialize_restart_input=_serialize_input):
        """Initialize the config object.

        The timer values are in seconds, and the child policy should be either
        TERMINATE, REQUEST_CANCEL, ABANDON or None.

        For the default configs, a value of None means that the config is unset
        and must be set explicitly in proxies pointing to this workflow.

        The rate_limit is used to limit the number of concurrent tasks. A value
        of None means no rate limit.

        The name is not required at this point but should be set before trying
        to register this config remotely and can be set later with
        set_alternate_name.
        """
        self.name = name
        self.version = version
        self.default_task_list = default_task_list
        self.default_workflow_duration = default_workflow_duration
        self.default_decision_duration = default_decision_duration
        self.default_child_policy = default_child_policy
        self.rate_limit = rate_limit
        self.proxy_factory_registry = {}
        super(SWFWorkflow, self).__init__(deserialize_input, serialize_result,
                                          serialize_restart_input)

    def init(self, workflow_factory, execution_history, decision):
        rate_limit = DescCounter(int(self.rate_limit))
        return super(SWFWorkflow, self).init(workflow_factory,
                                             execution_history,
                                             decision, rate_limit)

    def set_alternate_name(self, name):
        new_config = super(SWFWorkflow, self).set_alternate_name(name)
        if new_config is not self:
            for dep_name, proxy in self.proxy_factory_registry.iteritems():
                new_config.conf_proxy(dep_name, proxy)
        return new_config

    def _cvt_values(self):
        """Convert values to their expected types or bailout."""
        if self.name is None:
            raise RuntimeError('Name is not set.')
        d_t_l = _str_or_none(self.default_task_list)
        d_w_d = _timer_encode(self.default_workflow_duration, 'default_workflow_duration')
        d_d_d = _timer_encode(self.default_decision_duration, 'default_decision_duration')
        d_c_p = _cp_encode(self.default_child_policy)
        return str(self.name), str(self.version), d_t_l, d_w_d, d_d_d, d_c_p

    def try_register_remote(self, swf_layer1, domain):
        """Register the workflow remotely.

        Returns True if registration is successful and False if another
        workflow with the same name is already registered.

        A name should be set before calling this method or RuntimeError is
        raised.

        Raise _RegistrationError in case of SWF communication errors and
        ValueError if any configuration values can't be converted to the
        required types.
        """
        n, version, d_t_l, d_w_d, d_d_d, d_c_p = self._cvt_values()
        try:
            swf_layer1.register_workflow_type(
                str(domain), name=n, version=version, task_list=d_t_l,
                default_execution_start_to_close_timeout=d_w_d,
                default_task_start_to_close_timeout=d_d_d,
                default_child_policy=d_c_p)
        except SWFTypeAlreadyExistsError:
            return False
        except SWFResponseError as e:
            logger.exception('Error while registering the workflow:')
            raise _RegistrationError(e)
        return True

    def check_compatible(self, swf_layer1, domain):
        """Check if the remote config has the same defaults as this one.

        A name should be set before calling this method or RuntimeError is
        raised.

        Raise _RegistrationError in case of SWF communication errors or
        incompatibility and ValueError if any configuration values can't be
        converted to the required types.
        """
        n, v, d_t_l, d_w_d, d_d_d, d_c_p = self._cvt_values()
        try:
            w_descr = swf_layer1.describe_workflow_type(str(domain), n, v)
            w_descr = w_descr['configuration']
        except SWFResponseError as e:
            logger.exception('Error while checking workflow compatibility:')
            raise _RegistrationError(e)
        r_d_t_l = w_descr.get('defaultTaskList', {}).get('name')
        if r_d_t_l != d_t_l:
            raise _RegistrationError('Default task list for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_t_l, d_t_l))
        r_d_d_d = w_descr.get('defaultTaskStartToCloseTimeout')
        if r_d_d_d != d_d_d:
            raise _RegistrationError('Default decision duration for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_d_d, d_d_d))
        r_d_w_d = w_descr.get('defaultExecutionStartToCloseTimeout')
        if r_d_w_d != d_w_d:
            raise _RegistrationError('Default workflow duration for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_w_d, d_w_d))
        r_d_c_p = w_descr.get('defaultChildPolicy')
        if r_d_c_p != d_c_p:
            raise _RegistrationError('Default child policy for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_c_p, d_c_p))

    def conf_activity(self, dep_name, version, name=None, task_list=None,
                      heartbeat=None, schedule_to_close=None,
                      schedule_to_start=None, start_to_close=None,
                      serialize_input=_serialize_input,
                      deserialize_result=_deserialize_input,
                      retry=(0, 0, 0)):
        """Configure an activity dependency for a workflow implementation.

        dep_name is the name of one of the workflow factory arguments
        (dependency). For example:

            class MyWorkflow:
                def __init__(self, a, b):  # Two dependencies: a and b
                    self.a = a
                    self.b = b
                def run(self, n):
                    pass

            cfg = SWFWorkflowConfig(version=1)
            cfg.conf_activity('a', name='MyActivity', version=1)
            cfg.conf_activity('b', version=2, task_list='my_tl')

        For convenience, if the activity name is missing, it will be the same
        as the dependency name.
        """
        if name is None:
            name = dep_name
        proxy = SWFActivityProxy(identity=dep_name, name=name, version=version,
                                 task_list=task_list, heartbeat=heartbeat,
                                 schedule_to_close=schedule_to_close,
                                 schedule_to_start=schedule_to_start,
                                 start_to_close=start_to_close,
                                 serialize_input=serialize_input,
                                 deserialize_result=deserialize_result,
                                 retry=retry)
        self.conf_proxy(dep_name, proxy)

    def conf_workflow(self, dep_name, version, name=None, task_list=None,
                      workflow_duration=None, decision_duration=None,
                      serialize_input=_serialize_input,
                      deserialize_result=_deserialize_input,
                      retry=(0, 0, 0)):
        """Same as conf_activity but for sub-workflows."""
        if name is None:
            name = dep_name
        proxy = SWFWorkflowProxy(identity=dep_name, name=name, version=version,
                                 task_list=task_list,
                                 workflow_duration=workflow_duration,
                                 decision_duration=decision_duration,
                                 serialize_input=serialize_input,
                                 deserialize_result=deserialize_result,
                                 retry=retry)
        self.conf_proxy(dep_name, proxy)


class SWFActivity(SWFConfigMixin, Activity):

    category = 'swf_activity'  # venusian category used for this type of confs

    def __init__(self, version, name=None, default_task_list=None,
                 default_heartbeat=None, default_schedule_to_close=None,
                 default_schedule_to_start=None, default_start_to_close=None,
                 deserialize_input=_deserialize_input,
                 serialize_result=_serialize_result):
        """Initialize the config object.

        The timer values are in seconds.

        For the default configs, a value of None means that the config is unset
        and must be set explicitly in proxies pointing to this activity.

        The name is not required at this point but should be set before trying
        to register this config remotely and can be set later with
        set_alternate_name.
        """
        self.name = name
        self.version = version
        self.default_task_list = default_task_list
        self.default_heartbeat = default_heartbeat
        self.default_schedule_to_close = default_schedule_to_close
        self.default_schedule_to_start = default_schedule_to_start
        self.default_start_to_close = default_start_to_close
        super(SWFActivity, self).__init__(deserialize_input, serialize_result)

    def _cvt_values(self):
        """Convert values to their expected types or bailout."""
        n = self.name
        if n is None:
            raise RuntimeError('Name is not set.')
        d_t_l = _str_or_none(self.default_task_list)
        d_h = _str_or_none(self.default_heartbeat)
        d_sch_c = _str_or_none(self.default_schedule_to_close)
        d_sch_s = _str_or_none(self.default_schedule_to_start)
        d_s_c = _str_or_none(self.default_start_to_close)
        return str(n), str(self.version), d_t_l, d_h, d_sch_c, d_sch_s, d_s_c

    def try_register_remote(self, swf_layer1, domain):
        """Same as SWFWorkflowConfig.try_register_remote."""
        n, version, d_t_l, d_h, d_sch_c, d_sch_s, d_s_c = self._cvt_values()
        try:
            swf_layer1.register_activity_type(
                str(domain), name=n, version=version, task_list=d_t_l,
                default_task_heartbeat_timeout=d_h,
                default_task_schedule_to_close_timeout=d_sch_c,
                default_task_schedule_to_start_timeout=d_sch_s,
                default_task_start_to_close_timeout=d_s_c)
        except SWFTypeAlreadyExistsError:
            return False
        except SWFResponseError as e:
            logger.exception('Error while registering the activity:')
            raise _RegistrationError(e)
        return True

    def check_compatible(self, swf_layer1, domain):
        """Same as SWFWorkflowConfig.check_compatible."""
        n, v, d_t_l, d_h, d_sch_c, d_sch_s, d_s_c = self._cvt_values()
        try:
            a_descr = swf_layer1.describe_activity_type(str(domain), n, v)
            a_descr = a_descr['configuration']
        except SWFResponseError as e:
            logger.exception('Error while checking activity compatibility:')
            raise _RegistrationError(e)
        r_d_t_l = a_descr.get('defaultTaskList', {}).get('name')
        if r_d_t_l != d_t_l:
            raise _RegistrationError('Default task list for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_t_l, d_t_l))
        r_d_h = a_descr.get('defaultTaskHeartbeatTimeout')
        if r_d_h != d_h:
            raise _RegistrationError('Default heartbeat for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_h, d_h))
        r_d_sch_c = a_descr.get('defaultTaskScheduleToCloseTimeout')
        if r_d_sch_c != d_sch_c:
            raise _RegistrationError('Default schedule to close for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_sch_c, d_sch_c))
        r_d_sch_s = a_descr.get('defaultTaskScheduleToStartTimeout')
        if r_d_sch_s != d_sch_s:
            raise _RegistrationError('Default schedule to start for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_sch_s, d_sch_s))
        r_d_s_c = a_descr.get('defaultTaskStartToCloseTimeout')
        if r_d_s_c != d_s_c:
            raise _RegistrationError('Default start to close for %r version %r does not match: %r != %r' %
                                     (n, v, r_d_s_c, d_s_c))


class SWFWorker(Worker):

    def register_remote(self, layer1, domain):
        """Register or check compatibility of all configs in Amazon SWF."""
        for workflow in self.registry.values():
            workflow.register_remote(layer1, domain)

    def register(self, config, impl):
        config = config.set_alternate_name(impl)
        super(SWFWorker, self).register(config, impl)


class SWFWorkflowWorker(SWFWorker):

    categories = ['swf_workflow']

    def run_forever(self):
        """Run the worker forever."""


class SWFActivityWorker(SWFWorker):

    categories = ['swf_activity']

    def run_forever(self):
        """Run the worker forever."""


class SWFActivityProxy(object):
    """An unbounded Amazon SWF activity proxy.

    This class is used by SWFWorkflowConfig for each activity configured.
    It must be bound to an execution context before it can be useful and uses
    double-dispatch trough ContextBoundProxy which has most of scheduling
    logic.
    """

    def __init__(self, identity, name, version, task_list=None, heartbeat=None,
                 schedule_to_close=None, schedule_to_start=None,
                 start_to_close=None, retry=(0, 0, 0),
                 serialize_input=_serialize_input,
                 deserialize_result=_deserialize_result):
        # This is a unique name used to generate unique identifiers
        self.identity = identity
        self.name = name
        self.version = version
        self.task_list = task_list
        self.heartbeat = heartbeat
        self.schedule_to_close = schedule_to_close
        self.schedule_to_start = schedule_to_start
        self.start_to_close = start_to_close
        self.retry = retry
        self.serialize_input = serialize_input
        self.deserialize_result = deserialize_result

    def __call__(self, context, rate_limit):
        """Return a ContextBoundProxy instance that calls back schedule."""
        return ContextBoundProxy(self, context)

    def schedule(self, context, call_key, delay, *args, **kwargs):
        """Schedule the activity in the execution context.

        If any delay is set use SWF timers before really scheduling anything.
        """
        if int(delay) > 0 and not context.timer_ready(call_key):
            context.schedule_timer(call_key, delay)
            return
        try:
            # Serialization errors are also handled outside but the logging
            # messages are more specific here
            input_data = self.serialize_input(*args, **kwargs)
        except Exception as e:
            logger.exception('Error while serializing activity input:')
            context.fail(e)
        else:
            context.schedule_activity(
                call_key, self.name, self.version, input_data, self.task_list,
                self.heartbeat, self.schedule_to_close, self.schedule_to_start,
                self.start_to_close)


class SWFWorkflowProxy(object):
    """Same as SWFActivityProxy but for sub-workflows."""
    def __init__(self, identity, name, version, task_list=None,
                 workflow_duration=None, decision_duration=None,
                 retry=(0, 0, 0), serialize_input=_serialize_input,
                 deserialize_result=_deserialize_result):
        self.identity = identity
        self.name = name
        self.version = version
        self.task_list = task_list
        self.workflow_duration = workflow_duration
        self.decision_duration = decision_duration
        self.retry = retry
        self.serialize_input = serialize_input
        self.deserialize_result = deserialize_result

    def bind(self, context, rate_limit=DescCounter()):
        return ContextBoundProxy(self, context, rate_limit)

    def schedule(self, context, call_key, delay, *args, **kwargs):
        if int(delay) > 0 and not context.timer_ready(call_key):
            return context.schedule_timer(call_key, delay)
        try:
            input_data = self.serialize_input(*args, **kwargs)
        except Exception as e:
            logger.exception('Error while serializing sub-workflow input:')
            context.fail(e)
        else:
            context.schedule_workflow(call_key, self.name, self.version,
                                      input_data, self.task_list,
                                      self.workflow_duration,
                                      self.decision_duration)


def poll_next_decision(layer1, domain, task_list, identity=None):
    """Poll a decision and create a SWFWorkflowContext instance."""
    first_page = poll_first_page(layer1, domain, task_list, identity)
    token = first_page['taskToken']
    all_events = events(layer1, domain, task_list, first_page, identity)
    # Sometimes the first event in on the second page,
    # and the first page is empty
    first_event = next(all_events)
    assert first_event['eventType'] == 'WorkflowExecutionStarted'
    wesea = 'workflowExecutionStartedEventAttributes'
    assert first_event[wesea]['taskList']['name'] == task_list
    decision_duration = first_event[wesea]['taskStartToCloseTimeout']
    workflow_duration = first_event[wesea]['executionStartToCloseTimeout']
    tags = first_event[wesea].get('tagList', None)
    child_policy = first_event[wesea]['childPolicy']
    name = first_event[wesea]['workflowType']['name']
    version = first_event[wesea]['workflowType']['version']
    input_data = first_event[wesea]['input']
    try:
        running, timedout, results, errors, order = load_events(all_events)
    except _PaginationError:
        # There's nothing better to do than to retry
        return poll_next_decision(layer1, task_list, domain, identity)
    return SWFWorkflowContext(layer1, token, name, version, input_data,
                      task_list, decision_duration, workflow_duration, tags,
                      child_policy, running, timedout, results, errors, order)

def poll_first_page(layer1, domain, task_list, identity=None):
    """Return the response from loading the first page.

    In case of errors, empty responses or whatnot retry until a valid response.
    """
    swf_response = {}
    while 'taskToken' not in swf_response or not swf_response['taskToken']:
        try:
            swf_response = layer1.poll_for_decision_task(
                str(domain), str(task_list), _str_or_none(identity))
        except SWFResponseError:
            logger.exception('Error while polling for decisions:')
    return swf_response

def poll_response_page(layer1, domain, task_list, token, identity=None):
    """Return a specific page. In case of errors retry a number of times."""
    swf_response = None
    for _ in range(7):  # give up after a limited number of retries
        try:
            swf_response = layer1.poll_for_decision_task(
                str(domain), str(task_list), _str_or_none(identity),
                next_page_token=str(token))
            break
        except SWFResponseError:
            logger.exception('Error while polling for decision page:')
    else:
        raise _PaginationError()
    return swf_response

def events(layer1, domain, task_list, first_page, identity=None):
    """Load pages one by one and generate all events found."""
    page = first_page
    while 1:
        for event in page['events']:
            yield event
        if not page.get('nextPageToken'):
            break
        page = poll_response_page(layer1, domain, task_list,
                                  page['nextPageToken'], identity)

def load_events(event_iter):
    """Combine all events in their order.

    This returns a tuple of the following things:
        running  - a set of the ids of running tasks
        timedout - a set of the ids of tasks that have timedout
        results  - a dictionary of id -> result for each finished task
        errors   - a dictionary of id -> error message for each failed task
        order    - an list of task ids in the order they finished
    """
    running, timedout = set(), set()
    results, errors = {}, {}
    order = []
    event2call = {}
    for event in event_iter:
        e_type = event.get('eventType')
        if e_type == 'ActivityTaskScheduled':
            eid = event['activityTaskScheduledEventAttributes']['activityId']
            event2call[event['eventId']] = eid
            running.add(eid)
        elif e_type == 'ActivityTaskCompleted':
            atcea = 'activityTaskCompletedEventAttributes'
            eid = event2call[event[atcea]['scheduledEventId']]
            result = event[atcea]['result']
            running.remove(eid)
            results[eid] = result
            order.append(eid)
        elif e_type == 'ActivityTaskFailed':
            atfea = 'activityTaskFailedEventAttributes'
            eid = event2call[event[atfea]['scheduledEventId']]
            reason = event[atfea]['reason']
            running.remove(eid)
            errors[eid] = reason
            order.append(eid)
        elif e_type == 'ActivityTaskTimedOut':
            attoea = 'activityTaskTimedOutEventAttributes'
            eid = event2call[event[attoea]['scheduledEventId']]
            running.remove(eid)
            timedout.add(eid)
            order.append(eid)
        elif e_type == 'ScheduleActivityTaskFailed':
            satfea = 'scheduleActivityTaskFailedEventAttributes'
            eid = event[satfea]['activityId']
            reason = event[satfea]['cause']
            # when a job is not found it's not even started
            errors[eid] = reason
            order.append(eid)
        elif e_type == 'StartChildWorkflowExecutionInitiated':
            scweiea = 'startChildWorkflowExecutionInitiatedEventAttributes'
            eid = _subworkflow_call_key(event[scweiea]['workflowId'])
            running.add(eid)
        elif e_type == 'ChildWorkflowExecutionCompleted':
            cwecea = 'childWorkflowExecutionCompletedEventAttributes'
            eid = _subworkflow_call_key(
                event[cwecea]['workflowExecution']['workflowId'])
            result = event[cwecea]['result']
            running.remove(eid)
            results[eid] = result
            order.append(eid)
        elif e_type == 'ChildWorkflowExecutionFailed':
            cwefea = 'childWorkflowExecutionFailedEventAttributes'
            eid = _subworkflow_call_key(
                event[cwefea]['workflowExecution']['workflowId'])
            reason = event[cwefea]['reason']
            running.remove(eid)
            errors[eid] = reason
            order.append(eid)
        elif e_type == 'ChildWorkflowExecutionTimedOut':
            cwetoea = 'childWorkflowExecutionTimedOutEventAttributes'
            eid = _subworkflow_call_key(
                event[cwetoea]['workflowExecution']['workflowId'])
            running.remove(eid)
            timedout.add(eid)
            order.append(eid)
        elif e_type == 'StartChildWorkflowExecutionFailed':
            scwefea = 'startChildWorkflowExecutionFailedEventAttributes'
            eid = _subworkflow_call_key(event[scwefea]['workflowId'])
            reason = event[scwefea]['cause']
            errors[eid] = reason
            order.append(eid)
        elif e_type == 'TimerStarted':
            eid = event['timerStartedEventAttributes']['timerId']
            # while the timer is running, act as if the task itself is running
            # to prevent it from being scheduled again
            running.add(_timer_call_key(eid))
        elif e_type == 'TimerFired':
            eid = event['timerFiredEventAttributes']['timerId']
            running.remove(_timer_call_key(eid))
            results[eid] = None
    return running, timedout, results, errors, order


class SWFWorkflowContext(object):
    def __init__(self, layer1, token, name, version, input_data,
                 task_list, decision_duration, workflow_duration, tags,
                 child_policy, running, timedout, results, errors, order):
        self.layer1 = layer1
        self.token = token
        self.name = name
        self.version = version
        self.input = input_data
        self.task_list = task_list
        self.decision_duration = decision_duration
        self.workflow_duration = workflow_duration
        self.tags = tags
        self.child_policy = child_policy
        self.running = running
        self.timedout = timedout
        self.results = results
        self.errors = errors
        self.order = order
        self.decisions = Layer1Decisions()
        self.closed = False

    def is_running(self, call_key):
        return str(call_key) in self.running

    def is_result(self, call_key):
        return str(call_key) in self.results

    def result(self, call_key):
        return self.results[str(call_key)], self.order.index(str(call_key))

    def is_error(self, call_key):
        return str(call_key) in self.errors

    def error(self, call_key):
        return self.errors[str(call_key)], self.order.index(str(call_key))

    def is_timeout(self, call_key):
        return str(call_key) in self.timedout

    def timeout(self, call_key):
        return self.order.index(str(call_key))

    def timer_ready(self, call_key):
        return _timer_key(call_key) in self.results

    def fail(self, reason):
        decisions = self.decisions = Layer1Decisions()
        decisions.fail_workflow_execution(reason=str(reason)[:_REASON_SIZE])
        self.flush()

    def flush(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.layer1.respond_decision_task_completed(
                task_token=str(self.token), decisions=self.decisions._data)
        except SWFResponseError:
            logger.exception('Error while sending the decisions:')
            # ignore the error and let the decision timeout and retry

    def restart(self, input_data):
        decisions = self.decisions = Layer1Decisions()
        decisions.continue_as_new_workflow_execution(
            start_to_close_timeout=_str_or_none(self.decision_duration),
            execution_start_to_close_timeout=_str_or_none(self.workflow_duration),
            task_list=_str_or_none(self.task_list),
            input=str(input_data)[:_INPUT_SIZE],
            tag_list=_tags(self.tags),
            child_policy=_cp_encode(self.child_policy))
        self.flush()

    def finish(self, result):
        decisions = self.decisions = Layer1Decisions()
        decisions.complete_workflow_execution(str(result)[:_RESULT_SIZE])
        self.flush()

    # Used by SWFProxy instances

    def schedule_timer(self, call_key, delay):
        call_key = _timer_key(call_key)
        self.decisions.start_timer(timer_id=str(call_key),
                                   start_to_fire_timeout=str(delay))

    def schedule_activity(self, call_key, name, version, input_data, task_list,
                          heartbeat, schedule_to_close, schedule_to_start,
                          start_to_close):
        self.decisions.schedule_activity_task(
            str(call_key), str(name), str(version),
            heartbeat_timeout=_str_or_none(heartbeat),
            schedule_to_close_timeout=_str_or_none(schedule_to_close),
            schedule_to_start_timeout=_str_or_none(schedule_to_start),
            start_to_close_timeout=_str_or_none(start_to_close),
            task_list=_str_or_none(task_list),
            input=str(input_data))

    def schedule_workflow(self, call_key, name, version, input_data, task_list,
                          workflow_duration, decision_duration):
        call_key = _subworkflow_key(call_key)
        self.decisions.start_child_workflow_execution(
            str(name), str(version), str(call_key),
            task_start_to_close_timeout=_str_or_none(decision_duration),
            execution_start_to_close_timeout=_str_or_none(workflow_duration),
            task_list=_str_or_none(task_list),
            input=str(input_data))


def start_swf_workflow_worker(domain, task_list, layer1=None, reg_remote=True,
                              package=None, ignore=None, setup_log=True,
                              identity=None, registry=None):
    """Start an endless single threaded/single process workflow worker loop.

    The worker polls endlessly for new decisions from the specified domain and
    task list and runs them.

    If no registry is passed, a new one is created and used to scan for
    workflows. Package and ignore can be used to control the scanning.

    If reg_remote is set, all registered workflow are registered remotely.

    An identity can be set to track this worker in the SWF console, otherwise
    a default identity is generated from this machine domain and process pid.

    If setup_log is set, a default configuration for the logger is loaded.

    A custom SWF client can be passed in layer1, otherwise a default client is
    instantiated and used.
    """
    if setup_log:
        setup_default_logger()
    identity = identity if identity is not None else _default_identity()
    identity = str(identity)[:_IDENTITY_SIZE]
    layer1 = layer1 if layer1 is not None else Layer1()
    if registry is None:
        registry = SWFWorkflowRegistry()
        # Add an extra level when scanning because of this function
        registry.scan(package=package, ignore=ignore, level=1)
    if reg_remote:
        try:
            registry.register_remote(layer1, domain)
        except _RegistrationError:
            logger.exception('Not all workflows could be registered:')
            print('Not all workflows could be registered.', file=sys.stderr)
            sys.exit(1)
    try:
        while 1:
            context = poll_next_decision(layer1, domain, task_list, identity)
            registry(context)  # execute the workflow
    except KeyboardInterrupt:
        pass


class SWFWorkflowStarter(object):
    """A simple workflow starter."""
    def __init__(self, layer1=None, setup_log=True):
        self.layer1 = layer1 if layer1 is not None else Layer1()
        if setup_log:
            setup_default_logger()
        self.registry = {}

    def start(self, domain, name, version, task_list=None,
              decision_duration=None, workflow_duration=None, wid=None,
              tags=None, serialize_input=_serialize_input, child_policy=None):
        """Prepare to start a new workflow, returns a callable.

        The callable should be called only with the input arguments and will
        start the workflow.
        """
        def really_start(*args, **kwargs):
            """Use this function to start a workflow by passing in the args."""
            l_wid = wid  # closue hack
            if l_wid is None:
                l_wid = uuid.uuid4()
            try:
                self.layer1.start_workflow_execution(
                    str(domain), str(l_wid), str(name), str(version),
                    task_list=_str_or_none(task_list),
                    execution_start_to_close_timeout=_str_or_none(workflow_duration),
                    task_start_to_close_timeout=_str_or_none(decision_duration),
                    input=str(serialize_input(*args, **kwargs))[:_INPUT_SIZE],
                    child_policy=_cp_encode(child_policy),
                    tag_list=_tags(tags))
            except SWFResponseError:
                return False
            return True
        return really_start




class DescCounter(object):
    """A simple semaphore-like descendent counter."""
    def __init__(self, to=None):
        if to is None:
            self.iterator = itertools.repeat(True)
        else:
            self.iterator = itertools.chain(itertools.repeat(True, to),
                                            itertools.repeat(False))

    def consume(self):
        """Conusme one position; returns True if positions are available."""
        return next(self.iterator)


def _default_identity():
    """Generate a local identity for this process."""
    identity = "%s-%s" % (socket.getfqdn(), os.getpid())
    return identity[-_IDENTITY_SIZE:]  # keep the most important part


class _PaginationError(Exception):
    """Can't retrieve the next page after X retries."""


class _RegistrationError(Exception):
    """Can't register a task remotely because of default config conflicts."""


def _subworkflow_id(workflow_id):
    return workflow_id.rsplit('-', 1)[-1]


def _timer_key(call_key):
    return '%s:t' % call_key


def _timer_call_key(timer_key):
    assert timer_key.endswith(':t')
    return timer_key[:-2]


def _subworkflow_key(call_key):
    return '%s:%s' % (uuid.uuid4(), call_key)


def _subworkflow_call_key(subworkflow_key):
    return subworkflow_key.split(':')[-1]


def _timer_encode(val, name):
    if val is None:
        return None
    val = max(int(val), 0)
    if val == 0:
        raise ValueError('The value of %r must be a strictly positive integer: %r' % (name, val))
    return str(val)


def _str_or_none(val):
    if val is None:
        return None
    return str(val)


def _cp_encode(val):
    if val is not None:
        val = str(val).upper()
    if val not in _CHILD_POLICY:
        raise ValueError('Invalid child policy value: %r' % val)
    return val


def _tags(tags):
    if tags is None:
        return None
    return list(set(str(t) for t in tags))[:5]
