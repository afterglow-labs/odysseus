import io
import json
from urllib.parse import parse_qs, urlparse

import pytest

from services.hwfit import hf_discovery, fit


@pytest.fixture(autouse=True)
def clean_search_cache():
    hf_discovery._SEARCH_CACHE.clear()
    yield
    hf_discovery._SEARCH_CACHE.clear()


def test_keyword_search_discovers_community_ggufs_and_matches_all_words(monkeypatch):
    calls = []
    repo = "JonathanColetti/Qwen3.8-27B-Uncensored-GGUF"
    def fetch(req, timeout):
        calls.append(parse_qs(urlparse(req.full_url).query))
        return io.StringIO(json.dumps([
            {"id": repo, "pipeline_tag": "text-generation", "tags": ["gguf"]},
            {"id": "community/Llama-8B-Uncensored", "pipeline_tag": "text-generation"},
            {"id": "community/Image-7B-Uncensored", "pipeline_tag": "text-to-image"},
        ]))
    monkeypatch.setattr(hf_discovery.urllib.request, "urlopen", fetch)
    candidates = hf_discovery.search_hf_models("Qwen UNcensored")
    assert calls[0]["search"] == ["uncensored"]
    assert len(candidates) == 2
    system = {"has_gpu": True, "backend": "cuda", "gpu_vram_gb": 32,
              "gpu_count": 1, "available_ram_gb": 64, "total_ram_gb": 64}
    results = fit.rank_models(system, search="Qwen UNcensored", candidate_models=candidates)
    assert [r["name"] for r in results] == [repo]
    assert candidates[0]["is_gguf"] is True
    assert candidates[0]["parameters_raw"] == 27_000_000_000
    assert hf_discovery.search_hf_models("Uncensored") == candidates
    assert len(calls) == 1


def test_search_route_includes_results_outside_catalog(monkeypatch):
    from services.hwfit import hardware, models
    from routes.hwfit_routes import setup_hwfit_routes
    monkeypatch.setattr(hardware, "detect_system", lambda **kw: {"has_gpu": False, "backend": "cpu", "available_ram_gb": 64, "total_ram_gb": 64})
    monkeypatch.setattr(models, "get_models", lambda: [])
    monkeypatch.setattr(hf_discovery, "search_hf_models", lambda query: [{
        "name": "community/Qwen-7B-Uncensored-GGUF", "parameters_raw": 7_000_000_000,
        "parameter_count": "7B", "quantization": "Q4_K_M", "context_length": 8192,
        "is_gguf": True, "use_case": "general",
    }])
    handler = next(r.endpoint for r in setup_hwfit_routes().routes if r.path.endswith("/models"))
    result = handler(search="Uncensored")
    assert result["models"][0]["name"] == "community/Qwen-7B-Uncensored-GGUF"


def test_search_failure_is_visible_instead_of_false_empty_result(monkeypatch):
    from services.hwfit import hardware, models
    from routes.hwfit_routes import setup_hwfit_routes
    monkeypatch.setattr(hardware, "detect_system", lambda **kw: {"has_gpu": False})
    monkeypatch.setattr(models, "get_models", lambda: [{"name": "unrelated", "parameters_raw": 7_000_000_000}])
    def fail(query):
        raise TimeoutError("Hub timed out")
    monkeypatch.setattr(hf_discovery, "search_hf_models", fail)
    handler = next(r.endpoint for r in setup_hwfit_routes().routes if r.path.endswith("/models"))
    result = handler(search="Uncensored")
    assert "Hub timed out" in result["error"]
