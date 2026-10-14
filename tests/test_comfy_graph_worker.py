"""Exercise real installed classes through the pinned headless executor on CPU."""
import json
import copy
from pathlib import Path
import subprocess
import zipfile

import pytest

from src.comfy_node_manifest import PROJECT, discover_node_package
from src.comfy_workflow import ComfyJobManager

SOURCE = '''
import json
from pathlib import Path
import folder_paths
from server import PromptServer
@PromptServer.instance.routes.get('/odysseus-test-package-route')
async def metadata_only_route(request):
    raise RuntimeError('Package routes must not be served')
class Numbers:
    @classmethod
    def INPUT_TYPES(cls): return {'required':{'start':('INT',{'default':2})}}
    RETURN_TYPES=('INT',)
    OUTPUT_IS_LIST=(True,)
    FUNCTION='generate'
    def generate(self,start): return ([start,start+1],)
    @classmethod
    def IS_CHANGED(cls,start): return start
class Scale:
    @classmethod
    def INPUT_TYPES(cls): return {'required':{'number':('INT',{'forceInput':True}),'factor':('INT',{'default':3})}}
    RETURN_TYPES=('INT',)
    FUNCTION='scale'
    @classmethod
    def VALIDATE_INPUTS(cls,factor): return True if factor>0 else 'factor must be positive'
    def scale(self,number,factor): return (number*factor,)
class Save:
    @classmethod
    def INPUT_TYPES(cls): return {'required':{'numbers':('INT',{'forceInput':True})},'hidden':{'identity':'UNIQUE_ID','prompt':'PROMPT'}}
    RETURN_TYPES=()
    OUTPUT_NODE=True
    INPUT_IS_LIST=True
    FUNCTION='save'
    def save(self,numbers,identity,prompt):
        result={'numbers':numbers,'id':identity[0],'classes':[row['class_type'] for row in prompt[0].values()]}
        (Path(folder_paths.get_output_directory())/'numbers.json').write_text(json.dumps(result))
        return {'ui':{'text':[json.dumps(result)]}}
class SaveFile:
    @classmethod
    def INPUT_TYPES(cls): return {'required':{'image':([p.name for p in Path(folder_paths.get_input_directory()).glob('*.png')] or ['placeholder.png'],{'image_upload':True})}}
    RETURN_TYPES=()
    OUTPUT_NODE=True
    FUNCTION='save'
    def save(self,image):
        source=Path(folder_paths.get_input_directory())/image
        (Path(folder_paths.get_output_directory())/'copied.png').write_bytes(source.read_bytes())
        return {'ui':{'text':[image]}}
NODE_CLASS_MAPPINGS={'TestNumbers':Numbers,'TestScale':Scale,'TestSave':Save,'TestSaveFile':SaveFile}
'''


@pytest.fixture
def installed(tmp_path):
    runtime = PROJECT / 'runtimes/minimax-h3/ComfyUI'
    python = PROJECT / 'venv/bin/python'
    if not runtime.is_dir() or not python.is_file():
        pytest.skip('Private Comfy inference runtime is not installed')
    folder = tmp_path / 'package'
    folder.mkdir()
    (folder / 'nodes.py').write_text(SOURCE)
    manifest = discover_node_package(folder)
    assert manifest['status']=='ready', manifest.get('issues')
    receipts = tmp_path / 'manifests'
    receipts.mkdir()
    (receipts / 'fixture.json').write_text(json.dumps(manifest))
    manager = ComfyJobManager(tmp_path / 'jobs',manifests=receipts)
    raw = {'prompt':{'n':{'class_type':'TestNumbers','inputs':{'start':2}},
                     's':{'class_type':'TestScale','inputs':{'number':['n',0],'factor':3}},
                     'out':{'class_type':'TestSave','inputs':{'numbers':['s',0]}}},
           'output_nodes':['out'],'packages':[{'id':manifest['package_id'],'fingerprint':manifest['source']['fingerprint']}],
           'external_inputs':{},'gpu':'cpu'}
    return manager,raw,folder


def execute(tmp_path,manager,raw,files=None):
    directory = tmp_path / 'work'
    directory.mkdir(exist_ok=True)
    saved={}
    for key,(name,data) in (files or {}).items():
        path=directory/name;path.write_bytes(data);saved[key]=[str(path)]
    config = manager.validate_config(raw,{key:[None] for key in saved})
    manifest = {'config':config,'runtime_path':str(manager.runtime),'output_path':str(directory/'output.zip'),
                'status_path':str(directory/'progress.json'),**saved}
    path = directory / 'manifest.json'
    path.write_text(json.dumps(manifest))
    result = subprocess.run([str(PROJECT/'venv/bin/python'),str(PROJECT/'scripts/comfy_graph_worker.py'),'--job',str(path)],
                            capture_output=True,text=True,timeout=60)
    return result,directory


def test_real_classes_list_broadcast_hidden_inputs_and_owned_outputs(installed,tmp_path):
    manager,raw,_=installed
    result,directory=execute(tmp_path,manager,raw)
    assert result.returncode==0,result.stderr[-6000:]
    with zipfile.ZipFile(directory/'output.zip') as archive:
        value=json.loads(archive.read('outputs/numbers.json'))
        assert value['numbers']==[6,9] and value['id']=='out'
        assert value['classes']==['TestNumbers','TestScale','TestSave']
        assert json.loads(archive.read('result.json'))['status']=='completed'
    assert json.loads((directory/'progress.json').read_text())['phase']=='completed'


def test_class_validator_blocks_execution_and_changed_source_requires_rediscovery(installed,tmp_path):
    manager,raw,folder=installed
    raw['prompt']['s']['inputs']['factor']=-1
    result,directory=execute(tmp_path,manager,raw)
    assert result.returncode!=0
    assert 'factor must be positive' in (directory/'progress.json').read_text()
    assert not (directory/'output.zip').exists()
    (folder/'new_helper.py').write_text('changed=True')
    with pytest.raises(ValueError,match='changed'):
        manager.validate_config(raw,{})


def test_file_widget_uses_owned_job_input_instead_of_client_path(installed,tmp_path):
    from tests.test_video_submission import image
    manager,raw,_=installed
    raw['prompt']={'out':{'class_type':'TestSaveFile','inputs':{}}}
    raw['external_inputs']={'out':{'image':{'kind':'FILE','media_kind':'image','binding':'asset_0'}}}
    result,directory=execute(tmp_path,manager,raw,{'asset_0':('source.png',image())})
    assert result.returncode==0,result.stderr[-6000:]
    with zipfile.ZipFile(directory/'output.zip') as archive:
        assert archive.read('outputs/copied.png')==image()
    assert (directory/'comfy-input'/'input-0-0.png').read_bytes()==image()


def test_invalid_receipts_hidden_inputs_and_media_mismatches_fail_before_launch(installed):
    manager,raw,_=installed
    invalid=[]
    for package in ({'id':[],'fingerprint':'bad'},{'id':'missing','fingerprint':'x'*64},raw['packages'][0]|{'path':'/tmp/arbitrary.py'}):
        value=copy.deepcopy(raw);value['packages']=[package];invalid.append(value)
    value=copy.deepcopy(raw);value['packages']*=2;invalid.append(value)
    value=copy.deepcopy(raw);value['prompt']['out']['inputs']['prompt']={};invalid.append(value)
    value=copy.deepcopy(raw);value['external_inputs']={'s':{'number':{'kind':'IMAGE','binding':'asset_0'}}};invalid.append(value)
    value=copy.deepcopy(raw);value['prompt']={'out':{'class_type':'TestSaveFile','inputs':{'image':'/etc/passwd'}}};invalid.append(value)
    for value in invalid:
        with pytest.raises(ValueError):
            manager.validate_config(value,{})
