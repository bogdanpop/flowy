class HistoryEvent(object):
    subdict = None

    def __init__(self, api_response):
        self.api_response = api_response

    @property
    def _my_attributes(self):
        if self.subdict is None:
            return self.api_response
        return self.api_response[self.subdict]

    @property
    def event_type(self):
        return self._my_attributes['eventType']

    @property
    def event_id(self):
        return self._my_attributes['eventId']


class ActivityScheduled(HistoryEvent):
    subdict = 'activityTaskScheduledEventAttributes'

    @property
    def activity_id(self):
        return self._my_attributes['activityId']


class ActivityCompleted(HistoryEvent):
    subdict = 'activityTaskCompletedEventAttributes'

    @property
    def scheduled_event_id(self):
        return self._my_attributes['scheduledEventId']

    @property
    def result(self):
        return self._my_attributes['result']


class DecisionTask(object):
    def __init__(self, api_response):
        self.api_response = api_response

    @property
    def name(self):
        return self.api_response['workflowType']['name']

    @property
    def version(self):
        return self.api_response['workflowType']['version']

    @property
    def token(self):
        return self.api_response['taskToken']

    @property
    def events(self):
        for event in self.api_response['events']:
            yield HistoryEvent(event)

    @property
    def scheduled_activities(self):
        for event in self.events:
            if event.event_type == 'ActivityTaskScheduled':
                yield ActivityScheduled(event)

    @property
    def completed_activities(self):
        for event in self.events:
            if event.event_type == 'ActivityTaskCompleted':
                yield ActivityCompleted(event)

    def scheduled_activity_by_event_id(self, id, default=None):
        for scheduled_activity in self.scheduled_activities:
            if scheduled_activity.event_id == id:
                return scheduled_activity
        return default

    def scheduled_activity_by_activity_id(self, id, default=None):
        for scheduled_activity in self.scheduled_activities:
            if scheduled_activity.event_id == id:
                return scheduled_activity
        return default

    def completed_activity_by_scheduled_id(self, id, default=None):
        for completed_activity in self.completed_activities:
            if completed_activity.scheduled_event_id == id:
                return completed_activity
        return default

    def completed_activity_by_activity_id(self, id, default=None):
        sa = self.scheduled_activity_by_activity_id(id)
        if sa is not None:
            ca = self.completed_activity_by_scheduled_id(sa.event_id)
            if ca is not None:
                return ca
        return default


class ActivityTask(object):
    def __init__(self, api_response):
        self.api_response = api_response

    @property
    def name(self):
        return self.api_response['activityType']['name']

    @property
    def version(self):
        return self.api_response['activityType']['version']

    @property
    def token(self):
        return self.api_response['taskToken']

    @property
    def input(self):
        return self.api_response['input']
