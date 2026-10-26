"""Created-date filtering uses client calendar bounds before pagination."""
import uuid
import pytest

from src import h3_video as h3
from tests.test_video_submission import backend
from tests.test_video_job_pagination import saved_job


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_date_bounds_are_half_open_owned_paged_and_do_not_filter_queue(backend, monkeypatch, family):
    manager = backend.managers[family]
    for index, created in enumerate([999, 1000, 1001, 1999, 2000], 1):
        saved_job(manager, index, created_at=created)
    saved_job(manager, 6, created_at=1100, owner='other')
    viewed = []
    original = manager.view
    def view(identity, *args, **kwargs):
        viewed.append(identity)
        return original(identity, *args, **kwargs)
    monkeypatch.setattr(manager, 'view', view)
    client = backend.client(); url = f'/api/video/{family}/jobs'
    bounds = dict(created_after=1000, created_before=2000)
    first = client.get(url, params={**bounds, 'page': 1, 'page_size': 2}).json()
    assert first['pagination']['total'] == 3
    assert first['date_filter'] == bounds
    assert len(viewed) == 2
    assert first['counts']['completed'] == 5
    second = client.get(url, params={**bounds, 'page': 2, 'page_size': 2, 'before': first['pagination']['before']}).json()
    assert [j['id'] for j in first['jobs'] + second['jobs']] == [f'{i:032x}' for i in [4, 3, 2]]
    unpaged = client.get(url, params=bounds).json()
    assert len(unpaged['jobs']) == 3 and 'pagination' not in unpaged
    assert len(client.get(url).json()['jobs']) == 5
    assert client.get(url, params={**bounds, 'page': 8}).json()['pagination']['page'] == 1


def test_date_filter_keeps_batch_and_its_children_together(backend):
    manager = backend.managers['h3']; batch = {'id': uuid.uuid4().hex, 'name': 'Cross midnight', 'count': 2}
    for index, created in [(1, 1500), (2, 2100)]:
        folder = saved_job(manager, index, created_at=created)
        h3._write(folder/'manifest.json', {**h3._read(folder/'manifest.json'), 'batch': batch})
    client = backend.client()
    result = client.get('/api/video/h3/jobs', params={'page': 1, 'group_batches': 'true', 'created_after': 1000, 'created_before': 2000}).json()
    assert result['pagination']['total'] == 1
    assert result['jobs'][0]['batch_count'] == 2
    next_day = client.get('/api/video/h3/jobs', params={'page': 1, 'group_batches': 'true', 'created_after': 2000, 'created_before': 3000}).json()
    assert not next_day['jobs']


@pytest.mark.parametrize('params', [dict(created_after='nan'), dict(created_before='inf'), dict(created_after=-1),
                                  dict(created_before='bad'), dict(created_after=2, created_before=1), dict(created_after=1, created_before=1)])
def test_date_bounds_validate(backend, params):
    assert backend.client().get('/api/video/h3/jobs', params=params).status_code == 400
