"""Offline, version-specific LoRA documentation, separate from execution profiles.

Inventory reads never fetch remote model cards. A saved profile may customize a
LoRA without hiding its publisher documentation or changing a frozen job.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _catalog():
    data = json.loads(Path(__file__).with_name("h3_lora_help.json").read_text(encoding="utf-8"))
    return {name.casefold(): row for name, row in data.items()}


def known_h3_lora(name):
    return Path(str(name)).name.casefold() in _catalog()


def lora_documentation(name):
    """Return a fresh object: callers cannot alter the catalog for other users."""
    row = _catalog().get(Path(str(name)).name.casefold())
    if row is not None:
        return deepcopy(row)
    return {
        "title": Path(str(name)).name,
        "summary": "An installed LoRA adapter. A publisher description has not been identified for this file yet.",
        "trigger": "Unknown. An empty trigger field does not mean no trigger is needed. Check the publisher's README or your saved profile.",
        "trigger_words": "",
        "usage": ["Select a compatible base model, enable this LoRA, and use the publisher's instructions. The filename alone cannot establish its purpose or compatibility."],
        "settings": [], "caveats": ["Publisher settings and a README link are not yet available for this file."],
        "sources": [], "coverage": "unidentified", "verified_on": "",
    }
