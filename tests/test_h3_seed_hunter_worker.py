"""Seed Hunter CPU regressions: artifacts, sampling, stage isolation and media."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
import torch

from scripts import h3_seed_hunter as hunter, h3_video_worker as worker
from scripts.h3_seed_hunter_controls import validate_options


@pytest.mark.parametrize("chunks", [0, 1, 8])
@pytest.mark.parametrize("sink,expected", [("none", "off"), ("exact_kv_and_rows", "exact_kv_and_rows")])
def test_patch_model_preserves_explicit_chunks_and_translates_sparse_sink(monkeypatch, chunks, sink, expected):
    calls = []
    mlp = SimpleNamespace()
    model = SimpleNamespace(get_model_object=lambda _: SimpleNamespace(blocks=[SimpleNamespace(mlp=mlp)]))
    backend = ModuleType("comfy_extras.nodes_model_advanced")
    backend.ModelAttentionBackend = SimpleNamespace(execute=lambda value, *_: (value,))
    sparse = ModuleType("comfy_extras.nodes_sparse_attention")
    sparse.BlockSparseAttention = SimpleNamespace(execute=lambda value, *args, **kw: (calls.append(kw) or value,))
    monkeypatch.setitem(sys.modules, "comfy_extras.nodes_model_advanced", backend)
    monkeypatch.setitem(sys.modules, "comfy_extras.nodes_sparse_attention", sparse)
    chunk_calls = []
    optimizations = SimpleNamespace(MiniMaxChunkFeedForward=SimpleNamespace(
        execute=lambda value, count, threshold: (chunk_calls.append((count, threshold)) or value,)))
    monkeypatch.setattr(hunter, "_load_module", lambda *_: optimizations)
    options = validate_options({"ff_chunk_size": chunks, "sparse_method": "sla", "sparse_sink_conditioning": sink})
    assert hunter.patch_model(None, {"seed_hunter": options}, model, "/tmp/ComfyUI") is model
    assert getattr(mlp, "_odysseus_manual_ff_chunks", 0) == chunks
    assert chunk_calls == ([(chunks, options["ff_chunk_threshold"])] if chunks else [])
    assert calls[0]["sink_conditioning"] == expected


@pytest.fixture
def manifest(tmp_path):
    config = dict(recipe="seed_hunter", mode="ref2va", prompt="A ball bounces.",
                  width=480, height=864, frames=73, steps=6, seed=42, gpu="0",
                  sampler="er_sde", scheduler="beta", reference_size="max", seed_hunter={})
    for key in ("model", "encoder", "video_vae", "audio_vae", "latent_upscaler"):
        path = tmp_path / f"{key}.safetensors"
        path.touch()
        config[key] = str(path)
    return dict(config=config, uploads={}, output_path=str(tmp_path / "output.mp4"),
                runtime_path=str(tmp_path / "ComfyUI"), status_path=str(tmp_path / "status.json"))


def test_recipe_scopes_text_only_ref2va_extended_samplers_and_canvas(manifest):
    config, media = worker.validate_job(manifest)
    assert config["frames"] == 73 and media["reference_mods"] == []
    assert config["seed_hunter"]["preview_count"] == 3
    manifest["config"]["seed_hunter"] = {"stage": "single"}
    manifest["config"].update(width=1088, height=1952)
    config, _ = worker.validate_job(manifest)
    assert config["seed_hunter"]["preview_count"] == 1
    manifest["config"].pop("recipe")
    with pytest.raises(ValueError, match="pixels"):
        worker.validate_job(manifest)


@pytest.mark.parametrize("option,match", [({"force_audio": True}, "audio attachment"),
    ({"first_frame_guide": True}, "needs an image"), ({"last_frame_guide": True}, "needs an image"),
    ({"stage": "continuation"}, "source video"), ({"stage": "final"}, "snapshot")])
def test_missing_required_recipe_inputs_fail_before_runtime(manifest, option, match):
    manifest["config"]["seed_hunter"] = option
    with pytest.raises(ValueError, match=match):
        worker.run_job(manifest, runtime_factory=lambda *_: pytest.fail("GPU must not initialize"))


def test_preview_seed_offsets_remain_exact_on_mobile(manifest):
    manifest["config"]["seed"] = 2**53 - 2000
    with pytest.raises(ValueError, match="53-bit"):
        worker.validate_job(manifest)


class Nested:
    is_nested = True
    def __init__(self, tensors):
        self.tensors = list(tensors)
    def unbind(self):
        return self.tensors


def test_snapshot_roundtrip_preserves_av_masks_and_reference_conditioning(manifest, tmp_path):
    config, _ = worker.validate_job(manifest)
    video = torch.arange(24.).reshape(1, 24, 1, 1, 1)
    audio = torch.ones((1, 32, 2, 8))
    latent = {"samples": Nested([video, audio]), "noise_mask": Nested([torch.ones_like(video), torch.zeros_like(audio)])}
    positive = [[torch.ones((1, 3, 7)), {"minimax_refs": [{"video": video, "nested": (None, 1.5, True)}]}]]
    path = tmp_path / "candidate-1.safetensors"
    hunter.save_snapshot(path, latent=latent, positive=positive, config=config, seed=42)
    spec = dict(latent=str(path), metadata=str(path.with_suffix(".json")))
    restored_positive, restored = hunter.load_snapshot(spec, {**config, "width": 960, "height": 1728}, nested_factory=Nested)
    assert torch.equal(restored["samples"].unbind()[0], video)
    assert torch.count_nonzero(restored["noise_mask"].unbind()[1]) == 0
    assert restored_positive[0][1]["minimax_refs"][0]["nested"] == (None, 1.5, True)
    with pytest.raises(ValueError, match="prompt differs"):
        hunter.load_snapshot(spec, {**config, "prompt": "Different"}, nested_factory=Nested)
    with pytest.raises(ValueError, match="cannot reduce"):
        hunter.load_snapshot(spec, {**config, "width": 256}, nested_factory=Nested)
    assert not list(tmp_path.glob("*.tmp*"))


def test_snapshot_rejects_objects_and_untrusted_node_tree(tmp_path):
    with pytest.raises(ValueError, match="Unsupported"):
        hunter._encode_tree(object(), {})
    with pytest.raises(ValueError, match="Invalid"):
        hunter._decode_tree({"type": "execute", "module": "os"}, {})
    with pytest.raises(ValueError, match="Invalid"):
        hunter._decode_tree({"type": "tensor", "key": "missing"}, {})


def test_three_previews_load_once_and_persist_x0_instead_of_decoded_output(manifest, monkeypatch):
    calls, decoded = [], []
    class Runtime:
        def __init__(self, *_):
            assert os.environ["CUDA_VISIBLE_DEVICES"] == "0"
        def load(self, *_):
            calls.append("load")
            return (object(),) * 4
        def decode(self, samples, *_):
            decoded.append(samples["samples"].item())
            return torch.zeros((73, 4, 4, 3)), {"waveform": torch.zeros((1, 2, 20)), "sample_rate": 32000}
        def cleanup_model_state(self):
            pass
    monkeypatch.setattr(hunter, "patch_model", lambda _r, _c, model, _path: model)
    monkeypatch.setattr(hunter, "condition", lambda *_: ([[torch.ones(1), {}]], {"samples": torch.zeros(1)}, [[torch.ones(1), {"base": True}]]))
    def sample(_r, config, *_):
        calls.append(config["seed"])
        return {"samples": torch.tensor(float(config["seed"]))}, {"samples": torch.tensor(float(config["seed"] + 10))}
    monkeypatch.setattr(hunter, "sample_candidate", sample)
    worker.run_job(manifest, runtime_factory=Runtime, media_loader=lambda *_: {},
                   muxer=lambda path, *_: Path(path).write_bytes(b"mp4"))
    assert calls == ["load", 42, 1042, 2042]
    assert decoded == [42., 1042., 2042.]
    folder = Path(manifest["output_path"]).parent
    rows = json.loads((folder / "candidates.json").read_text())["candidates"]
    assert [row["status"] for row in rows] == ["completed"] * 3
    config, _ = worker.validate_job(manifest)
    positive, latent = hunter.load_snapshot({"latent": str(folder / rows[1]["latent"]), "metadata": str(folder / rows[1]["metadata"])}, config)
    assert latent["samples"].item() == 1052 and positive[0][1]["base"]
    assert Path(manifest["output_path"]).read_bytes() == b"mp4"
    assert json.loads(Path(manifest["status_path"]).read_text())["phase"] == "completed"


def test_final_uses_saved_conditioning_without_reencoding_or_reloading_media(manifest, monkeypatch, tmp_path):
    config, _ = worker.validate_job(manifest)
    candidate = tmp_path / "original.safetensors"
    hunter.save_snapshot(candidate, latent={"samples": torch.tensor(11.)}, positive=[[torch.tensor(7.), {}]], config=config, seed=42)
    manifest["seed_hunter_input"] = {"latent": str(candidate), "metadata": str(candidate.with_suffix(".json"))}
    manifest["config"].update(width=960, height=1728, seed=123, steps=2, scheduler="linear_quadratic", seed_hunter={"stage": "final"})
    calls = []
    class Runtime:
        def __init__(self, *_): pass
        def load(self, *_): return (object(),) * 4
        def release_conditioning(self): calls.append("release_encoder")
        def cleanup_model_state(self): pass
        def decode(self, samples, *_):
            assert samples["samples"].item() == 13
            return torch.zeros((73, 4, 4, 3)), {}
    monkeypatch.setattr(hunter, "patch_model", lambda _r, _c, model, _path: model)
    monkeypatch.setattr(hunter, "condition", lambda *_: pytest.fail("must preserve original conditioning"))
    def upscale(_m, _c, latent, _p):
        assert latent["samples"].item() == 11
        calls.append("upscale")
        return {"samples": torch.tensor(12.)}
    monkeypatch.setattr(hunter, "upscale_latent", upscale)
    def sample(_r, config, _m, positive, latent, _p):
        assert config["seed"] == 123 and config["steps"] == 2
        assert positive[0][0].item() == 7 and latent["samples"].item() == 12
        calls.append("sample")
        return {"samples": torch.tensor(999.)}, {"samples": torch.tensor(13.)}
    monkeypatch.setattr(hunter, "sample_candidate", sample)
    worker.run_job(manifest, runtime_factory=Runtime, media_loader=lambda *_: pytest.fail("no media reload"),
                   muxer=lambda path, *_: Path(path).write_bytes(b"mp4"))
    assert calls == ["release_encoder", "upscale", "sample"]


def test_edited_final_prompt_uses_original_context_and_latent_without_reencoding_vaes(manifest, monkeypatch, tmp_path):
    original, _ = worker.validate_job(manifest)
    refs = [{"kind": "video", "latent": torch.tensor(5.)}, {"kind": "refmod", "latent": torch.tensor(9.)}]
    candidate = tmp_path / 'original.safetensors'
    hunter.save_snapshot(candidate, latent={'samples': torch.tensor(11.), 'noise_mask': torch.tensor(0.)},
                         positive=[[torch.tensor(7.), {'minimax_refs': refs, 'pooled_output': torch.tensor(3.)}]],
                         config=original, seed=42)
    manifest['seed_hunter_input'] = {'latent': str(candidate), 'metadata': str(candidate.with_suffix('.json'))}
    snapshot = candidate.read_bytes(), candidate.with_suffix('.json').read_bytes()
    manifest['config'].update(width=960, height=1728, prompt='Make the ball green.', seed_hunter={'stage': 'final'})
    prepared = {'ref_images': {'ref_image_1': object()}, 'ref_videos': {'ref_video_1': object()},
                'ref_video_audios': {'ref_video_audio_1': object()}, 'ref_audios': {'ref_audio_1': object()}}
    calls = []
    def encode(**kwargs):
        calls.append('encode')
        assert 'vae' not in kwargs and 'audio_vae' not in kwargs
        assert kwargs['prompt'] == 'Make the ball green.'
        assert (kwargs['width'], kwargs['height'], kwargs['length']) == (480, 864, 73)
        for key, value in prepared.items():
            assert kwargs[key] is value
        return [[torch.tensor(8.), {'pooled_output': torch.tensor(4.)}]], {'samples': torch.tensor(999.)}
    class Runtime:
        def __init__(self, *_):
            self.h3 = SimpleNamespace(MiniMaxH3ReferenceToVideo=SimpleNamespace(execute=encode))
        def load(self, *_): return (object(),) * 4
        def release_conditioning(self): pass
        def cleanup_model_state(self): pass
        def decode(self, *_): return torch.zeros((73, 4, 4, 3)), {}
    def media_loader(_media, config):
        calls.append('media')
        assert config['width'] == original['width'] and config['frames'] == original['frames']
        return prepared
    def upscale(_manifest, _config, latent, _progress):
        calls.append('upscale')
        assert latent['samples'].item() == 11 and latent['noise_mask'].item() == 0
        return {**latent, 'samples': latent['samples'] + 1}
    def sample(_runtime, config, _model, positive, latent, _progress):
        calls.append('sample')
        assert config['prompt'] == 'Make the ball green.'
        assert positive[0][0].item() == 8 and positive[0][1]['pooled_output'].item() == 4
        assert [r['latent'].item() for r in positive[0][1]['minimax_refs']] == [5, 9]
        assert latent['samples'].item() == 12 and latent['noise_mask'].item() == 0
        return latent, latent
    monkeypatch.setattr(hunter, 'patch_model', lambda _r, _c, model, _path: model)
    monkeypatch.setattr(hunter, 'condition', lambda *_: pytest.fail('Do not replace the saved latent or VAE context'))
    monkeypatch.setattr(hunter, 'upscale_latent', upscale)
    monkeypatch.setattr(hunter, 'sample_candidate', sample)
    worker.run_job(manifest, runtime_factory=Runtime, media_loader=media_loader,
                   muxer=lambda path, *_: Path(path).write_bytes(b'mp4'))
    assert calls == ['media', 'encode', 'upscale', 'sample']
    assert (candidate.read_bytes(), candidate.with_suffix('.json').read_bytes()) == snapshot
    assert json.loads(Path(manifest['status_path']).read_text())['prompt_changed'] is True


@pytest.mark.parametrize('changed_prompt', [False, True])
@pytest.mark.parametrize('changed_lora', [False, True])
def test_longer_final_regenerates_only_selected_seed_at_original_settings_before_refinement(
        manifest, monkeypatch, tmp_path, changed_lora, changed_prompt):
    from copy import deepcopy
    original, _ = worker.validate_job(manifest)
    original['loras'] = []
    candidate = tmp_path / 'original.safetensors'
    hunter.save_snapshot(candidate, latent={'samples': torch.tensor(999.)},
                         positive=[[torch.tensor(999.), {}]], config=original, seed=1042)
    manifest['seed_hunter_input'] = {'latent': str(candidate), 'metadata': str(candidate.with_suffix('.json'))}
    manifest['config'].update(width=960, height=1728, frames=311, seed=123, steps=2,
                             scheduler='linear_quadratic', loras=[], seed_hunter={'stage': 'final'})
    if changed_prompt:
        manifest['config']['prompt'] = 'Make the ball green.'
    if changed_lora:
        adapter = tmp_path / 'refinement_lora.safetensors'
        adapter.touch()
        manifest['config']['loras'] = [{'path': str(adapter), 'strength': .75}]
    calls, loaded, sampled, statuses = [], [], [], []
    class Runtime:
        def __init__(self, *_):
            self.model_management = SimpleNamespace(
                unload_all_models=lambda: calls.append('unload_all'),
                soft_empty_cache=lambda: calls.append('empty_cache'))
        def load(self, config, _progress):
            calls.append('load')
            loaded.append(deepcopy(config))
            return (object(),) * 4
        def release_conditioning(self): pass
        def cleanup_model_state(self): pass
        def decode(self, samples, *_):
            assert samples['samples'].item() == 30
            return torch.zeros((311, 4, 4, 3)), {}
    def media_loader(_media, config):
        calls.append('media')
        assert config['frames'] == 311 and config['width'] == 480
        return {'full_reference': True}
    def condition(_runtime, config, prepared, *_):
        calls.append('condition')
        assert config['frames'] == 311 and prepared['full_reference']
        assert config['prompt'] == original['prompt']
        return [[torch.tensor(5.), {}]], {'samples': torch.tensor(0.)}, [[torch.tensor(6.), {}]]
    def reencode(_runtime, config, prepared, _clip, positive):
        calls.append('edited_prompt')
        assert config['prompt'] == 'Make the ball green.' and prepared['full_reference']
        assert config['width'] == original['width'] and config['frames'] == 311
        assert positive[0][0].item() == 6
        return [[torch.tensor(8.), {}]]
    def sample(_runtime, config, _model, positive, latent, progress):
        sampled.append(deepcopy(config))
        if len(sampled) == 1:
            assert config['seed'] == 1042  # Actual selected branch, not base 42 or edited 123.
            assert config['steps'] == 6 and config['scheduler'] == 'beta' and config['sampler'] == 'er_sde'
            assert config['width'] == 480 and config['frames'] == 311 and config['loras'] == []
            assert config['seed_hunter']['preview_count'] == 1 and config['seed_hunter']['stage'] == 'preview'
            assert positive[0][0].item() == 5
            progress('sampling', 1)
            statuses.append(json.loads(Path(manifest['status_path']).read_text()))
            return {'samples': torch.tensor(10.)}, {'samples': torch.tensor(20.)}
        assert config['seed'] == 123 and config['steps'] == 2 and config['scheduler'] == 'linear_quadratic'
        assert config['frames'] == 311 and config['width'] == 960
        assert positive[0][0].item() == (8 if changed_prompt else 6) and latent['samples'].item() == 21
        return {'samples': torch.tensor(29.)}, {'samples': torch.tensor(30.)}
    def upscale(_manifest, config, latent, _progress):
        calls.append('upscale')
        assert config['frames'] == 311 and latent['samples'].item() == 20
        return {'samples': torch.tensor(21.)}
    monkeypatch.setattr(hunter, 'patch_model', lambda _r, _c, m, _p: m)
    monkeypatch.setattr(hunter, 'condition', condition)
    monkeypatch.setattr(hunter, 'reencode_refinement_prompt', reencode)
    monkeypatch.setattr(hunter, 'sample_candidate', sample)
    monkeypatch.setattr(hunter, 'upscale_latent', upscale)
    worker.run_job(manifest, runtime_factory=Runtime, media_loader=media_loader,
                   muxer=lambda path, *_: Path(path).write_bytes(b'mp4'))
    assert len(sampled) == 2 and calls.count('condition') == 1
    assert calls.count('edited_prompt') == int(changed_prompt)
    assert len(loaded) == (2 if changed_lora else 1)
    if changed_lora:
        assert calls.index('unload_all') < calls.index('load', calls.index('load') + 1)
        assert loaded[1]['loras'][0]['strength'] == .75
    assert statuses[0]['phase'] == 'regenerating_full_length_candidate'
    assert statuses[0]['total_steps'] == 6 and statuses[0]['source_seed'] == 1042
    assert json.loads(Path(manifest['status_path']).read_text())['frames'] == 311


def test_continuation_overlap_is_graphs_constant_half_blend():
    source = torch.full((5, 2, 2, 3), .8)
    generated = torch.full((22, 2, 2, 3), .2)
    output = hunter.blend_continuation(source, generated, 5)
    assert torch.allclose(output[:5], torch.full_like(output[:5], .5))
    assert torch.equal(output[5:], generated[5:])
    assert len(output) == 22


def test_real_short_continuation_preserves_source_prefix_and_duration(manifest, tmp_path):
    import av
    source = tmp_path / "source.mp4"
    worker.mux_mp4(source, torch.zeros((73, 32, 32, 3)), {"waveform": torch.zeros((1, 2, 97334)), "sample_rate": 32000})
    config, _ = worker.validate_job(manifest)
    config.update(width=256, height=256, seed_hunter=validate_options({"stage": "continuation", "continuation_overlap": 22}))
    prepared = hunter.prepare_continuation(source, config, tmp_path)
    assert len(prepared["continuation_guide"]) == 22 and prepared["continuation_keep"] == 51
    output = tmp_path / "continued.mp4"
    frames = hunter.blend_continuation(prepared["continuation_guide"], torch.ones((73, 256, 256, 3)), 22)
    worker.mux_mp4(output, frames, {"waveform": torch.zeros((1, 2, 97334)), "sample_rate": 32000})
    hunter.join_continuation(prepared, output)
    with av.open(str(output)) as file:
        decoded = list(file.decode(video=0))
    assert len(decoded) == 124  # 73 source + 73 new - 22 overlap


def test_flac_audio_candidate_is_real_decodable_media(tmp_path):
    import av
    path = tmp_path / "candidate.flac"
    hunter.save_flac(path, {"waveform": torch.zeros((1, 2, 3200)), "sample_rate": 32000})
    with av.open(str(path)) as output:
        assert sum(frame.samples for frame in output.decode(audio=0)) == 3200


def test_missing_optional_runtime_fails_before_gpu(manifest):
    manifest["config"]["seed_hunter"] = {"output_fps": 48}
    with pytest.raises(ValueError, match="RIFE"):
        worker.run_job(manifest, runtime_factory=lambda *_: pytest.fail("No GPU work before dependency check"))


def test_advanced_sampler_keeps_noise_mask_and_saves_processed_x0(monkeypatch):
    import ast
    runtime_root = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI"
    if not runtime_root.is_dir():
        pytest.skip("Pinned runtime is not installed")
    # Run the installed core scheduler method with fake model sigma generation;
    # this verifies the exact 2-step/.4-denoise tail, without initializing CUDA.
    import types
    comfy = ModuleType("comfy")
    samplers = ModuleType("comfy.samplers")
    seen = {}
    def calculate(_model, scheduler, total_steps):
        seen["scheduler"] = (scheduler, total_steps)
        return torch.arange(total_steps + 1, dtype=torch.float32).flip(0)
    samplers.calculate_sigmas = calculate
    samplers.sampler_object = lambda name: name
    comfy.samplers = samplers
    nested = ModuleType("comfy.nested_tensor")
    nested.NestedTensor = Nested
    comfy.nested_tensor = nested
    custom = ModuleType("comfy_extras.nodes_custom_sampler")
    tree = ast.parse((runtime_root / "comfy_extras/nodes_custom_sampler.py").read_text())
    scheduler_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BasicScheduler")
    execute = next(node for node in scheduler_class.body if isinstance(node, ast.FunctionDef) and node.name == "execute")
    namespace = {"torch": torch, "comfy": comfy, "io": SimpleNamespace(NodeOutput=lambda *args: args)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name="Scheduler", bases=[], keywords=[], body=[execute], decorator_list=[])], type_ignores=[])), "scheduler-test", "exec"), namespace)
    custom.BasicScheduler = namespace["Scheduler"]
    expected_mask = torch.zeros(1)
    class Guider:
        model_patcher = SimpleNamespace(model=SimpleNamespace(process_latent_out=lambda x: x + 100))
        def sample(self, noise, latent, sampler, sigmas, **kwargs):
            assert sampler == "er_sde" and sigmas.tolist() == [2., 1., 0.]
            assert kwargs["denoise_mask"] is expected_mask
            kwargs["callback"](1, torch.tensor(7.), torch.tensor(8.), 2)
            return torch.tensor(9.)
    custom.BasicGuider = SimpleNamespace(execute=lambda *_: [Guider()])
    for name, value in {"comfy": comfy, "comfy.samplers": samplers, "comfy.nested_tensor": nested,
                        "comfy_extras": ModuleType("comfy_extras"), "comfy_extras.nodes_custom_sampler": custom}.items():
        monkeypatch.setitem(sys.modules, name, value)
    calls = []
    runtime = SimpleNamespace(release_conditioning=lambda: calls.append("release"),
        cleanup_model_state=lambda: calls.append("cleanup"), sample=SimpleNamespace(prepare_noise=lambda *_: torch.zeros(1)),
        model_management=SimpleNamespace(intermediate_device=lambda: "cpu"))
    config = dict(seed=12, steps=2, sampler="er_sde", scheduler="linear_quadratic", seed_hunter={"stage": "final", "denoise": .4})
    output, denoised = hunter.sample_candidate(runtime, config, SimpleNamespace(get_model_object=lambda _: object()), [],
        {"samples": torch.tensor(1.), "noise_mask": expected_mask}, lambda *_: None)
    assert seen["scheduler"] == ("linear_quadratic", 5)
    assert output["samples"].item() == 9 and denoised["samples"].item() == 107
    assert denoised["noise_mask"] is expected_mask and calls == ["release", "cleanup"]


def test_guides_do_not_replace_native_references_or_refinement_conditioning(monkeypatch):
    refs = {"ref_image_1": torch.ones((1, 3, 3, 3)), "ref_image_2": torch.zeros((1, 3, 3, 3))}
    original = [[torch.tensor(7), {"minimax_refs": refs}]]
    observed = []
    def reference(**kwargs):
        assert kwargs["ref_images"] is refs
        return original, {"samples": torch.tensor(1)}
    def guide(**kwargs):
        observed.append(kwargs)
        return [["guide", {"frame": kwargs["frame_idx"]}]]
    runtime = SimpleNamespace(h3=SimpleNamespace(MiniMaxH3ReferenceToVideo=SimpleNamespace(execute=reference),
                                                  MiniMaxH3AddGuide=SimpleNamespace(execute=guide)))
    config = dict(prompt="x", width=480, height=864, frames=73, reference_size="max",
                  seed_hunter=validate_options({"first_frame_guide": True, "last_frame_guide": True}))
    _, _, base_positive = hunter.condition(runtime, config, {"ref_images": refs}, None, None, None)
    assert base_positive is original
    assert [row["frame_idx"] for row in observed] == [0, -1]
    assert observed[0]["image"] is refs["ref_image_1"] and observed[1]["image"] is refs["ref_image_2"]


def test_actual_refmod_reader_accepts_bundle_and_keeps_native_conditioning(tmp_path):
    from safetensors.torch import save_file
    runtime_path = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI"
    if not (runtime_path.parent / "seed-hunter/refmods/core.py").is_file():
        pytest.skip("Pinned RefMod dependency is not installed")
    first = tmp_path / "image.safetensors"
    save_file({"latent": torch.ones((1, 24, 1, 4, 4))}, str(first), metadata={"refmod_meta": json.dumps({"kind": "image", "name": "character"})})
    bundle = tmp_path / "bundle.safetensors"
    save_file({"ref_0": torch.zeros((1, 32, 2, 7)), "ref_1": torch.ones((1, 24, 2, 4, 4))}, str(bundle),
        metadata={"refmod_meta": json.dumps({"_format_version": 5, "kind": "bundle", "members": [{"kind": "audio"}, {"kind": "video"}]})})
    blocks = hunter.load_refmods(str(runtime_path), [str(first), str(bundle)], [1, 1])
    assert [block["kind"] for block in blocks] == ["image", "audio", "video"]
    assert all(block["refmod"] for block in blocks)
    assert hunter.load_refmods(str(runtime_path), [str(first)], [0]) == []
    invalid = tmp_path / "not-refmod.safetensors"
    save_file({"weight": torch.zeros((2, 2))}, str(invalid))
    with pytest.raises(ValueError, match="not an encoded"):
        hunter.load_refmods(str(runtime_path), [str(invalid)], [])
    save_file({"latent": torch.zeros((1, 3, 1, 4, 4))}, str(invalid), metadata={"refmod_meta": json.dumps({"kind": "image"})})
    with pytest.raises(ValueError, match="layout"):
        hunter.load_refmods(str(runtime_path), [str(invalid)], [])


def test_streaming_interpolation_preserves_duration_and_audio(manifest, monkeypatch, tmp_path):
    import av
    path = tmp_path / "candidate.mp4"
    worker.mux_mp4(path, torch.zeros((17, 32, 32, 3)), {"waveform": torch.zeros((1, 2, round(17 / 24 * 32000))), "sample_rate": 32000})
    module = ModuleType("comfy_extras.nodes_frame_interpolation")
    module.FrameInterpolationModelLoader = SimpleNamespace(execute=lambda _: ["model"])
    batch_lengths = []
    def interpolate(_model, frames, _multiplier):
        batch_lengths.append(len(frames))
        result = torch.empty((len(frames) * 2 - 1, *frames.shape[1:]))
        result[::2] = frames
        result[1::2] = (frames[:-1] + frames[1:]) / 2
        return [result]
    module.FrameInterpolate = SimpleNamespace(execute=interpolate)
    folders = ModuleType("folder_paths")
    folders.folder_names_and_paths = {}
    monkeypatch.setitem(sys.modules, "folder_paths", folders)
    monkeypatch.setitem(sys.modules, "comfy_extras", ModuleType("comfy_extras"))
    monkeypatch.setitem(sys.modules, "comfy_extras.nodes_frame_interpolation", module)
    hunter.interpolate_file(manifest, path, lambda *_args, **_kwargs: None)
    with av.open(str(path)) as output:
        assert output.streams.video[0].average_rate == 48
        assert len(list(output.decode(video=0))) == 34
    with av.open(str(path)) as output:
        assert len(list(output.decode(audio=0))) > 0
    assert max(batch_lengths) <= 8 and len(batch_lengths) == 3


def test_trimmed_audio_uses_requested_time_window_and_rate(tmp_path):
    source = tmp_path / "audio-source.mp4"
    waveform = torch.cat((torch.zeros((1, 2, 32000)), torch.ones((1, 2, 32000)) * .2), dim=-1)
    worker.mux_mp4(source, torch.zeros((48, 32, 32, 3)), {"waveform": waveform, "sample_rate": 32000})
    result = hunter.read_trimmed_audio(source, 1.1, .5)
    assert result["sample_rate"] == 32000 and result["waveform"].shape == (1, 2, 16000)
    assert result["waveform"].mean().item() > .1
