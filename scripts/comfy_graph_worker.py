#!/usr/bin/env python3
"""Run installed Comfy Python classes headlessly, without a Comfy server/UI.

The pinned execution core retains validation, hidden inputs, list broadcasting,
lazy inputs, expansion and IS_CHANGED semantics. Only package receipts approved
by the server supply code paths. Each invocation owns its media/output folders.
"""
import argparse
import asyncio
import json
import mimetypes
import os
from pathlib import Path
import shutil
import sys
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def atomic(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, allow_nan=False))
    os.replace(temporary, path)


def run(manifest):
    config = manifest['config']
    root = Path(manifest['output_path']).parent
    input_dir, output_dir, temp_dir = (root / name for name in ('comfy-input','comfy-output','comfy-temp'))
    for folder in (input_dir, output_dir, temp_dir):
        folder.mkdir(mode=0o700, exist_ok=True)
    progress_path = Path(manifest['status_path'])
    def progress(phase, **more):
        atomic(progress_path, {'phase':phase,'step':more.pop('step',0),'total_steps':len(config['prompt']),**more})
    class Events:
        client_id = 'odysseus'
        last_node_id = None
        sockets_metadata = {}
        sockets = {}
        error = None
        count = 0
        def send_sync(self, event, data, sid=None):
            if (root / 'cancel').exists():
                import nodes
                nodes.interrupt_processing(True)
            if event == 'executing' and isinstance(data,dict) and data.get('node'):
                self.count += 1
                progress('executing_node',step=self.count,node=data['node'])
            elif event == 'execution_error':
                self.error = data.get('exception_message','Node execution failed')
            elif event == 'execution_interrupted':
                self.error = 'Workflow stopped'
        def queue_updated(self):
            pass
    events = Events()
    progress('loading_node_packages')
    from src.comfy_node_manifest import verify_source
    packages = config['package_manifests']
    for package in packages:
        verify_source(package)
    runtime = Path(manifest['runtime_path'])
    sys.path.insert(0, str(runtime))
    import comfy.options
    comfy.options.enable_args_parsing(False)
    from comfy.cli_args import args
    args.cpu = config['gpu'] == 'cpu'
    args.enable_assets = False
    if args.cpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = ''
    else:
        os.environ['CUDA_VISIBLE_DEVICES'] = config['gpu']
        # Keep H3's tested Blackwell allocator and bounded-kernel setup.
        from scripts.h3_video_worker import H3Runtime
        H3Runtime(runtime)
    import folder_paths
    folder_paths.set_input_directory(str(input_dir))
    folder_paths.set_output_directory(str(output_dir))
    folder_paths.set_temp_directory(str(temp_dir))
    # Model discovery is scoped to Odysseus's configured library. No custom
    # package is asked to install dependencies or download arbitrary weights.
    from src.h3_video import discover_components, cache_roots
    categories = {'model':'diffusion_models','encoder':'text_encoders','video_vae':'vae',
                  'audio_vae':'vae','lora':'loras','latent_upscaler':'latent_upscale_models'}
    for category in ('checkpoints','diffusion_models','text_encoders','vae','loras','vae_approx',
                     'latent_upscale_models','controlnet','clip_vision','embeddings','upscale_models'):
        directory=PROJECT/'models'/category
        if directory.is_dir():
            folder_paths.add_model_folder_path(category,str(directory))
    for component in discover_components(cache_roots(PROJECT)):
        category = categories.get(component.get('role'))
        if category:
            folder_paths.add_model_folder_path(category, str(Path(component['path']).parent))
    import nodes
    # Some packages declare aiohttp routes while importing their classes.
    # Give them a private registration context without constructing or starting
    # a Comfy web server. These package routes are never exposed by Odysseus.
    from aiohttp import web
    import server
    events.routes = web.RouteTableDef()
    server.PromptServer.instance = events
    async def load():
        from scripts.comfy_node_discovery import load_package, node_mapping
        for package in packages:
            source = package.get('source') or package.get('package') or {}
            path = source.get('package_path') or source.get('source_path')
            if not path:
                raise ValueError('This manifest has no installed Python package')
            if Path(path).resolve() == (runtime / 'nodes.py').resolve():
                continue
            # Use exactly the entrypoint/namespace/V3 lifecycle used at
            # discovery, including directories whose entrypoint is nodes.py.
            module, entry = load_package(Path(path))
            registered, _ = await node_mapping(module)
            exported = dict(registered)
            for declared in package['nodes']:
                name = declared['class_type']
                cls = exported.get(name)
                if not isinstance(cls,type) or not callable(getattr(cls,declared.get('function') or 'execute',None)):
                    raise ValueError('Installed node no longer provides its declared Python function: '+name)
                cls.RELATIVE_PYTHON_MODULE = module.__name__
                nodes.NODE_CLASS_MAPPINGS[name] = cls
            display = getattr(module,'NODE_DISPLAY_NAME_MAPPINGS',{})
            if isinstance(display,dict):
                nodes.NODE_DISPLAY_NAME_MAPPINGS.update(display)
    asyncio.run(load())
    prompt = json.loads(json.dumps(config['prompt']))
    external = config.get('external_inputs', {})
    for index, (identity, ports) in enumerate(external.items()):
        for port_index, (port, descriptor) in enumerate(ports.items()):
            paths = manifest.get(descriptor['binding'], [])
            if not isinstance(paths, list) or len(paths) != 1:
                raise ValueError('A saved workflow input is missing')
            path = Path(paths[0])
            if path.is_symlink() or path.resolve().parent != root.resolve() or not path.is_file():
                raise ValueError('Input file is outside this job')
            name = f'input-{index}-{port_index}{path.suffix.lower()}'
            shutil.copyfile(path, input_dir / name)
            kind = descriptor['kind']
            if kind == 'FILE':
                prompt[identity]['inputs'][port] = name
                continue
            class_name = f'OdysseusOwnedInput{index}_{port_index}'
            bridge_id = f'odysseus_input_{index}_{port_index}'
            while bridge_id in prompt:
                bridge_id += '_owned'
            while class_name in nodes.NODE_CLASS_MAPPINGS:
                class_name += 'Owned'
            def make_loader(kind, filename):
                class OwnedInput:
                    RETURN_TYPES = (kind,)
                    FUNCTION = 'load'
                    CATEGORY = 'Odysseus inputs'
                    @classmethod
                    def INPUT_TYPES(cls):
                        return {'required': {}}
                    def load(self):
                        if kind == 'IMAGE':
                            return (nodes.LoadImage().load_image(filename)[0],)
                        if kind == 'VIDEO':
                            from comfy_api.input_impl import VideoFromFile
                            return (VideoFromFile(str(input_dir / filename)),)
                        from comfy_extras.nodes_audio import LoadAudio
                        result = LoadAudio.execute(filename)
                        return (result[0],)
                return OwnedInput
            nodes.NODE_CLASS_MAPPINGS[class_name] = make_loader(kind, name)
            prompt[bridge_id] = {'class_type':class_name,'inputs':{}}
            prompt[identity]['inputs'][port] = [bridge_id,0]
    # Prevent an imported prefix from redirecting stock output nodes outside
    # this job. Custom packages themselves remain trusted installed Python.
    for node in prompt.values():
        inputs = node['inputs']
        for key in ('filename_prefix','output_path','output_directory','save_path'):
            value = inputs.get(key)
            normalized=value.replace('\\','/') if isinstance(value,str) else ''
            if isinstance(value,str) and (Path(normalized).is_absolute() or '..' in Path(normalized).parts or len(normalized)>1 and normalized[1]==':'):
                raise ValueError('Output paths must stay inside this workflow job')
        if node['class_type']=='MiniMaxH3SeedScoutSampler' and inputs.get('mode','interactive') not in ('scout','final'):
            raise ValueError('Use the native Seed Scout node for interactive selection in Odysseus')
    import execution
    valid, error, outputs, node_errors = asyncio.run(execution.validate_prompt(root.name, prompt, config['output_nodes']))
    if not valid:
        raise ValueError('Node validation failed: '+json.dumps({'error':error,'nodes':node_errors}, default=str)[:6000])

    from app.assets.manager import NoAssets
    engine = execution.PromptExecutor(events, cache_type=execution.CacheType.CLASSIC,
        cache_args={'ram':0,'ram_inactive':0,'lru':0}, asset_manager=NoAssets(args))
    engine.execute(prompt,root.name,{'client_id':'odysseus'},outputs)
    if not engine.success or events.error:
        raise RuntimeError(events.error or 'Node execution failed; see the job log')
    artifacts = []
    for path in sorted(output_dir.rglob('*')):
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(output_dir.resolve()):
            name = str(path.relative_to(output_dir))
            artifacts.append({'name':name,'size':path.stat().st_size,
                              'mime_type':mimetypes.guess_type(name)[0] or 'application/octet-stream'})
    # Outputs may legitimately be text/UI data, so the receipt itself is a
    # useful result even when no media files were emitted.
    receipt = {'version':1,'status':'completed','artifacts':artifacts,'output_nodes':outputs,
               'ui':engine.history_result.get('outputs',{})}
    atomic(root / 'comfy-result.json', receipt)
    temporary = Path(manifest['output_path']).with_suffix('.tmp.zip')
    with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
        archive.write(root / 'comfy-result.json','result.json')
        for item in artifacts:
            archive.write(output_dir / item['name'],'outputs/'+item['name'])
    os.replace(temporary,manifest['output_path'])
    progress('completed',step=len(config['prompt']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job',required=True,type=Path)
    options = parser.parse_args()
    manifest = json.loads(options.job.read_text())
    try:
        run(manifest)
    except BaseException as exc:
        from src.h3_video import _redact
        atomic(Path(manifest['status_path']),{'phase':'failed','error':_redact(str(exc))[:8000]})
        raise
