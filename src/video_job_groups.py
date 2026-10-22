"""Present Hunter refinements with their source without moving any artifacts."""
from src import h3_video as h3


def refinement_groups(manager, owner, records=None):
    if records is None:
        records = {}
        for directory in manager.root.iterdir() if manager.root.is_dir() else ():
            if not h3.JOB_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            state = h3._read(directory / "state.json")
            if state.get("owner") == owner:
                records[directory.name] = (state, h3._read(directory / "manifest.json"))
    parents, children = {}, {}
    for identifier, (state, manifest) in records.items():
        config = manifest.get("config", {})
        source = manifest.get("seed_hunter_source", {})
        if (state.get("owner") != owner or not isinstance(config, dict) or not isinstance(source, dict)
                or config.get("recipe") != "seed_hunter" or config.get("seed_hunter", {}).get("stage") != "final"):
            continue
        parent, candidate = source.get("source_job_id"), source.get("source_candidate")
        if not isinstance(parent, str) or parent == identifier or type(candidate) is not int or not 1 <= candidate <= 3:
            continue
        parent_state, parent_manifest = records.get(parent, ({}, {}))
        parent_config = parent_manifest.get("config", {})
        if (parent_state.get("owner") != owner or not isinstance(parent_config, dict)
                or parent_config.get("recipe") != "seed_hunter"
                or parent_config.get("seed_hunter", {}).get("stage") != "preview"):
            continue
        from src.h3_seed_hunter import artifact
        try:
            artifact(manager.directory(parent), candidate, "video")
        except (ValueError, FileNotFoundError):
            # A damaged or removed preview must never hide its surviving final.
            continue
        parents[identifier] = parent
        children.setdefault(parent, []).append(identifier)
    for identifiers in children.values():
        identifiers.sort(key=lambda identifier: (records[identifier][0].get("created_at", 0), identifier))
    return parents, children


def attach_refinements(manager, owner, job, children, **view_options):
    job["refinements"] = []
    for identifier in children.get(job["id"], ()):
        try:
            job["refinements"].append(manager.view(identifier, owner, **view_options))
        except FileNotFoundError:
            pass
    return job


def detail(manager, identifier, owner):
    job = manager.view(identifier, owner)
    if job.get("seed_hunter", {}).get("stage") not in {"preview", "final"}:
        return job
    parents, children = refinement_groups(manager, owner)
    if identifier in parents:
        job["refinement_parent"] = parents[identifier]
    return attach_refinements(manager, owner, job, children)
