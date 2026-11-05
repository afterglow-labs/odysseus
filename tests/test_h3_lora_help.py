"""Documentation must cover discovered adapters without changing execution profiles."""
import json
from pathlib import Path
from urllib.parse import urlparse

from scripts.h3_lora_controls import write_profile
from scripts.h3_lora_help import known_h3_lora, lora_documentation
from src.h3_video import discover_components
from tests.test_h3_video import weights


CATALOG = json.loads((Path(__file__).parents[1] / 'scripts/h3_lora_help.json').read_text())


def test_every_catalog_adapter_is_discoverable_with_complete_help(tmp_path):
    for name in CATALOG:
        weights(tmp_path / name, True)
    found = {row['name']: row for row in discover_components([tmp_path])}
    assert set(found) == set(CATALOG)
    for name, row in found.items():
        assert row['role'] == 'lora'
        help = row['lora_help']
        assert help['title'] and help['summary'] and help['trigger'] and help['sources']
        assert help['usage'] or help['settings'] or help['caveats']
        assert known_h3_lora(name.upper())
        assert help == lora_documentation(name.upper())
        assert all(urlparse(link['url']).scheme == 'https' and urlparse(link['url']).netloc for link in help['sources'])


def test_saved_custom_and_empty_profiles_do_not_mask_publisher_help(tmp_path):
    name = 'h3-realism-people-t2v-i2v-r2v.safetensors'
    path = weights(tmp_path / name, True)
    for values in [{}, {'notes': 'My own notes', 'trigger_words': 'my-token', 'variant': 'ref2va'}]:
        saved = write_profile(path, values, overwrite=True)
        row, = discover_components([tmp_path])
        assert row['lora_profile'] == saved
        assert row['lora_help']['trigger_words'] == 'r34l1sm'
        row['lora_help']['sources'].clear()
        assert lora_documentation(name)['sources']


def test_unknown_adapter_is_honest_and_checkpoint_is_not_added_as_lora(tmp_path):
    weights(tmp_path / 'HMBreastsV2.safetensors', False)
    assert discover_components([tmp_path]) == []
    unknown = weights(tmp_path / 'minimax_h3_custom_adapter.safetensors', True)
    row, = discover_components([tmp_path])
    assert row['name'] == unknown.name and row['lora_help']['coverage'] == 'unidentified'
    assert row['lora_help']['trigger'].startswith('Unknown.')
    assert not row['lora_help']['trigger_words'] and not row['lora_help']['sources']


def test_bfs_adapters_receive_the_same_documentation(tmp_path):
    from src.bfs_video import discover_components as discover_bfs
    name = 'minimax_h3_head_swap_v1.0_r32.safetensors'
    weights(tmp_path / name, True)
    row, = discover_bfs([tmp_path])
    assert row['lora_help'] == lora_documentation(name)
