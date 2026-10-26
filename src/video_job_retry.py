"""Restart a failed/stopped receipt in place, without another history item."""
import os
import subprocess
import sys
import threading
import time

from src import h3_video as h3
from src.video_job_edit import revision


def retry_job(manager, owner, identifier, config, expected_revision, request_id, *, allow_completed=False):
    directory = manager.directory(identifier)
    state = h3._read(directory / 'state.json')
    if not state or state.get('owner') != owner:
        raise FileNotFoundError('Video job not found')
    with h3._locked(manager.root.parent / 'video-render.lock'), h3._locked(directory / 'job.lock'):
        state = h3._read(directory / 'state.json')
        manifest = h3._read(directory / 'manifest.json')
        if not state or state.get('owner') != owner:
            raise FileNotFoundError('Video job not found')
        # A lost response must never restart a second attempt, even if that
        # attempt has already completed or failed before the client reconnects.
        if state.get('retry_request_id') == request_id:
            return state
        # A second group action may have prepared before the first requeued
        # this item. Never restart or duplicate work that is already active.
        if allow_completed and state.get('status') in h3.ACTIVE:
            return state
        staged = manifest.get('retry_request_id') == request_id and revision(manifest) == expected_revision + 1
        if revision(manifest) != expected_revision and not staged:
            raise RuntimeError('A selected job was edited elsewhere. Refresh to load its latest settings.')
        allowed = h3.TERMINAL if allow_completed else {'failed', 'stopped'}
        if state.get('status') not in allowed:
            raise RuntimeError('This job is no longer finished. Refresh jobs.')
        if h3._live_record(state.get('supervisor')) or h3._owned_workers(directory):
            raise RuntimeError('The previous video process is still exiting. Retry once it has stopped.')
        from src.video_job_actions import ensure_parent_available
        ensure_parent_available(manager, manifest)
        if h3.shutil.disk_usage(manager.root).free < 512 * 1024 * 1024:
            raise RuntimeError('At least 512 MB of free space is required for a video render')
        previous = h3._read(manager.root.parent / 'video-queue.json').get('last_order', 0)
        order = max(time.time_ns(), previous + 1)
        h3._write(manager.root.parent / 'video-queue.json', {'last_order': order})
        env = os.environ.copy()
        env[h3.MARKER], env['PYTHONUNBUFFERED'] = directory.name, '1'
        # The replacement waits on job.lock. A spawn failure leaves the old
        # receipt intact; dispatch cannot observe a half-reset job.
        with (directory / 'render.log').open('ab') as output:
            process = h3.subprocess.Popen(
                [sys.executable, '-m', 'src.h3_video', '--supervise', str(directory / 'manifest.json')],
                cwd=manager.project, env=env, stdout=output, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
        replacement = h3._identity(process.pid)
        try:
            for name in ('cancel', 'progress.json', 'candidates.json', 'scout.json', 'scout-selection.json'):
                (directory / name).unlink(missing_ok=True)
            (directory / 'render.log').write_bytes(b'')
            # Keep every input, latent snapshot, batch membership and parent
            # link. Only the execution receipt and visible progress are reset.
            updated = {**manifest, 'config': config, 'revision': expected_revision + 1,
                       'retry_request_id': request_id}
            state = {key: state[key] for key in ('id', 'owner', 'filename', 'created_at', 'source_name', 'submission_id') if key in state}
            state.update(status='queued', phase='queued', error=None, queue_order=order,
                         retried_at=time.time(), retry_request_id=request_id, steps=config['steps'], recipe=config.get('recipe', 'h3'),
                         supervisor=replacement, supervisor_edit_version=1,
                         supervisor_control_version=h3.SUPERVISOR_CONTROL_VERSION)
            h3._write(directory / 'manifest.json', updated)
            h3._write(directory / 'state.json', state)
        except BaseException:
            h3._stop_supervisor(replacement)
            h3._write(directory / 'manifest.json', manifest)
            raise
        threading.Thread(target=process.wait, daemon=True).start()
    return state
