#!/usr/bin/env python3
"""One private BFS render; curated adapters, no workflow JSON execution."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from scripts.h3_video_worker import H3Runtime, Progress, mux_mp4, read_image
from src.bfs_video import BY_ID, UPLOAD_EXTENSIONS


def validate_job(manifest):
    config = dict(manifest.get('config') or {})
    workflow_id = config.get('workflow_id')
    workflow = BY_ID.get(workflow_id) if isinstance(workflow_id, str) else None
    if not workflow or config.get('family') != workflow['family']:
        raise ValueError('Unknown or mismatched BFS workflow')
    if not isinstance(config.get('prompt'), str) or not config['prompt'].strip() or len(config['prompt']) > 16000:
        raise ValueError('A saved BFS prompt is required')
    if not re.fullmatch(r'(?:\d+|GPU-[0-9a-fA-F-]{36})', str(config.get('gpu', ''))):
        raise ValueError('Choose one NVIDIA GPU for BFS')
    for control in workflow['controls']:
        key = control['key']
        value = config.get(key, control['default'])
        if control['type'] == 'select':
            if value not in [o['value'] for o in control['options']]:
                raise ValueError('Invalid ' + key)
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not control['min'] <= value <= control['max'] or (control['step'] >= 1 and (value - control['min']) % control['step']):
            raise ValueError('Invalid ' + key)
        if control['type'] != 'select' and control['step'] >= 1:
            value = int(value)
        config[key] = value
    if config['width'] * config['height'] > 768 * 1344:
        raise ValueError('Output canvas too large')
    for spec in workflow['slots']:
        value = config.get(spec['key'])
        if not value and not spec['required']:
            continue
        path = Path(value or '')
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != '.safetensors':
            raise ValueError('Missing compatible ' + spec['label'])
    media = {}
    uploads = manifest.get('uploads', manifest)
    for key in UPLOAD_EXTENSIONS:
        value = uploads.get(key)
        requirement = workflow['source_requirements'].get(key, {})
        if value and not requirement or requirement.get('required') and not value:
            raise ValueError('Invalid ' + key + ' for this workflow')
        if value:
            path = Path(value)
            if not path.is_absolute() or not path.is_file() or path.suffix.lower() not in UPLOAD_EXTENSIONS[key]:
                raise ValueError('Missing valid ' + key)
            media[key] = str(path)
    for key in ('output_path', 'status_path', 'runtime_path'):
        if not Path(manifest.get(key, '')).is_absolute():
            raise ValueError(key + ' must be absolute')
    config['runtime_path'] = manifest['runtime_path']
    config['fps'] = workflow['fps']
    return config, media


def read_segment(path, config):
    """Decode only the selected interval; resample timestamps before float tensors."""
    import av
    import numpy as np
    import torch
    from PIL import Image, ImageOps

    fps, count, start = config['fps'], config['frames'], config['start_seconds']
    arrays, prior, prior_time, last_time, duration = [], None, 0.0, 0.0, 0.0
    with av.open(str(path)) as source:
        if not source.streams.video:
            raise ValueError('The uploaded video has no video stream')
        stream = source.streams.video[0]
        rate = float(stream.average_rate or fps)
        if not math.isfinite(rate) or rate <= 0:
            rate = fps
        origin = float((stream.start_time or 0) * stream.time_base)
        if start:
            source.seek(int((start + origin) / stream.time_base), stream=stream, backward=True)
        for index, frame in enumerate(source.decode(stream)):
            timestamp = (float(frame.time) - origin if frame.time is not None else start + index / rate) - start
            if timestamp < -1 / rate:
                continue
            picture = frame.to_image().convert('RGB')
            rotation = int(frame.rotation)
            if rotation:
                picture = picture.rotate(rotation, expand=True)
            fit = ImageOps.fit if config['resize_mode'] == 'crop' else ImageOps.pad
            picture = fit(picture, (config['width'], config['height']), method=Image.Resampling.LANCZOS)
            current = np.asarray(picture, dtype=np.uint8)
            while len(arrays) < count and len(arrays) / fps <= timestamp:
                target = len(arrays) / fps
                arrays.append(prior if prior is not None and target - prior_time < timestamp - target else current)
            prior, prior_time, last_time = current, timestamp, timestamp
            duration = float(frame.duration * frame.time_base) if frame.duration and frame.time_base else 1 / rate
            if len(arrays) >= count:
                break
        while prior is not None and len(arrays) < count and len(arrays) / fps < last_time + duration - 1e-6:
            arrays.append(prior)
    if not arrays:
        raise ValueError('No video frames were found at the selected start time')
    return torch.from_numpy(np.stack(arrays)).float().div_(255)


def read_source_audio(path, start, seconds):
    import av
    import numpy as np
    import torch

    rate = 32000
    result = np.zeros((2, round(seconds * rate)), dtype=np.float32)
    written = False
    with av.open(str(path)) as source:
        if not source.streams.audio:
            return None
        stream = source.streams.audio[0]
        timeline = source.streams.video[0] if source.streams.video else stream
        origin = float((timeline.start_time or 0) * timeline.time_base)
        if start:
            source.seek(int((start + origin) / stream.time_base), stream=stream, backward=True)
        resampler = av.AudioResampler(format='fltp', layout='stereo', rate=rate)
        fallback = start
        for frame in source.decode(stream):
            timestamp = float(frame.time) - origin if frame.time is not None else fallback
            fallback = timestamp + frame.samples / frame.sample_rate
            if timestamp >= start + seconds:
                break
            for chunk in resampler.resample(frame):
                time = float(chunk.time) - origin if chunk.time is not None else timestamp
                offset = round((time - start) * rate)
                samples = chunk.to_ndarray()
                begin, end = max(0, offset), min(result.shape[1], offset + samples.shape[1])
                if end > begin:
                    result[:, begin:end] = samples[:, begin - offset:end - offset]
                    written = True
    return {'waveform': torch.from_numpy(result).unsqueeze(0), 'sample_rate': rate} if written else None


def prepare_media(paths, config):
    workflow = BY_ID[config['workflow_id']]
    source = read_segment(paths['source_video'], config)
    usable = min(config['frames'], len(source))
    usable -= (usable - workflow['frame_min']) % workflow['frame_step']
    if usable < workflow['frame_min']:
        raise ValueError('Source segment is too short for the selected workflow')
    config['frames'] = usable
    media = {'source_video': source[:usable],
             'identity_image': read_image(paths['identity_image'], 1024 * 1024),
             'source_audio': read_source_audio(paths['source_video'], config['start_seconds'], usable / config['fps']) if config['audio_mode'] == 'source' else None}
    if paths.get('last_frame'):
        media['last_frame'] = read_image(paths['last_frame'], 1024 * 1024)
    if paths.get('mask_video'):
        mask = read_segment(paths['mask_video'], config)
        if len(mask) < usable:
            raise ValueError('The mask video must cover the entire selected source segment')
        media['mask_video'] = mask[:usable]
    return media


def run_job(manifest, *, runtime_factory=H3Runtime, media_loader=prepare_media, generator=None, muxer=mux_mp4):
    config, paths = validate_job(manifest)
    os.environ['CUDA_VISIBLE_DEVICES'] = str(config['gpu'])
    progress = Progress(manifest['status_path'], config['steps'])
    progress('initializing_runtime')
    runtime = runtime_factory(manifest['runtime_path'])
    progress('preparing_guide_video')
    media = media_loader(paths, config)
    if generator is None:
        if config['family'] == 'h3':
            from scripts.bfs_h3 import generate as generator
        else:
            from scripts.bfs_ltx_wan import generate as generator
    import torch
    with torch.inference_mode():
        frames, audio = generator(config, media, progress, runtime=runtime)
        if audio is None:
            audio = {'waveform': torch.zeros(1, 2, round(len(frames) / config['fps'] * 32000)), 'sample_rate': 32000}
        progress('encoding_video')
        muxer(manifest['output_path'], frames, audio, fps=config['fps'])
    progress('completed', config['steps'], frames=len(frames), fps=config['fps'], duration=len(frames) / config['fps'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.job.read_text())
    try:
        run_job(manifest)
    except Exception as exc:
        Progress(manifest['status_path'], manifest.get('config', {}).get('steps', 0))('failed', error=str(exc))
        raise


if __name__ == '__main__':
    main()
