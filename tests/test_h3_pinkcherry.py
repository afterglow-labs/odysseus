from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts.h3_pinkcherry_controls import COMPONENTS, LORA
from scripts.h3_video_worker import validate_job
from src import h3_video as h3, h3_pinkcherry as pink
from src.video_job_listing import list_options
from src.video_history_delete import _matches
from src.video_queue_bulk import _patched
from tests.test_h3_video import inventory as base_inventory, config, weights


@pytest.fixture
def inventory(base_inventory, tmp_path):
    for role, name in COMPONENTS.items():
        weights(tmp_path / name, lora=role == "lora")
    base_inventory["components"] = h3.discover_components([tmp_path])
    return base_inventory


def test_pinkcherry_uses_exact_json_lora_and_explicit_heretic_replacement(inventory):
    cap = pink.capabilities(inventory["components"])
    assert cap["ready"]
    assert [p["config"]["steps"] for p in cap["presets"]] == [12, 4]
    raw = {**config(inventory, "ref2va"), **cap["defaults"]}
    resolved = h3.validate_config(raw, inventory["components"], inventory["gpus"], {"reference_images": ["/owned/image.png"]})
    assert resolved["recipe"] == "pinkcherry" and "seed_hunter" not in resolved
    assert Path(resolved["loras"][0]["path"]).name == LORA
    assert resolved["loras"][0]["strength"] == 1
    assert "heretic" in resolved["encoder"]
    assert (resolved["width"], resolved["height"], resolved["frames"], resolved["steps"]) == (672, 1216, 294, 12)
    assert resolved["sampler"] == "euler" and resolved["scheduler"] == "simple"
    assert (resolved["shift_video"], resolved["shift_audio"]) == (1, 1)
    updated = _patched(resolved, {"steps": 4}, inventory, "h3", {"reference_images": ["/owned/image.png"]})
    assert updated["steps"] == 4 and updated["loras"] == resolved["loras"]


def test_pinkcherry_missing_exact_lora_is_visible(inventory):
    cap = pink.capabilities([c for c in inventory["components"] if c["name"] != LORA])
    assert cap["missing"] == [LORA] and not cap["ready"]
    assert cap["defaults"]["loras"] == [{"id": LORA, "strength": 1}]


def test_pinkcherry_history_is_independent():
    assert list_options({"recipe": "pinkcherry"}, "h3")["recipe"] == "pinkcherry"
    manager = SimpleNamespace(root=Path("/jobs/h3"))
    job = {"config": {"recipe": "pinkcherry"}}
    assert _matches(manager, {}, job, "workflow", "pinkcherry", None)
    assert not _matches(manager, {}, job, "workflow", None, None)
