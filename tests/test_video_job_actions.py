"""Group operations use real submission, dispatch and cleanup code with fake workers."""
import uuid
import json
from pathlib import Path
import pytest
from src import h3_video as h3, video_job_actions as actions
from tests.test_video_submission import backend, send
from tests.test_h3_seed_hunter_api import hunter, preview as saved_preview
from tests.test_video_auto_refine import apply, settings, complete
from tests.test_h3_gpu_delete import db


@pytest.fixture(autouse=True)
def finish_fake_workers(monkeypatch, db):
    # The submission fixture uses pytest's own PID as its fake supervisor.
    # Model its exit when cancelled; never signal the test runner itself.
    def stop(directory):
        path = directory / 'state.json'
        h3._write(path, {**h3._read(path), 'supervisor': None, 'worker': None})
        return True
    monkeypatch.setattr(h3, 'stop_workers', stop)


def preview(hunter):
    job, folder = saved_preview(hunter)
    state = h3._read(folder / 'state.json')
    h3._write(folder / 'state.json', {**state, 'supervisor': None})
    return job, folder


def act(backend, identity, action, *, batch=False, owner="corey", key=None):
    kind = "batches" if batch else "jobs"
    return backend.client().post(f"/api/video/h3/{kind}/{identity}/actions", headers={"x-user": owner},
        json={"action": action, "request_id": key or uuid.uuid4().hex})


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_submission_persists_one_batch_identity_and_refuses_deleted_batch(backend, family):
    identity = uuid.uuid4().hex
    metadata = {'id': identity, 'name': 'Two source files', 'count': 2}
    extra = [('batch', (None, json.dumps(metadata)))]
    first = send(backend, family, extra=extra)
    assert first.status_code == 201, first.text
    assert first.json()['batch'] == metadata
    second = send(backend, family, extra=extra)
    assert second.status_code == 201
    manager = backend.managers[family]
    result = actions.perform(manager, 'corey', family, identity, 'delete', batch=True)
    assert result['failed'] == 0
    assert set(result['deleted']) == {first.json()['id'], second.json()['id']}
    refused = send(backend, family, extra=extra)
    assert refused.status_code == 409, refused.text
    assert not actions.records(manager, 'corey')


def test_priority_moves_only_selected_waiter_and_preserves_running_job(backend):
    first, second, third = [send(backend, "h3").json() for _ in range(3)]
    m = backend.managers["h3"]
    for j in (first, second, third):
        p=m.directory(j["id"])/"state.json"; s=h3._read(p); h3._write(p, {**s, "status":"queued"})
    p=m.directory(first["id"])/"state.json"; h3._write(p, {**h3._read(p), "status":"running"})
    before=p.read_bytes()
    r=act(backend,third["id"],"process_now")
    assert r.status_code==200, r.text
    assert p.read_bytes()==before
    orders=[h3._read(m.directory(j["id"])/"state.json")["queue_order"] for j in (second,third)]
    assert orders[1]<orders[0]
    assert act(backend,first["id"],"process_now").status_code==409
    assert act(backend,second["id"],"process_now",owner="other").status_code==404


def test_delete_parent_removes_queued_running_and_finished_children_and_outputs(hunter, monkeypatch):
    parent, folder=preview(hunter);m=hunter.managers["h3"]
    rows=apply(hunter,[parent["id"]],settings(hunter)).json()["jobs"][parent["id"]]
    ids=[r["job_id"] for r in rows]
    complete(m,ids[0]);from src import video_auto_refine
    video_auto_refine.publish_output(m.directory(ids[0]))
    outputs=list((m.project/"outputs/video-projects").rglob("*.mp4"));assert outputs
    for i,status in zip(ids[1:], ["queued","running"]):
        p=m.directory(i)/"state.json";h3._write(p,{**h3._read(p),"status":status})
    unrelated,_=preview(hunter)
    r=act(hunter,parent["id"],"delete")
    assert r.status_code==200,r.text
    assert r.json()["failed"]==0,r.text
    assert set(r.json()["deleted"])=={parent["id"],*ids}
    assert all(not m.directory(i).exists() for i in [parent["id"],*ids])
    assert all(not p.exists() for p in outputs)
    assert m.directory(unrelated["id"]).exists()
    assert all(Path(c["path"]).is_file() for c in hunter.inventories["h3"]["components"])
    assert act(hunter,parent["id"],"delete").json()["failed"]==0
    with pytest.raises(RuntimeError,match="deleted"):
        actions.ensure_parent_available(m,{"seed_hunter_source":{"source_job_id":parent["id"]}})


def test_delete_child_does_not_delete_parent_or_other_candidates(hunter):
    parent,folder=preview(hunter)
    ids=[r["job_id"] for r in apply(hunter,[parent["id"]],settings(hunter)).json()["jobs"][parent["id"]]]
    r=act(hunter,ids[0],"delete")
    assert r.status_code==200 and r.json()["failed"]==0,r.text
    assert folder.exists() and all(hunter.managers["h3"].directory(i).exists() for i in ids[1:])


def test_batch_card_paginates_complete_members_and_deletes_off_page_jobs(hunter):
    m=hunter.managers["h3"];batch=uuid.uuid4().hex; ids=[]
    for n in range(12):
        job,folder=preview(hunter);ids.append(job["id"])
        p=folder/"manifest.json";h3._write(p,{**h3._read(p),"batch":{"id":batch,"name":"Twelve videos","count":12}})
    children=apply(hunter,[ids[-1]],settings(hunter,candidates=[2])).json()["jobs"][ids[-1]]
    reply=hunter.client().get('/api/video/h3/jobs?recipe=seed_hunter&page=1&page_size=10&group_batches=true&group_refinements=true')
    assert reply.status_code==200,reply.text
    assert reply.json()["pagination"]["total"]==1
    assert reply.json()["jobs"][0]["batch_count"]==12
    page=hunter.client().get(f'/api/video/h3/batches/{batch}?page=2').json()
    assert len(page["jobs"])==2 and len(page["root_ids"])==12
    r=act(hunter,batch,"delete",batch=True)
    assert r.status_code==200 and r.json()["failed"]==0,r.text
    assert set(r.json()["deleted"])=={*ids,children[0]["job_id"]}
    with pytest.raises(RuntimeError,match="deleted"):
        actions.ensure_parent_available(m,{"batch":{"id":batch}})


def test_duplicate_does_not_refine_and_rerun_all_is_explicit_and_idempotent(hunter):
    parent,folder=preview(hunter)
    children=apply(hunter,[parent["id"]],settings(hunter,candidates=[1,3])).json()["jobs"][parent["id"]]
    key=uuid.uuid4().hex
    first=act(hunter,parent["id"],"duplicate",key=key)
    assert first.status_code==200,first.text
    assert len(first.json()["jobs"])==1
    assert first.json()==act(hunter,parent["id"],"duplicate",key=key).json()
    copied=h3._read(hunter.managers['h3'].directory(first.json()['jobs'][0]['id'])/'manifest.json')
    assert copied['config']['seed_hunter']['stage']=='preview' and 'auto_refine' not in copied['config']['seed_hunter']
    rerun=act(hunter,parent["id"],"rerun_all")
    assert rerun.status_code==200 and len(rerun.json()["jobs"])==3,rerun.text
    mapping={j['source_id']:j['id'] for j in rerun.json()['jobs']}
    for child in children:
        manifest=h3._read(hunter.managers['h3'].directory(mapping[child['job_id']])/'manifest.json')
        assert manifest['job_group_parent']==mapping[parent['id']]
    assert act(hunter,parent['id'],'delete').json()['failed']==0
    # Lost responses retry the same accepted copies after source cleanup.
    assert first.json()==act(hunter,parent['id'],'duplicate',key=key).json()
