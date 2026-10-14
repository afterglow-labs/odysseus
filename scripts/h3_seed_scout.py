"""Adapt SeedScout's real split-schedule sampler to Odysseus selection IPC.

The upstream sampling implementation is retained. Its browser events and
headless auto-selection are replaced with owned job artifacts and explicit
selection, so viewing or disconnecting a client never chooses a seed.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import time
import types


def wait_for_selection(folder, seeds, timeout, *, check_cancel, notify, clock=time.monotonic, sleep=time.sleep):
    started = clock()
    notify("waiting_for_selection")
    while True:
        check_cancel()
        path = Path(folder) / "scout-selection.json"
        if path.is_file() and not path.is_symlink():
            try:
                value = json.loads(path.read_text())
                if type(value.get("seed")) is int and value["seed"] in seeds:
                    return value["seed"]
            except (ValueError, OSError):
                pass
        if timeout and clock() - started >= timeout:
            raise TimeoutError("Seed selection timed out. No seed was chosen automatically; rerun to scout again.")
        sleep(.25)


def load_source(runtime_path):
    path = Path(runtime_path).parent / "seed-scout/nodes.py"
    if not path.is_file():
        raise ValueError("SeedScout is not installed in this Odysseus runtime")
    spec = importlib.util.spec_from_file_location("odysseus_seed_scout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample(runtime, manifest, config, model, positive, latent, vae, progress):
    from scripts.h3_seed_hunter import _worker
    import comfy.samplers
    from comfy_extras.nodes_custom_sampler import BasicGuider, BasicScheduler
    from PIL import Image
    worker = _worker()
    options = config["seed_hunter"]
    if options["scout_step"] >= config["steps"] and options["scout_mode"] != "final":
        raise ValueError("Scout step must be before the end of the full schedule")
    folder = Path(manifest["output_path"]).parent
    source = load_source(manifest["runtime_path"])
    sampler = source.MiniMaxH3SeedScoutSampler()
    # Retain the same inference-boundary cleanup as Odysseus's normal sampler.
    # A preview decode can change the loaded model between scout and continue.
    core_sample = sampler._sample_core
    def sample_core(*args, **kwargs):
        try:
            return core_sample(*args, **kwargs)
        finally:
            runtime.cleanup_model_state()
    sampler._sample_core = sample_core
    decode_preview = source._vae_preview_images
    def vae_preview(*args, **kwargs):
        try:
            return decode_preview(*args, **kwargs)
        finally:
            runtime.cleanup_model_state()
    source._vae_preview_images = vae_preview
    seeds = [config["seed"] + index for index in range(options["scout_count"])]
    state = {"version": 1, "mode": options["scout_mode"], "status": "scouting", "seeds": seeds,
             "scout_step": options["scout_step"], "total_steps": config["steps"], "previews": []}

    def publish():
        worker.atomic_json(folder / "scout.json", state)

    def check_cancel():
        if (folder / "cancel").exists():
            raise comfy.model_management.InterruptProcessingException()
        comfy.model_management.throw_exception_if_processing_interrupted()

    def update_preview(seed, data, provisional=False):
        if seed not in seeds:
            return
        name = f"scout-{seed}.webp"
        temp = folder / (name + ".tmp")
        temp.write_bytes(data)
        os.replace(temp, folder / name)
        entry = {"seed": seed, "filename": name, "provisional": provisional}
        state["previews"] = [row for row in state["previews"] if row["seed"] != seed] + [entry]
        state["previews"].sort(key=lambda row: row["seed"])
        publish()

    def send(event, payload):
        if event == source.EVT_PREVIEW:
            update_preview(int(payload["seed"]), base64.b64decode(payload["image_b64"], validate=True), payload.get("provisional", False))
            progress("scouting_seeds", seed=payload["seed"], candidate=payload.get("index", 0) + 1,
                     candidate_count=len(seeds))
        elif event == source.EVT_DONE:
            state.update(status=payload["status"], selected_seed=payload.get("seed"))
            publish()
        return True

    def save_preview(images, _prefix, seed, fps):
        frames = [Image.fromarray((frame.cpu().numpy().clip(0, 1) * 255).astype("uint8")) for frame in images]
        if not frames:
            raise ValueError("The seed preview had no frames")
        name = f"scout-{seed}.webp"
        path = folder / name
        frames[0].save(path, format="WEBP", save_all=True, append_images=frames[1:],
                       duration=max(1, round(1000 / fps)), loop=0, quality=80)
        update_preview(seed, path.read_bytes())
        return {"filename": name, "subfolder": "", "type": "output"}, len(frames) > 1, str(path), "image/webp"

    def await_selection(_self, _node_id, candidates, _k, _total, timeout, _pushed):
        def notify(status):
            state["status"] = status
            publish()
            progress(status, options["scout_step"], candidate_count=len(candidates))
        chosen = wait_for_selection(folder, candidates, timeout, check_cancel=check_cancel, notify=notify)
        state.update(status="continuing", selected_seed=chosen)
        publish()
        progress("continuing_selected_seed", options["scout_step"], seed=chosen)
        return chosen, "selected in Odysseus"

    source._send = send
    source._save_preview = save_preview
    # The chosen preview mode is explicit. Do not discover unrelated tiny-VAEs
    # while producing provisional previews or import custom-node packages.
    source._get_tae_decoder = lambda *_args, **_kwargs: None
    sampler._await_selection = types.MethodType(await_selection, sampler)
    runtime.release_conditioning()
    publish()
    guider = BasicGuider.execute(model, positive)[0]
    sigmas = BasicScheduler.execute(model, config["scheduler"], config["steps"], 1.)[0]
    try:
        result = sampler.run(guider=guider, sampler=comfy.samplers.sampler_object(config["sampler"]), sigmas=sigmas,
            latent_image=latent, mode=options["scout_mode"], seed_start=config["seed"], seed_count=options["scout_count"],
            seed_stride=1, scout_step=options["scout_step"], selected_seed=config["seed"],
            preview_mode=options["scout_preview_mode"], preview_frames=min(config["frames"], 124),
            preview_fps=24, max_preview_side=512, filename_prefix="scout", selection_timeout=options["scout_timeout"],
            vae=vae, unique_id=folder.name)
        output, denoised, chosen, _report = result["result"]
        state.update(status="scouted" if options["scout_mode"] == "scout" else "completed", selected_seed=chosen)
        publish()
        return output, denoised, chosen
    except BaseException as exc:
        state.update(status="stopped" if (folder / "cancel").exists() else "failed", error=str(exc)[:2000])
        publish()
        raise
