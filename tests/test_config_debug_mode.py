import pytest
from pydantic import ValidationError

from src.config import AppConfig


@pytest.mark.parametrize("value,expected", [
    ("release", False), ("Release", False), ("debug", True),
    ("true", True), ("false", False), ("1", True), ("0", False),
])
def test_debug_accepts_native_build_modes_and_booleans(monkeypatch, value, expected):
    monkeypatch.setenv("DEBUG", value)
    assert AppConfig().debug is expected


def test_unknown_debug_value_is_still_an_error(monkeypatch):
    monkeypatch.setenv("DEBUG", "typo")
    with pytest.raises(ValidationError):
        AppConfig()
