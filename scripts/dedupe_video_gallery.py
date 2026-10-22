"""Replace byte-identical Gallery copies with hardlinks to completed job output."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.h3_video import H3JobManager, _read, _locked, JOB_ID
from src.video_job_storage import deduplicate_gallery_video


def deduplicate(manager, owner=None):
    from core.database import GalleryImage, SessionLocal
    saved, count = 0, 0
    for directory in manager.root.iterdir() if manager.root.exists() else ():
        if not JOB_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
            continue
        with _locked(directory / 'job.lock'):
            state = _read(directory / 'state.json')
            if (state.get('status') != 'completed' or state.get('filename') != directory.name + '.mp4'
                    or owner is not None and state.get('owner') != owner):
                continue
            with SessionLocal() as db:
                row = db.get(GalleryImage, directory.name)
                if row is None or not row.is_active or row.owner != state.get('owner') or row.filename != state['filename']:
                    continue
                amount = deduplicate_gallery_video(directory / 'output.mp4', manager.gallery_directory / row.filename)
            saved += amount
            count += bool(amount)
    return {'linked_videos': count, 'bytes_reclaimed': saved}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Verify identical contents and replace redundant Gallery files')
    parser.add_argument('--owner', help='Only deduplicate this owner’s saved videos')
    args = parser.parse_args()
    if not args.apply:
        parser.error('Use --apply to deduplicate existing completed video outputs')
    print(json.dumps(deduplicate(H3JobManager(), args.owner)))
