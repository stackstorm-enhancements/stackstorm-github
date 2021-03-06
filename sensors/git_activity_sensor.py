import six
import eventlet
from github import Github

import json
from st2client.client import Client
from st2client.models import KeyValuePair

from st2reactor.sensor.base import PollingSensor

eventlet.monkey_patch(
    os=True,
    select=True,
    socket=True,
    thread=True,
    time=True)

DATE_FORMAT_STRING = '%Y-%m-%d %H:%M:%S'


# Default Github API url
DEFAULT_API_URL = 'https://api.github.com'


class GitActivitySensor(PollingSensor):
    def __init__(self, sensor_service, config=None, poll_interval=None):
        super(GitActivitySensor, self).__init__(sensor_service=sensor_service,
                                                     config=config,
                                                     poll_interval=poll_interval)
        self._trigger_ref = 'github.activity_sensor'
        self._logger = self._sensor_service.get_logger(__name__)

        self._last_event_ids = {}
        self.EVENT_TYPE_WHITELIST = []
        self._st2client = Client()
        self._repositories_cache = {}

    def setup(self):
        activity_sensor = self._config.get('activity_sensor', None)
        if activity_sensor is None:
            raise ValueError('"activity_sensor" config value is required.')

        self.EVENT_TYPE_WHITELIST = activity_sensor.get('event_type_whitelist', [])

    def poll(self):
        self._logger.debug('Checking repository activity')
        gitorgs = self._st2client.keys.get_by_name(name='git-orgs', decrypt=True)
        if gitorgs:
            orgs=json.loads(gitorgs.value)
        else:
            orgs={}
        for org_name in orgs:
            org = orgs[org_name]
            if 'repositories' not in org:
                continue
            token = org['token']
            user = org['user']
            repositories = org['repositories']
            event_type_whitelist = org.get('event_type_whitelist', self.EVENT_TYPE_WHITELIST)

            if org['type'] == 'online':
                config_base_url = DEFAULT_API_URL
            else:
                if org['url']:
                    config_base_url = org['url']
                else:
                    self._logger.debug('URL is not configured for org: "%s"' % (user))
                    continue
                
            client = None 
            userobj = None
            for repo_name in repositories:
                repository = {}
                key = org_name + '|' + repo_name + '|' + config_base_url
                if key in self._repositories_cache:
                    repository = self._repositories_cache[key]
                    self._logger.debug('Repository found in cache: "%s/%s"' %(user, repo_name))
                else:
                    if client == None:
                        client = Github(token or None, base_url=config_base_url)
                        userobj = client.get_user(user)
                    repo = userobj.get_repo(repo_name)
                    repository['repository'] = repo
                    self._repositories_cache[key] = repository
                    self._logger.debug('Creating new repository: "%s/%s"' %(user, repo_name))
                repository['used'] = True
                self._logger.debug('Processing dynamic repository: "%s/%s"' %(user, repo_name))
                self._process_repository(name=repo_name,
                                         repository=repository['repository'], whitelist=event_type_whitelist)
        for repo in list(self._repositories_cache):
            if self._repositories_cache[repo]['used']:
                self._repositories_cache[repo]['used'] = False
            else:
                del self._repositories_cache[repo]


    def _process_repository(self, name, repository, whitelist):
        """
        Retrieve events for the provided repository and dispatch triggers for
        new events.

        :param name: Repository name.
        :type name: ``str``

        :param repository: Repository object.
        :type repository: :class:`Repository`
        """
        self._logger.debug('in _process_repository "%s/%s"' %
                               (name, repository))
        assert(isinstance(name, six.text_type))

        last_event_id = self._get_last_id(name=name)

        # Assume a default value of 30. Better for the sensor to operate with some
        # default value in this case rather than raise an exception.
        count = self._config['repository_sensor'].get('count', 30)
        events = repository.get_events()
        event_list = []
        for event in events:
            if len(event_list) == count:
                break
            event_list.append(event)

        event_list.reverse()
        for event in event_list:
            if last_event_id and int(event.id) <= int(last_event_id):
                # This event has already been processed
                continue

            self._handle_event(repository=name, event=event, whitelist=whitelist or {})

        if len(event_list):
            self._set_last_id(name=name, last_id=event_list[-1].id)

    def cleanup(self):
        pass

    def add_trigger(self, trigger):
        pass

    def update_trigger(self, trigger):
        pass

    def remove_trigger(self, trigger):
        pass

    def _get_last_id(self, name):
        """
        :param name: Repository name.
        :type name: ``str``
        """
        if not self._last_event_ids.get(name, None) and hasattr(self._sensor_service, 'get_value'):
            key_name = 'last_id.%s' % (name)
            self._last_event_ids[name] = self._sensor_service.get_value(name=key_name)

        return self._last_event_ids.get(name, None)

    def _set_last_id(self, name, last_id):
        """
        :param name: Repository name.
        :type name: ``str``
        """
        self._last_event_ids[name] = last_id

        if hasattr(self._sensor_service, 'set_value'):
            key_name = 'last_id.%s' % (name)
            self._sensor_service.set_value(name=key_name, value=last_id)

    def _handle_event(self, repository, event, whitelist):
        self._logger.debug('_handle_event: whitelist %s' % (whitelist))
        if event.type not in whitelist:
            self._logger.debug('Skipping ignored event (type=%s)' % (event.type))
            return

        self._dispatch_trigger_for_event(repository=repository, event=event)

    def _dispatch_trigger_for_event(self, repository, event):
        trigger = self._trigger_ref

        created_at = event.created_at

        if created_at:
            created_at = created_at.strftime(DATE_FORMAT_STRING)

        # Common attributes
        payload = {
            'repository': repository,
            'id': event.id,
            'created_at': created_at,
            'type': event.type,
            'actor': {
                'id': event.actor.id,
                'login': event.actor.login,
                'name': event.actor.name,
                'email': event.actor.email,
                'loaction': event.actor.location,
                'bio': event.actor.bio,
                'url': event.actor.html_url
            },
            'payload': {}
        }

        event_specific_payload = self._get_payload_for_event(event=event)
        payload['payload'] = event_specific_payload
        self._sensor_service.dispatch(trigger=trigger, payload=payload)

    def _get_payload_for_event(self, event):
        payload = event.payload or {}
        return payload
