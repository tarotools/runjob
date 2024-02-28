import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from logging import DEBUG
from threading import Condition, Event, Lock

import runtools.runcore
from runtools.runcore import paths
from runtools.runcore.criteria import InstanceMetadataCriterion, EntityRunCriteria, PhaseCriterion
from runtools.runcore.job import JobRun, JobRuns, InstanceTransitionObserver
from runtools.runcore.listening import InstanceTransitionReceiver
from runtools.runcore.run import RunState, Phase, TerminationStatus, PhaseRun, TerminateRun, RunContext
from runtools.runcore.util import lock, KVParser
from runtools.runcore.util.log import ForwardLogs
from runtools.runner.task import OutputToTask

log = logging.getLogger(__name__)


def output_to_task_handler(run_ctx):
    return OutputToTask(run_ctx.task_tracker, parsers=[KVParser()]).create_logging_handler()


def forward_logs(logger, run_ctx):
    return ForwardLogs(logger, [run_ctx.create_logging_handler(), output_to_task_handler(run_ctx)])


class ApprovalPhase(Phase):
    """
    Approval parameters (incl. timeout) + approval eval as separate objects
    TODO: parameters
    """

    def __init__(self, phase_name, timeout=0):
        super().__init__(phase_name, RunState.PENDING)
        self._log = logging.getLogger(self.__class__.__name__)
        self._log.setLevel(DEBUG)
        self._timeout = timeout
        self._event = Event()
        self._stopped = False

    def run(self, run_ctx: RunContext):
        with forward_logs(self._log, run_ctx):
            self._log.debug("task=[Approval] operation=[Waiting]")

            approved = self._event.wait(self._timeout or None)
            if self._stopped:
                self._log.debug("task=[Approval] result=[Cancelled]")
                return
            if not approved:
                self._log.debug("task=[Approval] result=[Not Approved]")
                raise TerminateRun(TerminationStatus.TIMEOUT)

            self._log.debug("task=[Approval] result=[Approved]")

    def approve(self):
        self._event.set()

    def is_approved(self):
        self._event.is_set() and not self._stopped

    def stop(self):
        self._stopped = True
        self._event.set()

    @property
    def stop_status(self):
        return TerminationStatus.CANCELLED


class NoOverlapPhase(Phase):
    """
    TODO Docs
    1. Set continue flag to be checked
    """

    def __init__(self, phase_name, no_overlap_id, until_phase=None, *, locker_factory=lock.default_locker_factory()):
        if not no_overlap_id:
            raise ValueError("no_overlap_id cannot be empty")

        params = {
            'phase': 'no_overlap',
            'protection_phase': 'no_overlap',
            'protection_id': no_overlap_id,
            'protected_until': until_phase
        }
        super().__init__(phase_name, RunState.EVALUATING, params)
        self._log = logging.getLogger(self.__class__.__name__)
        self._log.setLevel(DEBUG)
        self._no_overlap_id = no_overlap_id
        self._locker = locker_factory(paths.lock_path(f"noo-{no_overlap_id}.lock", True))

    def run(self, run_ctx):
        with forward_logs(self._log, run_ctx):
            self._log.debug("task=[No Overlap Check]")
            with self._locker():
                c = EntityRunCriteria(phase_criteria=PhaseCriterion(parameters=self.metadata.parameters))
                runs, _ = runtools.runcore.get_active_runs(c)
                if any(r for r in runs if r.run.in_protected_phase('no_overlap', self._no_overlap_id)):
                    self._log.debug("task=[No Overlap Check] result=[Overlap found]")
                    raise TerminateRun(TerminationStatus.OVERLAP)

        self._log.debug("task=[No Overlap Check] result=[No overlap found]")

    def stop(self):
        pass

    @property
    def stop_status(self):
        return TerminationStatus.CANCELLED


class DependencyPhase(Phase):

    def __init__(self, phase_name, dependency_match):
        self._parameters = {'phase': 'dependency', 'dependency': str(dependency_match.serialize())}
        super().__init__(phase_name, RunState.EVALUATING, self._parameters)
        self._log = logging.getLogger(self.__class__.__name__)
        self._log.setLevel(DEBUG)
        self._dependency_match = dependency_match

    @property
    def dependency_match(self):
        return self._dependency_match

    @property
    def parameters(self):
        return self._parameters

    def run(self, run_ctx):
        with forward_logs(self._log, run_ctx):
            self._log.debug("task=[Dependency pre-check] dependency=[%s]", self._dependency_match)
            runs, _ = runtools.runcore.get_active_runs()
            matches = [r.metadata for r in runs if self._dependency_match(r.metadata)]
            if not matches:
                self._log.debug("result=[No active dependency found] dependency=[%s]]", self._dependency_match)
                raise TerminateRun(TerminationStatus.UNSATISFIED)
            self._log.debug("result=[Active dependency found] matches=%s", matches)

    def stop(self):
        pass

    @property
    def stop_status(self):
        return TerminationStatus.CANCELLED


class WaitingPhase(Phase):
    """
    """

    def __init__(self, phase_name, observable_conditions, timeout=0):
        super().__init__(phase_name, RunState.WAITING)
        self._observable_conditions = observable_conditions
        self._timeout = timeout
        self._conditions_lock = Lock()
        self._event = Event()
        self._term_status = TerminationStatus.NONE

    def run(self, run_ctx):
        for condition in self._observable_conditions:
            condition.add_result_listener(self._result_observer)
            condition.start_evaluating()

        resolved = self._event.wait(self._timeout or None)
        if not resolved:
            self._term_status = TerminationStatus.TIMEOUT

        self._stop_all()
        if self._term_status:
            raise TerminateRun(self._term_status)

    def _result_observer(self, *_):
        wait = False
        with self._conditions_lock:
            for condition in self._observable_conditions:
                if not condition.result:
                    wait = True
                elif not condition.result.success:
                    self._term_status = TerminationStatus.UNSATISFIED
                    wait = False
                    break

        if not wait:
            self._event.set()

    def stop(self):
        self._stop_all()
        self._event.set()

    @property
    def stop_status(self):
        return TerminationStatus.CANCELLED

    def _stop_all(self):
        for condition in self._observable_conditions:
            condition.stop()


class ConditionResult(Enum):
    """
    Enum representing the result of a condition evaluation.

    Attributes:
        NONE: The condition has not been evaluated yet.
        SATISFIED: The condition is satisfied.
        UNSATISFIED: The condition is not satisfied.
        EVALUATION_ERROR: The condition could not be evaluated due to an error in the evaluation logic.
    """
    NONE = (auto(), False)
    SATISFIED = (auto(), True)
    UNSATISFIED = (auto(), False)
    EVALUATION_ERROR = (auto(), False)

    def __new__(cls, value, success):
        obj = object.__new__(cls)
        obj._value_ = value
        obj.success = success
        return obj

    def __bool__(self):
        return self != ConditionResult.NONE


class ObservableCondition(ABC):
    """
    Abstract base class representing a (child) waiter associated with a specific (parent) pending object.

    A waiter is designed to be held by a job instance, enabling the job to enter its waiting phase
    before actual execution. This allows for synchronization between different parts of the system.
    Depending on the parent waiting, the waiter can either be manually released, or all associated
    waiters can be released simultaneously when the main condition of the waiting is met.

    TODO:
    1. Add notifications to this class
    """

    @abstractmethod
    def start_evaluation(self) -> None:
        """
        Instructs the waiter to begin waiting on its associated condition.

        When invoked by a job instance, the job enters its pending phase, potentially waiting for
        the overarching pending condition to be met or for a manual release.
        """
        pass

    @property
    @abstractmethod
    def result(self):
        """
        Returns:
            ConditionResult: The result of the evaluation or NONE if not yet evaluated.
        """
        pass

    @abstractmethod
    def add_result_listener(self, listener):
        pass

    @abstractmethod
    def remove_result_listener(self, listener):
        pass

    def stop(self):
        pass


class Queue:

    @abstractmethod
    def create_waiter(self, job_instance):
        pass


class QueuedState(Enum):
    NONE = auto(), False
    IN_QUEUE = auto(), False
    DISPATCHED = auto(), True
    CANCELLED = auto(), True
    UNKNOWN = auto(), False

    def __new__(cls, value, dequeued):
        obj = object.__new__(cls)
        obj._value_ = value
        obj.dequeued = dequeued
        return obj

    @classmethod
    def from_str(cls, value: str):
        try:
            return cls[value.upper()]
        except KeyError:
            return cls.UNKNOWN


class QueueWaiter:

    @property
    @abstractmethod
    def state(self):
        """
        Returns:
            QueuedState: The current state of the waiter.
        """
        pass

    @abstractmethod
    def wait(self):
        pass

    @abstractmethod
    def cancel(self):
        pass

    @abstractmethod
    def signal_dispatch(self):
        pass


@dataclass
class ExecutionGroupLimit:
    group: str
    max_executions: int


class ExecutionQueue(Phase, InstanceTransitionObserver):

    def __init__(self, phase_name, queue_id, max_executions, until_phase=None, *,
                 queue_locker=lock.default_queue_locker(), state_receiver_factory=InstanceTransitionReceiver):
        parameters = {
            'phase': 'execution_queue',
            'protection_phase': 'execution_queue',
            'queue_id': queue_id,
            'protection_id': queue_id,
            'protected_until': until_phase,
            'max_executions': max_executions,
        }
        super().__init__(phase_name, RunState.IN_QUEUE, parameters)
        if not queue_id:
            raise ValueError('Queue ID must be specified')
        if max_executions < 1:
            raise ValueError('Max executions must be greater than zero')
        self._state = QueuedState.NONE
        self._queue_id = queue_id
        self._max_executions = max_executions
        self._locker = queue_locker
        self._state_receiver_factory = state_receiver_factory
        self._wait_guard = Condition()
        # vv Guarding these fields vv
        self._current_wait = False
        self._state_receiver = None

    @property
    def stop_status(self):
        return TerminationStatus.CANCELLED

    @property
    def state(self):
        return self._state

    def run(self, run_ctx):
        while True:
            with self._wait_guard:
                if self._state == QueuedState.NONE:
                    # Should new waiter run scheduler?
                    self._state = QueuedState.IN_QUEUE

                if self._state.dequeued:
                    return

                if self._current_wait:
                    self._wait_guard.wait()
                    continue

                self._current_wait = True
                self._start_listening()

            with self._locker():
                self._dispatch_next()

    def stop(self):
        with self._wait_guard:
            if self._state.dequeued:
                return

            self._state = QueuedState.CANCELLED
            self._wait_guard.notify_all()

    def signal_dispatch(self):
        with self._wait_guard:
            if self._state.dequeued:
                return False  # Cancelled

            self._state = QueuedState.DISPATCHED
            self._wait_guard.notify_all()
            return True

    def _start_listening(self):
        listen_criteria = EntityRunCriteria(phase_criteria=PhaseCriterion(parameters=self.metadata.parameters))
        self._state_receiver = self._state_receiver_factory(listen_criteria)
        self._state_receiver.add_observer_transition(self)
        self._state_receiver.start()

    def _dispatch_next(self):
        criteria = EntityRunCriteria(phase_criteria=PhaseCriterion(parameters=self.metadata.parameters))
        jobs, _ = runtools.runcore.get_active_runs(criteria)

        group_jobs_sorted = JobRuns(sorted(jobs, key=lambda job_run: job_run.run.lifecycle.created_at))
        next_count = self._max_executions - len(group_jobs_sorted.executing)
        if next_count <= 0:
            return False

        for next_proceed in group_jobs_sorted.queued:
            c = EntityRunCriteria(metadata_criteria=InstanceMetadataCriterion.for_run(next_proceed))
            signal_resp = runtools.runcore.signal_dispatch(c)
            for r in signal_resp.responses:
                if r.executed:
                    next_count -= 1
                    if next_count <= 0:
                        return

    def new_instance_phase(self, job_run: JobRun, previous_phase: PhaseRun, new_phase: PhaseRun, ordinal: int):
        with self._wait_guard:
            if not self._current_wait:
                return
            if previous_phase.phase_name in job_run.run.protected_phases('execution_queue', self._queue_id):
                self._current_wait = False
                self._stop_listening()
                self._wait_guard.notify()

    def _stop_listening(self):
        self._state_receiver.close()
        self._state_receiver.listeners.remove(self)
        self._state_receiver = None
