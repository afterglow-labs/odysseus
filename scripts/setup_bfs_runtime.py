#!/usr/bin/env python3
"""Prepare pinned BFS inference helpers in Odysseus's private environment."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name('bfs_runtime_sources.json')


def setup(runtime=None, *, install=True):
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Run with Odysseus venv/bin/python; global Python is not modified')
    try:
        from .setup_h3_runtime import setup as setup_core
    except ImportError:
        from setup_h3_runtime import setup as setup_core
    core = Path(runtime or PROJECT / 'runtimes/minimax-h3/ComfyUI')
    setup_core(core, install=install)
    source = json.loads(MANIFEST.read_text())
    destination = core.parent / 'bfs_nodes'
    destination.mkdir(parents=True, exist_ok=True)
    import httpx
    for name, expected in source['files'].items():
        path = destination / name
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f'Existing BFS helper differs from its pin; preserve or move it before setup: {path}')
            continue
        url = f'https://raw.githubusercontent.com/alisson-anjos/ComfyUI-BFSNodes/{source["revision"]}/{name}'
        with httpx.stream('GET', url, follow_redirects=True, timeout=30) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 2 * 1024 * 1024:
                    raise RuntimeError('BFS helper exceeds the expected source size')
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError('BFS helper checksum mismatch: ' + name)
        path.write_bytes(content)
    # Import only reviewed helpers, never the upstream custom-node registration.
    package = destination / '__init__.py'
    initializer = '"""Selected unmodified BFSNodes helpers. See UPSTREAM.json and LICENSE."""'
    if not package.exists():
        package.write_text(initializer + '\n')
    elif package.read_text().strip() not in {'', initializer}:
        raise RuntimeError('BFS helper package contains an unexpected initializer')
    (destination / 'UPSTREAM.json').write_text(json.dumps(source, indent=2) + '\n')
    print(f'BFS helpers ready: {destination}\nRevision: {source["revision"]}\nModel weights are selected from Cookbook caches.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--skip-dependencies', action='store_true')
    args = parser.parse_args()
    setup(args.runtime, install=not args.skip_dependencies)
