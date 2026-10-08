import pytest
from packaging.requirements import Requirement

from src.dependency_catalog import dependency_catalog, requirement_specs, unsupported_windows_requirements


def row(name, local="darwin", target="linux"):
    return next(p for p in dependency_catalog(local_platform=local, target_platform=target) if p["name"] == name)


def test_app_background_removal_uses_local_mac_runtime_with_remote_selected():
    assert row("rembg")["pip"] == "rembg[cpu]"
    assert requirement_specs(row("rembg")) == ["rembg[cpu]"]


def test_app_package_installer_can_be_updated_from_dependencies():
    pip = row("pip", local="win32", target="linux")
    assert pip["target"] == "local"
    assert requirement_specs(pip) == ["pip"]


def test_realesrgan_declares_distutils_provider_for_runtime_repairs():
    assert "setuptools" in requirement_specs(row("realesrgan"))


def test_sglang_requires_current_wheel_based_release_without_obsolete_all_extra():
    requirements = requirement_specs(row("sglang", local="linux"))
    assert len(requirements) == 1
    requirement = Requirement(requirements[0])
    assert requirement.name == "sglang"
    assert not requirement.extras
    assert "0.5.21" in requirement.specifier
    assert "0.5.5.post2" not in requirement.specifier
    assert "0.5.4.post2" not in requirement.specifier


def test_linux_serving_recipes_are_not_offered_as_native_mac_installs():
    for platform in ("darwin", "macos", "mac"):
        for name in ("vllm", "sglang"):
            pkg = row(name, target=platform)
            assert not pkg["install_supported"]
            assert "Linux" in pkg["install_hint"]
    assert row("vllm", target="linux")["install_supported"]


def test_source_distributions_have_checkable_package_identities():
    assert requirement_specs(row("krea_diffusers"))[0] == "diffusers"
    assert requirement_specs(row("boogu_image_mlx"))[0] == "boogu-image-mlx"


def test_pipeline_plans_include_components_the_app_imports():
    for name in ("diffusers", "krea_diffusers"):
        assert "transformers" in requirement_specs(row(name))
    for name in ("mflux", "boogu_image_mlx"):
        assert {"fastapi", "uvicorn", "python-multipart"} <= set(requirement_specs(row(name)))


def test_catalog_changes_for_one_request_do_not_leak_into_another():
    first = row("rembg")
    first["pip"] = "wrong"
    assert row("rembg")["pip"] == "rembg[cpu]"
    assert row("rembg", local="linux")["pip"] == "rembg[gpu]"


@pytest.mark.parametrize("target", ["win32", "windows", "win", "Windows"])
def test_native_windows_catalog_contains_only_supported_dependencies(target):
    names = {pkg["name"] for pkg in dependency_catalog(local_platform="win32", target_platform=target)}
    assert not names.intersection({
        "tmux", "APFEL", "vllm", "sglang", "mlx_lm", "mlx_vlm", "mflux",
        "boogu_image_mlx", "mlx_lama_swift", "mlx_ddcolor_swift",
    })
    assert {
        "docker", "llama_cpp", "hf_transfer", "diffusers", "rembg",
        "realesrgan", "sam_mask", "playwright",
    } <= names


@pytest.mark.parametrize("target", ["linux", "darwin", "macos"])
def test_windows_client_keeps_tmux_for_supported_remote_targets(target):
    assert row("tmux", local="win32", target=target)["install_supported"] is True


def test_platform_filter_uses_each_dependency_execution_target():
    mac_server = {p["name"] for p in dependency_catalog(local_platform="win32", target_platform="darwin")}
    assert {"mlx_lm", "mlx_vlm", "mflux", "tmux"} <= mac_server
    assert "APFEL" not in mac_server  # This feature always runs inside the local app.
    windows_server = {p["name"] for p in dependency_catalog(local_platform="darwin", target_platform="windows")}
    assert "APFEL" in windows_server
    assert not windows_server.intersection({"mlx_lm", "vllm", "sglang", "tmux"})


def test_windows_client_keeps_linux_engines_for_linux_server():
    for name in ("vllm", "sglang"):
        assert row(name, local="win32", target="linux")["install_supported"] is True


def test_windows_install_guard_matches_projects_without_blocking_index_urls():
    assert unsupported_windows_requirements([
        "--extra-index-url", "https://example.org/wheels/mlx", "mlx-helper",
        "llama-cpp-python[server]", "diffusers[torch]", "rembg[gpu]",
    ]) == []
    assert unsupported_windows_requirements([
        "MLX_LM>=0.32", "sglang[all]", "mlx[cpu]", "mlx-vlm==0.1",
        "git+https://github.com/xocialize/boogu-image-mlx.git",
    ]) == ["boogu-image-mlx", "mlx", "mlx-lm", "mlx-vlm", "sglang"]
