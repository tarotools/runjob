"""
This module contains event dispatchers as part of the Job Instance Event framework.
The dispatchers deliver events of registered job instances to the listeners using domain socket communication.
The event listeners are expected to create a domain socket with a corresponding file suffix located
in the user's own subdirectory, which is in the `/tmp` directory by default. The sockets are used by the dispatchers
to send the events. In this design, the communication is unidirectional, with servers acting as consumers
and clients as producers.
"""

import abc
import json
import logging

from runtools.runcore import util, paths
from runtools.runcore.job import JobInstanceMetadata, JobRun, InstanceTransitionObserver, InstanceOutputObserver, \
    InstanceTransitionEvent, InstanceOutputEvent
from runtools.runcore.output import OutputLine
from runtools.runcore.util.socket import SocketClient, PayloadTooLarge

TRANSITION_LISTENER_FILE_EXTENSION = '.tlistener'
OUTPUT_LISTENER_FILE_EXTENSION = '.olistener'

log = logging.getLogger(__name__)


class EventDispatcher(abc.ABC):
    """
    This serves as a parent class for event producers. The subclasses (children) are expected to provide a specific
    socket client and utilize the `_send_event` method to dispatch events to the respective consumers.
    """

    @abc.abstractmethod
    def __init__(self, client):
        self._client = client

    def _send_event(self, event_type, instance_meta, event_serialized):
        event_body = {
            "event_metadata": {
                "event_type": event_type
            },
            "instance_metadata": instance_meta.serialize(),
            "event": event_serialized
        }
        try:
            self._client.communicate(json.dumps(event_body))
        except PayloadTooLarge:
            log.warning("event=[event_dispatch_failed] reason=[payload_too_large] note=[Please report this issue!]")

    def close(self):
        self._client.close()


class TransitionDispatcher(EventDispatcher, InstanceTransitionObserver):
    """
    This producer emits an event when the state of a job instance changes. This dispatcher should be registered to
    the job instance as an `InstanceStateObserver`.
    """

    def __init__(self):
        super(TransitionDispatcher, self).__init__(
            SocketClient(paths.socket_files_provider(TRANSITION_LISTENER_FILE_EXTENSION)))

    def new_instance_transition(self, event: InstanceTransitionEvent):
        self._send_event("new_instance_transition", event.instance, event.serialize())


class OutputDispatcher(EventDispatcher, InstanceOutputObserver):
    """
    This producer emits an event when a job instance generates a new output. This dispatcher should be registered to
    the job instance as an `JobOutputObserver`.
    """

    def __init__(self):
        super(OutputDispatcher, self).__init__(
            SocketClient(paths.socket_files_provider(OUTPUT_LISTENER_FILE_EXTENSION)))

    def new_instance_output(self, event: InstanceOutputEvent):
        self._send_event("new_instance_output", event.instance, event.serialize(10000))
