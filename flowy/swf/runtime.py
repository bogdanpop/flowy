from boto.swf.exceptions import SWFResponseError


class Heartbeat(object):
    def __init__(self, client, token):
        self._client = client
        self._token = token

    def __call__(self):
        try:
            self._client.record_activity_task_heartbeat(task_token=self._token)
        except SWFResponseError:
            return False
        return True


class ActivityResult(object):
    def __init__(self, client, token):
        self._client = client
        self._token = token

    def complete(self, result):
        try:
            self._client.respond_activity_task_completed(
                task_token=self._token, result=result
            )
        except SWFResponseError:
            return False
        return True

    def fail(self, reason):
        try:
            self._client.respond_activity_task_failed(
                task_token=self._token, reason=reason
            )
        except SWFResponseError:
            return False
        return True

    def suspend(self):
        pass


class DecisionRuntime(object):
    def __init__(self, client, token):
        self._client = client
        self._token = token

    def remote_activity(self, result_deserializer,
                        heartbeat=None,
                        schedule_to_close=None,
                        schedule_to_start=None,
                        start_to_close=None,
                        task_list=None,
                        retry=None,
                        delay=None,
                        error_handling=None):
        pass

    def remote_subworkflow(self, result_deserializer,
                           heartbeat=None,
                           workflow_duration=None,
                           decision_duration=None,
                           task_list=None,
                           retry=3,
                           delay=0,
                           error_handling=None):
        pass

    def complete(self, result):
        pass

    def fail(self, reason):
        pass

    def suspend(self):
        pass
