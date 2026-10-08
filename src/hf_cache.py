"""Hugging Face cache paths without importing ML or Hub packages.

This module is also embedded in Cookbook's remote cache scanner and probes.
"""

import os


def effective_hf_cache(environ=None):
    """Match huggingface_hub's cache environment precedence."""
    env = os.environ if environ is None else environ
    cache = env.get("HF_HUB_CACHE") or env.get("HUGGINGFACE_HUB_CACHE")
    if not cache:
        home = env.get("HF_HOME")
        if not home:
            home = os.path.join(env.get("XDG_CACHE_HOME") or "~/.cache", "huggingface")
        cache = os.path.join(home, "hub")
    return os.path.abspath(os.path.expanduser(cache))


def download_cache_paths(directory=None, environ=None):
    """Return (HF_HOME, hub) for a local/remote download destination.

    Existing parent-directory settings keep their <directory>/hub layout.
    A hub directory itself is used directly, never nested as hub/hub.
    Explicit remote paths keep '~' for expansion on the selected host.
    """
    if not directory:
        cache = effective_hf_cache(environ)
        env = os.environ if environ is None else environ
        home = env.get("HF_HOME") or os.path.dirname(cache)
        return os.path.abspath(os.path.expanduser(home)), cache
    path = str(directory).replace("\\", "/").rstrip("/") or "/"
    if path.rsplit("/", 1)[-1].lower() == "hub":
        return path.rsplit("/", 1)[0] or "/", path
    return path, path.rstrip("/") + "/hub"


def normalize_local_cache_settings(environment):
    """Replace the old browser-inserted default, preserving explicit paths.

    Apply on both read and write so an already-open client cannot restore the
    obsolete tilde path after the server has moved to its private cache.
    Remote server paths belong to that host and are never changed here.
    """
    if not isinstance(environment, dict):
        return
    servers = environment.get("servers")
    if not isinstance(servers, list):
        return
    cache = effective_hf_cache()

    def canonical(value):
        if isinstance(value, str) and value.strip().rstrip("/") == "~/.cache/huggingface/hub":
            return cache
        return value

    for server in servers:
        if not isinstance(server, dict) or str(server.get("host") or "").strip():
            continue
        for key in ("modelDir", "downloadDir"):
            if key in server:
                server[key] = canonical(server[key])
        # Read/write old-client profiles with their migrated download target.
        # Scan roots remain valid compatibility locations and are preserved.
        if server.get("downloadDir"):
            from src.model_library import migrated_library_root
            server["downloadDir"] = migrated_library_root(server["downloadDir"])
        if isinstance(server.get("modelDirs"), list):
            directories = []
            seen = set()
            for value in server["modelDirs"]:
                value = canonical(value)
                if isinstance(value, str):
                    key = value.strip().rstrip("/")
                    if key in seen:
                        continue
                    seen.add(key)
                directories.append(value)
            server["modelDirs"] = directories
