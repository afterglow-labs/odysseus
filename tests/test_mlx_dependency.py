"""Linux MLX installs must include the backend used by the selected Python."""

from importlib.metadata import PackageNotFoundError

import pytest

from src.dependency_catalog import dependency_catalog, requirement_specs
from src.dependency_health import check_dependency_health
from src.mlx_dependency import mlx_lm_recipe


def versions(values):
    def version(name):
        if name not in values:
            raise PackageNotFoundError(name)
        return values[name]
    return version


@pytest.mark.parametrize("installed,backend", [
    ({"mlx-cuda-13": "0.32.2", "cuda-toolkit": "12.9"}, "cuda13"),
    ({"mlx-cuda-12": "0.32.2", "cuda-toolkit": "13.0"}, "cuda12"),
    ({"mlx-cpu": "0.32.2", "nvidia-cuda-runtime": "13.0"}, "cpu"),
    ({"cuda-toolkit": "13.0.3.0"}, "cuda13"),
    ({"nvidia-cuda-runtime": "13.0.96"}, "cuda13"),
    ({"nvidia-cuda-runtime": "12.9.79"}, "cuda12"),
    ({"nvidia-cuda-runtime-cu13": "13.0.96"}, "cuda13"),
    ({"nvidia-cublas-cu12": "12.9.1"}, "cuda12"),
    ({}, "cuda12"),
])
def test_linux_recipe_matches_selected_environment(installed, backend):
    recipe = mlx_lm_recipe("linux", version_fn=versions(installed))
    assert recipe["pip"] == f"mlx-lm[{backend}]>=0.32.2"
    assert recipe["mlx_backend"] == backend
    assert recipe["install_hint"]


@pytest.mark.parametrize("platform", ["darwin", "macos", "mac"])
def test_mac_recipe_does_not_inspect_local_or_remote_cuda_metadata(platform):
    def unexpected(name):
        pytest.fail(f"Mac recipe must not inspect CUDA metadata: {name}")
    assert mlx_lm_recipe(platform, version_fn=unexpected) == {"pip": "mlx-lm"}


def test_catalog_linux_default_has_backend_without_reading_app_environment(monkeypatch):
    monkeypatch.setattr("importlib.metadata.version", lambda name: pytest.fail("catalogue has no target metadata"))
    rows = dependency_catalog(local_platform="darwin", target_platform="linux")
    row = next(row for row in rows if row["name"] == "mlx_lm")
    assert requirement_specs(row) == ["mlx-lm[cuda12]>=0.32.2"]
    assert "No backend was detected" in row["install_hint"]
    assert "Linux" in row["desc"]


@pytest.mark.parametrize("installed_mlx,missing,requirement", [
    (False, "mlx", "mlx[cuda13]>=0.32.2"),
    (True, "mlx-cuda-13", "mlx-cuda-13==0.32.2"),
])
def test_repair_follows_linux_extra_to_missing_runtime(installed_mlx, missing, requirement):
    installed = {"mlx-lm": "0.32.2", "nvidia-cuda-runtime": "13.0.96"}
    if installed_mlx:
        installed["mlx"] = "0.32.2"
    requires = {
        "mlx-lm": [
            'mlx>=0.32.2; platform_system == "Darwin"',
            'mlx[cuda13]>=0.32.2; extra == "cuda13"',
            'mlx[cuda12]>=0.32.2; extra == "cuda12"',
        ],
        "mlx": ['mlx-cuda-13==0.32.2; platform_system == "Linux" and extra == "cuda13"'],
    }
    recipe = mlx_lm_recipe("linux", version_fn=versions(installed))
    health = check_dependency_health(
        [recipe["pip"]], version_fn=versions(installed),
        requires_fn=lambda name: requires.get(name, []),
        environment={"platform_system": "Linux"},
    )
    assert health["installed"] is False
    assert [(issue["name"], issue["requirement"]) for issue in health["issues"]] == [(missing, requirement)]


def test_older_lm_without_backend_extras_requires_upgrade_before_claiming_ready():
    recipe = mlx_lm_recipe("linux", version_fn=versions({"mlx-lm": "0.31.0"}))
    health = check_dependency_health(
        [recipe["pip"]], version_fn=versions({"mlx-lm": "0.31.0"}), requires_fn=lambda name: [],
    )
    assert health["conflicts"][0]["requirement"] == "mlx-lm[cuda12]>=0.32.2"
