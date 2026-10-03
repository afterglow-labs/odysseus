from src.dependency_catalog import dependency_catalog, requirement_specs


def row(name, local="darwin", target="linux"):
    return next(p for p in dependency_catalog(local_platform=local, target_platform=target) if p["name"] == name)


def test_app_background_removal_uses_local_mac_runtime_with_remote_selected():
    assert row("rembg")["pip"] == "rembg[cpu]"
    assert requirement_specs(row("rembg")) == ["rembg[cpu]"]


def test_linux_serving_recipes_are_not_offered_as_native_mac_or_windows_installs():
    for platform in ("darwin", "win32", "windows"):
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
