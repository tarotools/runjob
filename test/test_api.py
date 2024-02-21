import pytest

import runtools.runcore
from runtools.runcore.client import APIClient, APIErrorType, ErrorCode, ApprovalResult, StopResult
from runtools.runcore.criteria import parse_criteria, EntityRunCriteria
from runtools.runcore.run import RunState, PhaseNames, TerminationStatus
from runtools.runcore.test.job import FakeJobInstanceBuilder, FakePhase
from runtools.runner.api import APIServer


@pytest.fixture(autouse=True)
def job_instances():
    server = APIServer()

    j1 = FakeJobInstanceBuilder('j1', 'i1').add_phase('EXEC', RunState.EXECUTING).build()
    server.register_instance(j1)
    j1.phaser.next_phase()

    j2 = FakeJobInstanceBuilder('j2', 'i2').add_phase(PhaseNames.APPROVAL, RunState.PENDING).build()
    server.register_instance(j2)
    j2.phaser.next_phase()

    assert server.start()
    try:
        yield j1, j2
    finally:
        server.close()


def test_error_not_found():
    with APIClient() as c:
        _, errors = c.send_request('/no-such-api')
    assert errors[0].error_type == APIErrorType.API_CLIENT
    assert errors[0].response_error.code == ErrorCode.NOT_FOUND


def test_instances_api():
    multi_resp = runtools.runcore.get_active_runs()
    instances = {inst.job_id: inst for inst in multi_resp.responses}
    assert instances['j1'].run.lifecycle.run_state == RunState.EXECUTING
    assert instances['j2'].run.lifecycle.run_state == RunState.PENDING

    multi_resp_j1 = runtools.runcore.get_active_runs(parse_criteria('j1'))
    multi_resp_j2 = runtools.runcore.get_active_runs(parse_criteria('j2'))
    assert multi_resp_j1.responses[0].job_id == 'j1'
    assert multi_resp_j2.responses[0].job_id == 'j2'

    assert not any([multi_resp.errors, multi_resp_j1.errors, multi_resp_j2.errors])


def test_approve_pending_instance(job_instances):
    instances, errors = runtools.runcore.approve_pending_instances(EntityRunCriteria.all(), PhaseNames.APPROVAL)

    assert not errors
    assert instances[0].instance_metadata.entity_id == 'j1'
    assert instances[0].release_result == ApprovalResult.NOT_APPLICABLE
    assert instances[1].instance_metadata.entity_id == 'j2'
    assert instances[1].release_result == ApprovalResult.APPROVED

    _, j2 = job_instances
    assert j2.get_typed_phase(FakePhase, PhaseNames.APPROVAL).approved


def test_stop(job_instances):
    instances, errors = runtools.runcore.stop_instances(parse_criteria('j1'))
    assert not errors
    assert len(instances) == 1
    assert instances[0].instance_metadata.entity_id == 'j1'
    assert instances[0].stop_result == StopResult.STOP_INITIATED

    j1, j2 = job_instances
    assert j1.job_run_info().run.termination.status == TerminationStatus.STOPPED
    assert not j2.job_run_info().run.termination


def test_tail(job_instances):
    j1, j2 = job_instances
    j1.output.add('EXEC', 'Meditate, do not delay, lest you later regret it.', False)

    instances, errors = runtools.runcore.fetch_output()
    assert not errors

    assert instances[0].instance_metadata.entity_id == 'j1'
    assert instances[0].output == [['Meditate, do not delay, lest you later regret it.', False]]

    assert instances[1].instance_metadata.entity_id == 'j2'
    assert not instances[1].output
