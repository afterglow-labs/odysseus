#!/usr/bin/env python3
"""Install a pinned optional H3 preset's weights with the official hf CLI."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

try:
    from scripts.h3_seed_hunter_presets import TURBO4, TURBO8, PIKA, LIGHTVAE, PRISM8, CHARACTER_SWAP, CHARACTER_TURBO
except ModuleNotFoundError:
    from h3_seed_hunter_presets import TURBO4, TURBO8, PIKA, LIGHTVAE, PRISM8, CHARACTER_SWAP, CHARACTER_TURBO

ASSETS = {'turbo4': {TURBO4}, 'turbo8': {TURBO8}, 'pika': {PIKA}, 'lightvae': {LIGHTVAE},
          'prism8': {PRISM8}, 'character_swap': {CHARACTER_SWAP, CHARACTER_TURBO}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preset', choices=['all', *ASSETS], required=True)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    wanted = set.union(*ASSETS.values()) if args.preset == 'all' else ASSETS[args.preset]
    manifest = json.loads((Path(__file__).with_name('h3_seed_hunter_preset_models.json')).read_text())
    cli = shutil.which('hf')
    for row in manifest['weights']:
        if Path(row['filename']).name not in wanted:
            continue
        target = root / 'models' / row['repository'] / row['filename']
        if not args.verify_only:
            if not cli:
                parser.error('Install the Hugging Face hf CLI before downloading presets')
            subprocess.run([cli, 'download', row['repository'], row['filename'], '--revision', row['revision'],
                            '--local-dir', str(root / 'models' / row['repository']), '--quiet'], check=True)
        if not target.is_file() or target.stat().st_size != row['bytes']:
            raise SystemExit(f'Missing or incomplete preset weight: {row["filename"]}')
        with target.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != row['sha256']:
            raise SystemExit(f'Checksum mismatch: {row["filename"]}')
        print(f'Verified {row["filename"]}', flush=True)


if __name__ == '__main__':
    main()
