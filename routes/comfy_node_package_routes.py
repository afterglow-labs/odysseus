"""Explicit administration of installed node schemas, separate from JSON import."""
import asyncio
import hashlib
from pathlib import Path

from fastapi import HTTPException, Request
from src.h3_video import PROJECT_ROOT, RUNTIME_ROOT, _write, discover_components, cache_roots
from src.comfy_node_manifest import discover_node_package, convert_object_info, load_manifests, MANIFEST_DIRECTORY


def candidates(project=PROJECT_ROOT, runtime=RUNTIME_ROOT):
    project, runtime = Path(project), Path(runtime)
    roots = [project / 'custom_nodes', runtime / 'custom_nodes', project / 'runtimes/comfy-packages/sources',
             project.parent / 'custom_nodes', runtime.parent / 'seed-hunter']
    paths = []
    for root in roots:
        if root.is_dir():
            paths.extend(path for path in root.iterdir() if not path.name.startswith('.') and
                         (path.is_dir() and ((path / '__init__.py').is_file() or (path / 'nodes.py').is_file()) or path.suffix == '.py'))
    if (runtime / 'nodes.py').is_file():
        paths.append(runtime / 'nodes.py')
    if (runtime / 'comfy_extras').is_dir():
        paths.extend((runtime / 'comfy_extras').glob('nodes_*.py'))
    result = {}
    for path in paths:
        path = path.resolve()
        identity = hashlib.sha256(str(path).encode()).hexdigest()[:32]
        result[identity] = {'id':identity,'name':path.stem if path.is_file() else path.name,'path':path}
    return result


def public_manifest(value):
    # The node metadata itself can contain required model choices. Only private
    # interpreter/package paths and source file hashes are omitted here.
    return {key:val for key,val in value.items() if key not in {'package','raw_source','source'}} | {
        'source': {key:val for key,val in value.get('source',{}).items() if key in {'id','name','kind','fingerprint'}}}


def model_paths(project=PROJECT_ROOT):
    roles = {'model':'diffusion_models','encoder':'text_encoders','video_vae':'vae','audio_vae':'vae',
             'lora':'loras','latent_upscaler':'latent_upscale_models'}
    result = {}
    for category in ('checkpoints','diffusion_models','text_encoders','vae','loras','vae_approx','latent_upscale_models',
                     'controlnet','clip_vision','embeddings','upscale_models'):
        directory = Path(project) / 'models' / category
        if directory.is_dir():
            result.setdefault(category,set()).add(str(directory.resolve()))
    for component in discover_components(cache_roots(project)):
        category = roles.get(component.get('role'))
        if category:
            result.setdefault(category,set()).add(str(Path(component['path']).parent))
    return {key:sorted(value) for key,value in result.items()}


def add_comfy_node_package_routes(router, owner_callback, body_callback, *, directory=MANIFEST_DIRECTORY):
    lock = asyncio.Lock()
    @router.get('/packages')
    async def packages(request: Request):
        owner_callback(request)
        installed, available = await asyncio.gather(asyncio.to_thread(load_manifests,directory),asyncio.to_thread(candidates))
        return {'packages':[public_manifest(row) for row in installed],
                'candidates':[{key:row[key] for key in ('id','name')} for row in available.values()]}

    @router.post('/packages/discover')
    async def discover(request: Request):
        owner_callback(request)
        try:
            body = await body_callback(request)
        except (ValueError, TypeError, RecursionError) as exc:
            raise HTTPException(400,'Supply a node package selection as JSON') from exc
        if set(body) != {'id'} or not isinstance(body['id'],str):
            raise HTTPException(400,'Choose a local node package')
        available = await asyncio.to_thread(candidates)
        row = available.get(body['id'])
        if not row:
            raise HTTPException(404,'The selected node package is no longer available')
        async with lock:
            try:
                value = await asyncio.to_thread(discover_node_package,row['path'],model_paths=await asyncio.to_thread(model_paths))
                identity = value.get('package_id') or value.get('source',{}).get('id')
                if not identity:
                    raise ValueError('Package discovery did not return an identity')
                Path(directory).mkdir(parents=True,exist_ok=True,mode=0o700)
                await asyncio.to_thread(_write,Path(directory)/(identity+'.json'),value)
                return public_manifest(value)
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from exc

    @router.post('/packages/object-info')
    async def object_info(request: Request):
        owner_callback(request)
        try:
            body = await body_callback(request)
        except (ValueError, TypeError, RecursionError) as exc:
            raise HTTPException(400,'Supply exported /object_info JSON') from exc
        if set(body) != {'object_info'}:
            raise HTTPException(400,'Supply exported /object_info JSON')
        try:
            value = convert_object_info(body['object_info'])
            identity = 'object-info-'+value['source']['fingerprint'][:24]
            value['package_id'] = identity
            for node in value['nodes']:
                node['package_id'] = identity
            Path(directory).mkdir(parents=True,exist_ok=True,mode=0o700)
            await asyncio.to_thread(_write,Path(directory)/(identity+'.json'),value)
            return public_manifest(value)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
