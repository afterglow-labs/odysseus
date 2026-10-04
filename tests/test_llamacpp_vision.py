from types import SimpleNamespace

import pytest

from src import chat_helpers, chat_handler, ai_interaction, document_processor


class Response:
    is_success = True
    def __init__(self, data): self.data = data
    def json(self): return self.data
    def raise_for_status(self): pass


@pytest.mark.parametrize('vision', [True, False])
def test_llamacpp_loaded_capability_wins_over_model_name(monkeypatch, vision):
    seen = []
    monkeypatch.setattr(chat_helpers, 'lmstudio_supports_vision', lambda *a: None)
    def get(url, **kwargs):
        seen.append((url, kwargs))
        return Response({'modalities': {'vision': vision}})
    monkeypatch.setattr(chat_helpers.httpx, 'get', get)
    assert chat_helpers.model_supports_vision('custom-finetune', 'http://localhost:8001/v1/chat/completions', {'Authorization': 'Bearer test'}) is vision
    assert seen == [('http://localhost:8001/props', {'timeout': 1.0, 'headers': {'Authorization': 'Bearer test'}})]


def test_llamacpp_probe_does_not_contact_cloud(monkeypatch):
    monkeypatch.setattr(chat_helpers.httpx, 'get', lambda *a, **kw: pytest.fail('unexpected cloud probe'))
    assert chat_helpers.llamacpp_supports_vision('https://api.provider.example/v1') is None


@pytest.mark.parametrize('path', [
    '/mnt/c/cache/models--Author--Qwen-Custom/snapshots/abc/model-Q8.gguf',
    r'C:\cache\models--Author--Qwen-Custom\snapshots\abc\model-Q8.gguf',
])
def test_configured_repo_resolves_exact_hf_cache_identity(monkeypatch, path):
    import src.database as db
    import src.auth_helpers as auth
    endpoint = SimpleNamespace(cached_models='[]')
    query = SimpleNamespace(filter=lambda *a: query, all=lambda: [endpoint])
    monkeypatch.setattr(db, 'SessionLocal', lambda: SimpleNamespace(query=lambda *a: query, close=lambda: None))
    owners = []
    monkeypatch.setattr(auth, 'owner_filter', lambda q, m, owner: owners.append(owner) or q)
    monkeypatch.setattr(ai_interaction, 'resolve_endpoint_runtime', lambda ep, owner=None: ('http://localhost:8001/v1', ''))
    monkeypatch.setattr(chat_helpers.httpx, 'get', lambda *a, **kw: Response({'data': [{'id': path}]}))
    url, model, _ = ai_interaction._resolve_model('Author/Qwen-Custom', owner='alice')
    assert model == path
    assert owners == ['alice']
    with pytest.raises(ValueError): ai_interaction._resolve_model('OtherAuthor/Qwen-Custom', owner='alice')


@pytest.mark.asyncio
async def test_chat_passes_image_bytes_to_native_vision_model(monkeypatch, tmp_path):
    import src.settings as settings
    image = tmp_path / 'image.png'; image.write_bytes(b'image-test-fixture')
    fi = {'id': 'image', 'name': 'image.png', 'mime': 'image/png', 'path': str(image)}
    uploads = SimpleNamespace(resolve_upload=lambda fid, owner=None: fi,
                              is_image_file=lambda *a: True, _inside_upload_dir=lambda path: True)
    monkeypatch.setattr(chat_handler, 'UPLOAD_DIR', str(tmp_path))
    monkeypatch.setattr(settings, 'get_setting', lambda name, default=None: True if name == 'vision_enabled' else default)
    monkeypatch.setattr(chat_helpers, 'lmstudio_supports_vision', lambda *a: None)
    monkeypatch.setattr(chat_helpers.httpx, 'get', lambda *a, **kw: Response({'modalities': {'vision': True}}))
    monkeypatch.setattr(chat_handler, 'analyze_image_with_vl_result', lambda *a, **kw: pytest.fail('must use image bytes, not caption fallback'))
    handler = chat_handler.ChatHandler(None, None, None, None, None, uploads)
    session = SimpleNamespace(model='Qwen3.5-custom', endpoint_url='http://localhost:8001/v1', owner='alice', id='test')
    enhanced, content, _, _, metadata = await handler.preprocess_message('What do you see?', ['image'], session)
    assert isinstance(content, list)
    assert any(b.get('type') == 'image_url' and b['image_url']['url'].startswith('data:image/png;base64,') for b in content)
    assert 'No vision' not in enhanced
    assert metadata[0]['vision_model'] == session.model


def test_configured_but_unresolved_vision_is_not_reported_as_unconfigured(monkeypatch):
    monkeypatch.setattr(document_processor, '_load_vl_settings', lambda: {'vision_enabled': True, 'vision_model': 'configured'})
    def unavailable(*a, **kw): raise ValueError('endpoint unavailable')
    monkeypatch.setattr(document_processor, '_resolve_vl_model', unavailable)
    result = document_processor.analyze_image_with_vl_result('unused.png', owner='alice')
    assert 'Configured vision model could not be resolved' in result['text']
