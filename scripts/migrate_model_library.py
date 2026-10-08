#!/usr/bin/env python3
"""Plan and resume verified migration from HF caches into named model folders."""
import argparse
import fcntl
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.model_library_migration import build_plan, execute_plan, _atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[], help="Existing HF hub cache; repeat for aliases/backups")
    parser.add_argument("--linux-root", type=Path)
    parser.add_argument("--other-root", type=Path)
    parser.add_argument("--h3-jobs", type=Path, help="Saved H3 job directory, to identify extra component files")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Execute an already saved plan")
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.execute:
        plan = json.loads(args.plan.read_text())
        journal = args.journal or args.plan.with_suffix(".journal.json")
        with journal.with_suffix(journal.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = execute_plan(plan, journal, limit=args.limit,
                                  progress=lambda event: print(json.dumps(event), flush=True))
        print(json.dumps({"journal": str(journal), "completed": sum(row.get("status") == "complete" for row in result["files"].values())}))
        return
    if not args.root or not args.linux_root or not args.other_root:
        parser.error("Planning requires --root, --linux-root and --other-root")
    paths = []
    if args.h3_jobs:
        for manifest in args.h3_jobs.glob("*/manifest.json"):
            config = json.loads(manifest.read_text()).get("config", {})
            paths.extend(config[key] for key in ("model", "encoder", "text_encoder", "video_vae", "audio_vae", "lora") if config.get(key))
            paths.extend(row["path"] for row in config.get("loras", []) if isinstance(row, dict) and row.get("path"))
    plan = build_plan(args.root, args.linux_root, args.other_root, h3_paths=paths)
    _atomic_json(args.plan, plan)
    print(json.dumps({"plan": str(args.plan), **plan["summary"], "warnings": len(plan["warnings"]), "unmapped_files": len(plan["unmapped"])}))


if __name__ == "__main__":
    main()
