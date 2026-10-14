"""Installed Comfy classes run privately in the existing shared job queue."""
import copy
import json
from pathlib import Path
import re

from src.h3_video import H3JobManager, JOB_ROOT, PROJECT_ROOT, RUNTIME_ROOT, gpu_inventory, _read

PACKAGE_ROOT = PROJECT_ROOT / "runtimes/comfy-packages/manifests"


def installed_manifests(root=PACKAGE_ROOT):
    values = []
    for path in sorted(Path(root).glob('*.json')):
        if not path.is_symlink():
            data = _read(path)
            if data:
                values.append(data)
    return values


def manifest_identity(manifest):
    return manifest.get('package_id') or manifest.get('id') or (manifest.get('package') or {}).get('id')


def manifest_fingerprint(manifest):
    return manifest.get('fingerprint') or (manifest.get('source') or manifest.get('package') or {}).get('fingerprint')


class ComfyJobManager(H3JobManager):
    output_basename = 'output.zip'

    def __init__(self, root=JOB_ROOT.with_name('comfy'), *, project=PROJECT_ROOT, runtime=RUNTIME_ROOT, manifests=PACKAGE_ROOT, **kwargs):
        super().__init__(root, project=project, runtime=runtime,
                         worker=Path(project) / 'scripts/comfy_graph_worker.py', **kwargs)
        self.manifests = Path(manifests)

    def inventory(self):
        return {'runtime_ready': (self.runtime / 'execution.py').is_file(), 'gpus': gpu_inventory(),
                'components': [], 'packages': [{'id':manifest_identity(m), 'fingerprint':manifest_fingerprint(m)}
                    for m in installed_manifests(self.manifests)]}

    def validate_config(self, raw, uploads):
        if not isinstance(raw, dict) or set(raw) - {'prompt','output_nodes','packages','external_inputs','gpu'}:
            raise ValueError('Invalid installed-node workflow settings')
        prompt, outputs = raw.get('prompt'), raw.get('output_nodes')
        if not isinstance(prompt, dict) or not 1 <= len(prompt) <= 300 or not isinstance(outputs, list) or not outputs:
            raise ValueError('Choose at least one installed output node')
        try:
            if len(json.dumps(raw,allow_nan=False)) > 12*1024*1024:
                raise ValueError('Workflow settings exceed 12 MB')
        except (TypeError,RecursionError) as error:
            raise ValueError('Workflow settings must be JSON values') from error
        if not isinstance(uploads,dict):
            raise ValueError('Invalid workflow media bindings')
        available = {manifest_identity(m):m for m in installed_manifests(self.manifests)
                     if isinstance(manifest_identity(m),str) and m.get('status') in {'ready','partial'}}
        requested = raw.get('packages')
        if not isinstance(requested, list) or not requested or len(requested) > 100:
            raise ValueError('Installed node package receipts are required')
        manifests, classes, package_ids = [], {}, set()
        for package in requested:
            if (not isinstance(package,dict) or set(package) != {'id','fingerprint'}
                    or not isinstance(package['id'],str) or not 1 <= len(package['id']) <= 240
                    or not isinstance(package['fingerprint'],str) or not re.fullmatch(r'[a-f0-9]{64}',package['fingerprint'])
                    or package['id'] in package_ids):
                raise ValueError('Invalid node package receipt')
            package_ids.add(package['id'])
            item = available.get(package.get('id'))
            if not item or manifest_fingerprint(item) != package.get('fingerprint'):
                raise ValueError('A node package changed or is not installed. Refresh its manifest and check the workflow.')
            from src.comfy_node_manifest import verify_source
            verify_source(item)
            manifests.append(item)
            for node in item.get('nodes', []):
                key = node.get('class_type') or node.get('type_id')
                if key in classes:
                    raise ValueError('Two installed packages export the same node type: '+str(key))
                classes[key] = node
        for identity, node in prompt.items():
            if (not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',identity)
                    or not isinstance(node,dict) or set(node) != {'class_type','inputs'}
                    or not isinstance(node.get('class_type'),str) or node['class_type'] not in classes):
                raise ValueError('Every executable node must match an installed class manifest')
            if not isinstance(node.get('inputs'), dict):
                raise ValueError('Node inputs must be an object')
        if any(not isinstance(identity,str) or identity not in prompt or not classes[prompt[identity]['class_type']].get('output_node') for identity in outputs):
            raise ValueError('Choose installed terminal nodes as workflow outputs')
        external = raw.get('external_inputs', {})
        if not isinstance(external, dict):
            raise ValueError('Invalid external inputs')
        referenced = set()
        for identity, ports in external.items():
            if identity not in prompt or not isinstance(ports, dict):
                raise ValueError('Invalid external node input')
            declared = {p.get('name'):p for p in classes[prompt[identity]['class_type']].get('inputs', [])}
            for name, descriptor in ports.items():
                if name not in declared or not isinstance(descriptor, dict):
                    raise ValueError('Unknown external input port')
                if descriptor.get('kind') not in {'IMAGE','VIDEO','AUDIO','FILE'}:
                    raise ValueError('Unsupported external input type')
                port=declared[name];kind=descriptor['kind'];metadata=port.get('metadata') or {}
                if port.get('section')=='hidden':
                    raise ValueError('Hidden node inputs are supplied by the execution engine')
                if kind=='FILE':
                    media_kind=descriptor.get('media_kind')
                    if media_kind not in {'image','video','audio'} or not metadata.get(media_kind+'_upload'):
                        raise ValueError('This file input needs an installed media adapter')
                elif port.get('type') != kind:
                    raise ValueError('The attached media does not match the node input type')
                binding = descriptor.get('binding')
                if (not isinstance(binding,str) or not re.fullmatch(r'asset_[0-9]+',binding)
                        or not isinstance(uploads.get(binding),list) or len(uploads[binding]) != 1):
                    raise ValueError('Attach exactly one file to each installed-node media input')
                referenced.add(binding)
                if name in prompt[identity]['inputs']:
                    raise ValueError('A media input cannot have both a file binding and an inline value')
        if set(uploads) != referenced:
            raise ValueError('Workflow media must match the declared external inputs')
        for identity,node in prompt.items():
            declared={p.get('name'):p for p in classes[node['class_type']].get('inputs',[])}
            if set(node['inputs']) - set(declared):
                raise ValueError('The node contains an input that is missing from its installed schema')
            for name,port in declared.items():
                if port.get('section')=='hidden' and name in node['inputs']:
                    raise ValueError('Hidden node inputs are supplied by the execution engine')
                metadata=port.get('metadata') or {}
                if any(metadata.get(kind+'_upload') for kind in ('image','video','audio')) and name not in external.get(identity,{}):
                    raise ValueError('Use the media picker for this installed file loader')
                if (port.get('is_widget') and not (port.get('type')=='COMBO' and name.lower()=='model_path')
                        and not any(metadata.get(kind+'_upload') for kind in ('image','video','audio'))
                        and re.search(r'load|read|input',node['class_type'],re.I)
                        and (name.lower() in {'file','filename','filepath','path','directory','folder','image','video','audio'}
                             or name.lower().endswith(('_path','_file','_directory','_folder')))):
                    raise ValueError('This filesystem loader needs an Odysseus media adapter')
            if node['class_type']=='MiniMaxH3SeedScoutSampler' and node['inputs'].get('mode','interactive') not in ('scout','final'):
                raise ValueError('Use the native Seed Scout node for interactive selection in Odysseus')
        device = raw.get('gpu', 'cpu')
        if not isinstance(device,str) or device != 'cpu' and not any(g['id']==device for g in self.submission_inventory()['gpus']):
            raise ValueError('Select an available GPU or CPU for the imported nodes')
        # Only server installation records provide executable Python paths.
        config = copy.deepcopy(raw)
        config.update(recipe='comfy_graph',gpu=device,steps=len(prompt),package_manifests=manifests)
        return config

    def view(self, identity, owner, **kwargs):
        data = super().view(identity, owner, **kwargs)
        data['recipe'] = 'comfy_graph'
        data['url'] = None
        data['filename'] = identity + '.zip'
        receipt = _read(self.directory(identity) / 'comfy-result.json')
        data['artifacts'] = [{key:row.get(key) for key in ('name','size','mime_type')} for row in receipt.get('artifacts', [])]
        return data

    def video_path(self, identity, owner):
        state = self._state(identity, owner)
        path = self.directory(identity) / self.output_basename
        if state.get('status') != 'completed' or not path.is_file() or path.is_symlink():
            raise FileNotFoundError('Completed workflow output not found')
        return path, identity + '.zip'
