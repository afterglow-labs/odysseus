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


class SamplingProgressGuider:
    """Keep the core's preview callback while reporting each completed update."""
    def __init__(self, guider, on_step):
        self.guider = guider
        self.on_step = on_step

    def __getattr__(self, name):
        return getattr(self.guider, name)

    def sample(self, *args, **kwargs):
        callback = kwargs.get("callback")

        def report(step, *values):
            if callback is not None:
                callback(step, *values)
            self.on_step(step + 1)

        return self.guider.sample(*args, **{**kwargs, "callback": report})


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


def sample(runtime, manifest, config, model, positive, latent, vae, progress, base_positive=None):
    if __package__:
        from .h3_seed_hunter import _worker, save_snapshot
    else:
        from h3_seed_hunter import _worker, save_snapshot
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
    def sample_core(guider, selected_sampler, sigmas, latent, latent_samples, noise, seed):
        continuing = state["status"] == "continuing"
        full_pass = options["scout_mode"] == "final"
        phase = "continuing_selected_seed" if continuing else "sampling" if full_pass else "scouting_seeds"
        offset = options["scout_step"] if continuing else 0
        updates = int(sigmas.shape[-1]) - 1

        def report(step):
            progress(phase, step, total_steps=updates, seed=seed,
                     schedule_step=offset + step, schedule_total_steps=config["steps"],
                     remaining_steps=max(0, updates - step),
                     candidate=seeds.index(seed) + 1 if seed in seeds else 1,
                     candidate_count=1 if full_pass else len(seeds))

        report(0)
        try:
            result = core_sample(SamplingProgressGuider(guider, report), selected_sampler,
                                 sigmas, latent, latent_samples, noise, seed)
            if not continuing and not full_pass:
                save_snapshot(folder / f"scout-{seed}.safetensors",
                    latent={**result[0], "_scout_sigmas": full_sigmas[options["scout_step"]:],
                            "_scout_step": options["scout_step"]},
                    positive={"sampling": positive, "refinement": base_positive if base_positive is not None else positive},
                    config={**config, "seed": seed}, seed=seed)
            return result
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
            progress("scouting_seeds", options["scout_step"], total_steps=options["scout_step"],
                     seed=payload["seed"], candidate=payload.get("index", 0) + 1, candidate_count=len(seeds))
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
            progress(status, options["scout_step"], total_steps=options["scout_step"],
                     candidate_count=len(candidates))
        chosen = wait_for_selection(folder, candidates, timeout, check_cancel=check_cancel, notify=notify)
        state.update(status="continuing", selected_seed=chosen)
        publish()
        progress("continuing_selected_seed", 0, total_steps=config["steps"] - options["scout_step"],
                 seed=chosen, schedule_step=options["scout_step"], schedule_total_steps=config["steps"],
                 remaining_steps=config["steps"] - options["scout_step"])
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
    full_sigmas = sigmas
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
        progress.total_steps = options["scout_step"] if options["scout_mode"] == "scout" else config["steps"]
        progress.details["seed"] = chosen
        progress("sampling_complete", progress.total_steps)
        return output, denoised, chosen
    except BaseException as exc:
        state.update(status="stopped" if (folder / "cancel").exists() else "failed", error=str(exc)[:2000])
        publish()
        raise


def resume(runtime, manifest, config, model, positive, latent, vae, progress, base_positive=None):
    """Continue the saved noisy partial through its exact remaining sigmas."""
    import comfy.samplers
    from comfy_extras.nodes_custom_sampler import BasicGuider
    sigmas = latent.pop("_scout_sigmas")
    offset = latent.pop("_scout_step")
    total = len(sigmas) - 1
    if offset + total != config["steps"] or total < 1:
        raise ValueError("Saved Scout schedule does not match this job")
    def report(step):
        progress("continuing_selected_seed", step, total_steps=total, seed=config["seed"],
                 schedule_step=offset + step, schedule_total_steps=config["steps"], remaining_steps=total-step)
    runtime.release_conditioning()
    guider = SamplingProgressGuider(BasicGuider.execute(model, positive)[0], report)
    sampler = load_source(manifest["runtime_path"]).MiniMaxH3SeedScoutSampler()
    report(0)
    try:
        output, denoised, _ = sampler._sample_continue(guider, comfy.samplers.sampler_object(config["sampler"]),
                                                       sigmas, latent, latent["samples"], config["seed"])
    finally:
        runtime.cleanup_model_state()
    return output, denoised if denoised is not None else output, config["seed"]
