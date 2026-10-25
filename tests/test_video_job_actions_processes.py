from tests.test_video_queue import queue, wait_for
from tests.test_h3_gpu_delete import db
from src import h3_video as h3, video_job_actions as actions


def test_group_delete_stops_real_detached_workers_and_preserves_other_job(queue, db):
    managers, launch, root = queue
    manager = managers[0]
    first, _, _ = launch(hold=True)
    wait_for(lambda: (first / "started").exists(), bool)
    child, _, _ = launch()
    manifest = h3._read(child / "manifest.json")
    h3._write(child / "manifest.json", {**manifest, "seed_hunter_source": {"source_job_id": first.name}})
    unrelated, _, _ = launch()
    result = actions.perform(manager, "corey", "h3", first.name, "delete")
    assert result['failed'] == 0, result
    assert not first.exists() and not child.exists()
    wait_for(lambda: manager.view(unrelated.name, "corey")['status'], lambda s: s=='completed')
    assert unrelated.exists() and not (root/'overlap').exists()
