"""Only Scout's completed chosen latent may enter learned refinement."""
import pytest
import torch

from scripts import h3_seed_hunter as worker
from src.h3_seed_hunter import summary
from tests.test_h3_seed_hunter_api import hunter, preview, final_body
from tests.test_video_submission import backend
from src.workflow_graph_runs import GraphRunManager


@pytest.mark.parametrize("mode", ["interactive", "final", "scout"])
def test_scout_finalization_clones_chosen_result_and_rejects_partial_schedule(hunter, mode):
    hunter.configs['h3']['seed_hunter'] = {'stage': 'scout', 'scout_mode': mode}
    hunter.inventories['h3']['seed_hunter']['features']['seed_scout'] = True
    job, directory = preview(hunter)
    from src.h3_video import _read
    config = _read(directory / 'manifest.json')['config']
    candidates = summary(directory, config, {'status': 'completed'})['candidates']
    assert [row['index'] for row in candidates if row['can_refine']] == ([] if mode == 'scout' else [1])
    body = {**final_body(hunter), 'candidate': 1}
    reply = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=body)
    assert reply.status_code == (400 if mode == 'scout' else 201), reply.text
    if mode != 'scout':
        final = hunter.managers['h3'].directory(reply.json()['id'])
        assert (final / 'source-latent.safetensors').read_bytes() == (directory / 'candidate-1.safetensors').read_bytes()
        assert _read(final / 'source-latent.json')['chosen'] == 1
        assert _read(final / 'manifest.json')['config']['seed_hunter']['stage'] == 'final'
    assert len(hunter.calls) == (1 if mode == 'scout' else 2)


def test_worker_rejects_partial_scout_snapshot_even_when_called_directly(tmp_path):
    config = {'seed_hunter': {'stage': 'scout', 'scout_mode': 'scout'}, 'width': 480, 'height': 864}
    path = tmp_path / 'candidate-1.safetensors'
    worker.save_snapshot(path, latent={'samples': torch.tensor(1.)}, positive=[], config=config, seed=42)
    with pytest.raises(ValueError, match='completed selected Scout latent'):
        worker.load_snapshot({'latent': str(path), 'metadata': str(path.with_suffix('.json')), 'candidate': 1},
                             {**config, 'seed_hunter': {'stage': 'final'}})


def test_enabled_refinement_checks_upscaler_before_starting_scout(hunter, tmp_path):
    hunter.configs['h3']['seed_hunter'] = {'stage': 'scout', 'scout_mode': 'interactive'}
    features = hunter.inventories['h3']['seed_hunter']['features']
    features.update(seed_scout=True, latent_upscaler=False)
    source = {'id': 'scout', 'node_type': 'h3.seed_scout', 'family': 'h3', 'operation': 'submit',
              'config': hunter.configs['h3'], 'inputs': {}, 'dependencies': []}
    final = {'id': 'refine', 'node_type': 'hunter.refine', 'family': 'h3', 'operation': 'refine',
             'config': final_body(hunter)['config'], 'candidate': 1, 'inputs': {}, 'depends_on': 'scout', 'dependencies': ['scout']}
    plan = {'valid': True, 'plan': [source, final], 'outputs': [{'id': 'output', 'source': 'refine'}]}
    manager = GraphRunManager(tmp_path / 'runs', hunter.managers)
    result = manager.validate_plan(plan, hunter.inventories)
    assert not result['valid']
    assert any(row['node_id'] == 'refine' and 'latent upscaler' in row['message'] for row in result['issues'])
    import uuid
    with pytest.raises(ValueError, match='latent upscaler'):
        manager.accept('corey', {}, plan, {}, str(uuid.uuid4()))
    assert not hunter.calls
    features['latent_upscaler'] = True
    assert manager.validate_plan(plan, hunter.inventories)['valid']
