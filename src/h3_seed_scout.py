"""Owner-scoped selection and preview artifacts for the split-schedule scout."""
from src.h3_video import _read, _write, _locked


def summary(directory, state=None):
    data = _read(directory / "scout.json")
    if not data:
        return None
    result = {key: data.get(key) for key in ("version", "mode", "status", "seeds", "scout_step", "total_steps", "selected_seed")}
    result["previews"] = []
    seeds = data.get("seeds", [])
    for row in data.get("previews", []):
        seed = row.get("seed")
        if type(seed) is not int or seed not in seeds:
            continue
        path = directory / f"scout-{seed}.webp"
        if path.is_file() and not path.is_symlink():
            result["previews"].append({"seed": seed, "provisional": bool(row.get("provisional")),
                "url": f"/api/video/h3/jobs/{directory.name}/scout/previews/{seed}", "mime_type": "image/webp"})
    # Worker termination may occur before it can update the scout receipt.
    if state and state.get("status") in {"stopped", "failed"}:
        result["status"] = state["status"]
    selected = _read(directory / "scout-selection.json").get("seed")
    if type(selected) is int:
        result["selected_seed"] = selected
        if result["status"] == "waiting_for_selection":
            result["status"] = "continuing"
    return result


def detail(manager, identity, owner):
    state = manager._state(identity, owner)
    value = summary(manager.directory(identity), state)
    if value is None:
        raise FileNotFoundError("SeedScout previews are not available yet")
    return value


def artifact(manager, identity, owner, seed):
    value = detail(manager, identity, owner)
    if type(seed) is not int or seed not in [row["seed"] for row in value["previews"]]:
        raise FileNotFoundError("Seed preview not found")
    path = manager.directory(identity) / f"scout-{seed}.webp"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Seed preview not found")
    return path


def select(manager, identity, owner, seed):
    # Check ownership before taking the same job lock used by cancellation.
    manager._state(identity, owner)
    directory = manager.directory(identity)
    if type(seed) is not int:
        raise ValueError("Choose a seed number from this job")
    with _locked(directory / "job.lock"):
        state = _read(directory / "state.json")
        if state.get("owner") != owner:
            raise FileNotFoundError("Video job not found")
        current = _read(directory / "scout-selection.json")
        if current:
            if current.get("seed") == seed:
                return summary(directory, state)
            raise RuntimeError("A seed was already chosen for this job")
        data = _read(directory / "scout.json")
        if state.get("status") != "running" or data.get("status") != "waiting_for_selection" or (directory / "cancel").exists():
            raise RuntimeError("This job is not waiting for a seed selection")
        if seed not in data.get("seeds", []):
            raise ValueError("Choose one of this job's scouted seeds")
        _write(directory / "scout-selection.json", {"version": 1, "seed": seed})
        return summary(directory, state)
