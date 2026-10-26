"""Keep an edit's original audio bitstream, independently of model generation."""
import os
from pathlib import Path
import subprocess


def preserve_soundtrack(manifest, output, ffmpeg):
    if not manifest.get('result_edit', {}).get('preserve_audio'):
        return
    output = Path(output)
    inputs = manifest.get('reference_videos', [])
    source = Path(inputs[0]) if inputs else None
    if source is None or source.is_symlink() or source.resolve().parent != output.resolve().parent or not source.is_file():
        raise ValueError('The saved original soundtrack source is unavailable')
    temporary = output.with_name(output.stem + '-original-audio.mp4')
    result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-y', '-i', str(output), '-i', str(source),
                             '-map', '0:v:0', '-map', '1:a:0?', '-c', 'copy', '-movflags', '+faststart', str(temporary)],
                            capture_output=True, timeout=60)
    if result.returncode:
        temporary.unlink(missing_ok=True)
        raise ValueError('Could not copy the original soundtrack into the edited video')
    os.replace(temporary, output)
