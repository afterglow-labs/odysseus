#!/usr/bin/env bash
# Exercise the built image's real entrypoint, privilege drop and app startup.
# Only the disposable container created here is removed; no host paths mount.
set -euo pipefail

image="${1:?Usage: smoke-image.sh IMAGE}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container=""
cleanup() {
  status=$?
  if [ -n "$container" ]; then
    if [ "$status" -ne 0 ]; then
      docker logs --tail 160 "$container" >&2 || true
    fi
    docker rm -f "$container" >/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

container="$(docker run --detach \
  --publish 127.0.0.1::7000 \
  --env AUTH_ENABLED=false \
  --env ODYSSEUS_ADMIN_USER=smoke-admin \
  --env ODYSSEUS_ADMIN_PASSWORD=ci-smoke-only-not-a-credential \
  --env ODYSSEUS_STARTUP_WARMUPS=false \
  --env HF_HUB_OFFLINE=1 \
  "$image")"
address="$(docker port "$container" 7000/tcp)"
python "$root/scripts/smoke_runtime.py" --url "http://$address"
docker exec "$container" python /app/src/python_runtime.py
docker exec "$container" python -c 'import bcrypt, chromadb, cryptography, fastembed, magic, numpy'
docker exec "$container" python -c 'from pathlib import Path; uid = next(line for line in Path("/proc/1/status").read_text().splitlines() if line.startswith("Uid:")); assert uid.split()[1:] == ["1000"] * 4, uid'
docker exec --user 1000:1000 "$container" python -c 'import os; from pathlib import Path; assert os.getuid() == 1000; path = Path("/app/data/.smoke-write"); path.write_text("ok"); path.unlink()'
