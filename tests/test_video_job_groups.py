import shutil

from src import h3_video as h3
from tests.test_h3_seed_hunter_api import hunter, preview, final_body
from tests.test_video_submission import backend


def refine(hunter, job, candidate):
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize",
                                   json={**final_body(hunter), 'candidate': candidate})
    assert response.status_code == 201, response.text
    return response.json()


def test_grouped_history_keeps_candidates_and_all_refinements_on_one_anchored_card(hunter):
    job, directory = preview(hunter)
    client = hunter.client()
    url = '/api/video/h3/jobs'
    params = {'page': 1, 'page_size': 1, 'recipe': 'seed_hunter', 'stage': 'not_scout', 'group_refinements': 'true'}
    first = client.get(url, params=params).json()
    params['before'] = first['pagination']['before']
    children = [refine(hunter, job, index) for index in (1, 3, 1)]
    result = client.get(url, params=params).json()
    assert result['grouped_refinements'] is True
    assert result['pagination']['total'] == 1
    assert result['counts']['queued'] == 3
    assert result['queue']['counts']['queued'] == 3
    assert [row['id'] for row in result['jobs']] == [job['id']]
    parent = result['jobs'][0]
    assert len(parent['seed_hunter']['candidates']) == 3
    assert [row['id'] for row in parent['refinements']] == [row['id'] for row in children]
    assert [row['seed_hunter']['source_candidate'] for row in parent['refinements']] == [1, 3, 1]
    assert [row['queue_position'] for row in parent['refinements']] == [1, 2, 3]
    assert len(client.get(url).json()['jobs']) == 4  # Existing clients keep their flat contract.
    detail = client.get(url + '/' + job['id']).json()
    assert detail['refinements'] == parent['refinements']
    child_detail = client.get(url + '/' + children[0]['id']).json()
    assert child_detail['refinement_parent'] == job['id']
    assert child_detail['refinements'] == []
    assert len(list(directory.glob('candidate-*.mp4'))) == 3


def test_deleted_parent_leaves_owned_refinements_visible_and_playable(hunter):
    job, directory = preview(hunter)
    child = refine(hunter, job, 2)
    shutil.rmtree(directory)
    client = hunter.client()
    result = client.get('/api/video/h3/jobs?recipe=seed_hunter&group_refinements=true&page=1').json()
    assert [row['id'] for row in result['jobs']] == [child['id']]
    assert result['pagination']['total'] == 1
    detail = client.get('/api/video/h3/jobs/' + child['id']).json()
    assert 'refinement_parent' not in detail
    assert (hunter.managers['h3'].directory(child['id']) / 'source-latent.safetensors').is_file()


def test_foreign_or_invalid_parent_cannot_hide_jobs_or_expose_refinements(hunter):
    job, directory = preview(hunter)
    child = refine(hunter, job, 1)
    state = h3._read(directory / 'state.json')
    h3._write(directory / 'state.json', {**state, 'owner': 'other'})
    client = hunter.client()
    url = '/api/video/h3/jobs?recipe=seed_hunter&group_refinements=true&page=1'
    assert [row['id'] for row in client.get(url).json()['jobs']] == [child['id']]
    foreign = client.get(url, headers={'x-user': 'other'}).json()['jobs']
    assert len(foreign) == 1 and foreign[0]['refinements'] == []
    assert client.get('/api/video/h3/jobs/' + job['id']).status_code == 404
    h3._write(directory / 'state.json', state)
    manifest = h3._read(directory / 'manifest.json')
    manifest['config']['seed_hunter']['stage'] = 'single'
    h3._write(directory / 'manifest.json', manifest)
    assert len(client.get(url).json()['jobs']) == 2


def test_grouping_validation_is_explicit(hunter):
    for path in ['h3/jobs?group_refinements=true', 'bfs/jobs?group_refinements=true',
                 'h3/jobs?recipe=seed_hunter&group_refinements=maybe']:
        assert hunter.client().get('/api/video/' + path).status_code == 400


def test_missing_preview_video_does_not_hide_surviving_refinement(hunter):
    job, directory = preview(hunter)
    child = refine(hunter, job, 2)
    (directory / 'candidate-2.mp4').unlink()
    result = hunter.client().get('/api/video/h3/jobs?recipe=seed_hunter&group_refinements=true&page=1').json()
    assert {row['id'] for row in result['jobs']} == {job['id'], child['id']}
    assert all(not row['refinements'] for row in result['jobs'])
