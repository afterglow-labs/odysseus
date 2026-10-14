"""Data-only conversion never executes graph code or accesses imported paths."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
import pytest

from src.comfy_workflow_conversion import convert_comfyui, WIDGETS, reference
from src.h3_video import H3JobManager
from routes.workflow_conversion_routes import setup_workflow_conversion_routes
from routes.video_workflow_routes import add_workflow_routes

FIXTURES = Path(__file__).parent / 'fixtures'


def graph():
    return json.loads((FIXTURES / 'comfy_h3_api.json').read_text())


def inventory():
    return {'components': [{'id': role, 'name': name, 'role': role, 'path': '/not-opened/' + name}
        for role, name in [('model', 'synthetic-fl2va.safetensors'), ('encoder', 'synthetic-encoder.safetensors'),
                          ('video_vae', 'synthetic-video-vae.safetensors'), ('audio_vae', 'synthetic-audio-vae.safetensors')]] +
        [{'id':'ref2va-model','name':'synthetic-ref2va.safetensors','role':'model','variant':'ref2va','path':'/not-opened/synthetic-ref2va.safetensors'}],
        'gpus': [], 'runtime_ready': False, 'defaults': {}}


def ui_graph(api):
    nodes, links = [], []
    defaults = {'bit_depth':'auto', 'color_space':'sRGB', 'control_after_generate':'fixed'}
    for identifier, row in api.items():
        widgets, ports = [], []
        schema = WIDGETS.get(row['class_type'], ())
        for key in schema:
            widgets.append(row['inputs'].get(key, defaults.get(key)))
        while widgets and widgets[-1] is None:
            widgets.pop()
        for key, value in row['inputs'].items():
            if reference(value):
                lid = len(links) + 1
                links.append([lid, value[0], value[1], identifier, len(ports), '*'])
                ports.append({'name': key, 'link': lid})
        nodes.append({'id': identifier, 'type': row['class_type'], 'mode': 0,
                      'inputs': ports, 'widgets_values': widgets})
    return {'nodes': nodes, 'links': links}


def seed_hunter():
    doc = json.loads((FIXTURES / 'comfy_seed_hunter_v251_structure.json').read_text())
    outer = {node['id']: node for node in doc['nodes']}
    inner = {node['id']: node for group in doc['definitions']['subgraphs'] for node in group['nodes']}
    from src.comfy_seed_hunter import FORMULAS
    for identifier, formula in FORMULAS.items():
        inner[identifier]['widgets_values'] = [formula]
    for identifier, values in {3: ['encoder.safetensors', 'minimax', 'default'], 4: ['model.safetensors', 'default'],
                               7: ['beta', 8, 1], 9: [.4, '9:16 (Portrait)', 32]}.items():
        inner[identifier]['widgets_values'] = values
    for identifier, values in {22: ['er_sde', 'beta', 6, .4, '9:16 (Portrait)', 3.0], 365: ['A neutral blue sphere.'],
                               16: [123, 'fixed'], 196: ['pytorch'],
                               402: ['sla', 10, .2, 1, '', 12288, 256, 'exact_kv_and_rows', False],
                               416: ['[]', 'none', 0, False], 5: ['', 512, 512, 124, 'match'],
                               171: ['synthetic-ref2va.safetensors', 'synthetic-encoder.safetensors', 'synthetic-video-vae.safetensors', 'synthetic-audio-vae.safetensors'],
                               554: [{'loras': [{'on': True, 'name': 'neutral-lora.safetensors', 'sm': .75}]}]}.items():
        outer[identifier]['widgets_values'] = values
    for identifier in (18, 134, 144):
        outer[identifier]['widgets_values'] = {'frame_rate':24, 'loop_count':0, 'pingpong':False, 'trim_to_audio':False,
                                              'format':'video/h264-mp4', 'pix_fmt':'yuv420p', 'crf':19}
    return doc


def test_api_roundtrip_preserves_values_and_missing_choices_never_retain_old_draft():
    report = convert_comfyui(graph())
    assert report['convertible'] and not report['runnable']
    cfg = report['config']
    assert (cfg['width'], cfg['height'], cfg['frames'], cfg['seed'], cfg['steps']) == (512, 512, 124, 42, 8)
    assert cfg['prompt'] == graph()['5']['inputs']['prompt']
    assert cfg['model'] == cfg['encoder'] == cfg['video_vae'] == cfg['audio_vae'] == ''
    resolved = convert_comfyui(graph(), inventory())
    assert resolved['runnable'] and resolved['resolved_components']['model'] == 'model'
    assert resolved['document']['format'] == 'odysseus-video-workflow'
    assert 'path' not in json.dumps(resolved['document'])


def test_ui_links_and_primitive_values_resolve_to_same_draft():
    api = graph()
    api['12'] = {'class_type':'PrimitiveInt', 'inputs':{'value':62}}
    api['7']['inputs']['seed'] = ['12', 0]
    ui = ui_graph(api)
    assert convert_comfyui(ui)['document'] == convert_comfyui(api)['document']
    assert convert_comfyui(ui)['config']['seed'] == 62


def test_lora_order_negative_strength_and_unavailable_rows_preserved():
    api = graph()
    api['12'] = {'class_type':'LoraLoaderModelOnly', 'inputs':{'model':['1',0], 'lora_name':r'C:\weights\age.safetensors', 'strength_model':-2.5}}
    api['13'] = {'class_type':'LoraLoaderModelOnly', 'inputs':{'model':['12',0], 'lora_name':'style.safetensors', 'strength_model':.75}}
    api['6']['inputs']['model'] = ['13',0]
    result = convert_comfyui(api)
    assert result['convertible']
    assert result['document']['config']['lora_strengths'] == [-2.5, .75]
    assert result['document']['components']['lora_0']['name'] == 'age.safetensors'
    assert result['config']['loras'][0] == {'id':'unavailable:lora_0:age.safetensors', 'strength':-2.5}
    inv = inventory()
    inv['components'] += [{'id':'a','role':'lora','name':'age.safetensors'}, {'id':'b','role':'lora','name':'age.safetensors'}]
    assert 'ambiguous_component' in [issue['code'] for issue in convert_comfyui(api,inv)['issues']]


def test_active_unknown_nodes_report_identity_without_executing_or_dropping_and_inactive_are_ignored():
    api = graph()
    api['99'] = {'class_type':'ExecutePython', 'inputs':{'code':'raise RuntimeError("DO NOT EXECUTE")'}}
    assert convert_comfyui(api)['convertible']
    api['6']['inputs']['model'] = ['99',0]
    result = convert_comfyui(api)
    assert not result['convertible']
    item = next(row for row in result['issues'] if row['code'] == 'unsupported_node')
    assert item['node_id'] == '99' and item['node_type'] == 'ExecutePython'
    assert 'DO NOT EXECUTE' not in json.dumps(result)


def test_multiple_outputs_require_explicit_selection_and_cycles_block():
    api = graph()
    api['12'] = copy.deepcopy(api['11'])
    assert convert_comfyui(api)['issues'][0]['code'] == 'output_selection'
    assert convert_comfyui(api, output_node='11')['convertible']
    api['6']['inputs']['model'] = ['6',0]
    assert convert_comfyui(api, output_node='11')['issues'][0]['code'] == 'cycle'


def test_paired_video_and_image_reference_slots_preserve_order_without_opening_paths():
    api = graph()
    api['5']['class_type']='MiniMaxH3ReferenceToVideo'
    api['5']['inputs']['audio_vae']=['4',0]
    api['1']['inputs']['unet_name']='synthetic-ref2va.safetensors'
    api['12'] = {'class_type':'LoadVideo','inputs':{'file':'../../private/reference.mp4'}}
    api['13'] = {'class_type':'GetVideoComponents','inputs':{'video':['12',0]}}
    api['14'] = {'class_type':'LoadImage','inputs':{'image':'head.png'}}
    api['5']['inputs'].update({'ref_videos.ref_video_1':['13',0], 'ref_video_audios.ref_video_audio_1':['13',1],
                              'ref_images.ref_image_1':['14',0]})
    result = convert_comfyui(api, inventory())
    assert result['convertible'] and not result['runnable']
    assert [(x['field'],x['name']) for x in result['inputs']] == [('reference_images','head.png'),('reference_videos','reference.mp4')]
    assert any(x['code'] == 'video_preprocessing' for x in result['issues'])
    del api['5']['inputs']['ref_video_audios.ref_video_audio_1']
    assert convert_comfyui(api)['issues'][0]['code'] == 'video_reference_audio'


def test_empty_ref2va_graph_uses_single_pass_engine_that_accepts_empty_conditioning():
    api=graph();api['5']['class_type']='MiniMaxH3ReferenceToVideo';api['5']['inputs']['audio_vae']=['4',0]
    api['1']['inputs']['unet_name']='synthetic-ref2va.safetensors'
    result=convert_comfyui(api)
    assert result['convertible']
    assert result['config']['recipe']=='seed_hunter'
    assert result['config']['seed_hunter']=={'stage':'single','shift_enabled':True,'attention_backend':'pytorch'}
    assert result['config']['shift_video']==12 and result['config']['shift_audio']==3


def test_vfx_adapter_cannot_silently_turn_reference_into_source_guide():
    api=graph()
    api['12']={'class_type':'LoraLoaderModelOnly','inputs':{'model':['1',0], 'lora_name':'minimax_h3_vfx_edit_v1.0_r128.safetensors','strength_model':1}}
    api['6']['inputs']['model']=['12',0]
    result=convert_comfyui(api)
    assert not result['convertible'] and result['issues'][0]['code']=='vfx_source_guide'


@pytest.mark.parametrize('key,value', [('cfg',7), ('denoise',.6), ('sampler_name','unsupported'), ('steps',0)])
def test_unrepresentable_sampling_is_not_silently_defaulted(key,value):
    api=graph(); api['7']['inputs'][key]=value
    assert not convert_comfyui(api)['convertible']


def test_known_seed_hunter_preserves_preview_recipe_without_reading_saved_media():
    result=convert_comfyui(seed_hunter(),inventory())
    assert result['convertible'],result['issues']
    cfg=result['config']
    assert cfg['recipe']=='seed_hunter' and cfg['seed_hunter']['preview_count']==3
    assert (cfg['frames'],cfg['width'],cfg['height'])==(73,480,864)
    assert (cfg['sampler'],cfg['scheduler'],cfg['steps'],cfg['seed'])==('er_sde','beta',6,123)
    assert cfg['seed_hunter']['sparse_method']=='sla' and not cfg['seed_hunter']['shift_enabled']
    assert cfg['loras'][0]['strength']==.75
    assert any(x['code']=='output_storage' for x in result['issues'])


def test_seed_hunter_changed_topology_or_calculator_blocks_execution():
    doc=seed_hunter(); doc['nodes'][0]['mode']=99
    assert convert_comfyui(doc)['issues'][0]['code']=='seed_hunter_topology'
    doc=seed_hunter()
    for group in doc['definitions']['subgraphs']:
        for node in group['nodes']:
            if node['id']==24: node['widgets_values']=['__import__("os").system("false")']
    result=convert_comfyui(doc)
    assert not result['convertible'] and result['issues'][0]['code']=='calculator'


def test_seed_hunter_subgraph_interface_and_implicit_wiring_cannot_silently_change():
    doc=seed_hunter()
    ports=doc['definitions']['subgraphs'][0]['outputs']
    ports[0],ports[1]=ports[1],ports[0]
    assert convert_comfyui(doc)['issues'][0]['code']=='seed_hunter_topology'
    doc=seed_hunter()
    node=next(row for row in doc['nodes'] if row['id']==107)
    node['properties']['ue_properties']['input_regex']='different'
    assert convert_comfyui(doc)['issues'][0]['code']=='seed_hunter_topology'


def test_dynamic_codec_schema_is_accepted_but_malformed_settings_are_reported():
    doc=graph();doc['11']['inputs']['format']={'format':'mp4','codec':{'codec':'h264','encoding':{'encoding':'re-encode','crf':19}}}
    report=convert_comfyui(doc)
    assert report['convertible'] and any(row['code']=='output_quality' for row in report['issues'])
    doc['7']['inputs']['sampler_name']={}
    report=convert_comfyui(doc)
    assert not report['convertible'] and report['issues'][-1]['code']=='invalid_settings'


@pytest.fixture
def api(tmp_path):
    manager=H3JobManager(tmp_path/'jobs',project=tmp_path/'project')
    manager.inventory=inventory
    app=FastAPI()
    app.state.auth_manager=SimpleNamespace(is_configured=True,is_admin=lambda user:user=='owner')
    @app.middleware('http')
    async def identity(request,call_next):
        request.state.current_user=request.headers.get('x-user','owner')
        return await call_next(request)
    app.include_router(setup_workflow_conversion_routes(manager))
    router=APIRouter(prefix='/api/video/h3');add_workflow_routes(router,manager,family='h3');app.include_router(router)
    return TestClient(app),tmp_path


def test_conversion_route_auth_size_finite_json_and_normal_import_contract(api):
    client,tmp=api
    url='/api/video/workflows/convert-comfyui'
    assert client.post(url,json={'workflow':graph()},headers={'x-user':'reader'}).status_code==403
    assert client.post(url,content=b'x'*(1024*1024+1)).status_code==413
    assert client.post(url,content=b'{"workflow":{"x":NaN}}').status_code==400
    preview=client.post(url,json={'workflow':graph()})
    assert preview.status_code==200 and preview.json()['runnable']
    imported=client.post('/api/video/h3/workflow/import',content=json.dumps(graph()))
    assert imported.status_code==200,imported.text
    value=imported.json()
    assert value['workflow']==preview.json()['document']
    assert value['resolved_components']['model']=='model'
    assert value['conversion']['runnable'] and value['input_requirements']==[]
    assert not any('Choose an installed' in warning for warning in value['warnings'])
    assert not list(tmp.rglob('upload.zip'))


def test_unsupported_import_returns_structured_report_and_cleans_staging(api):
    client,tmp=api;doc=graph();doc['6']['class_type']='UnimplementedEffect'
    result=client.post('/api/video/h3/workflow/import',content=json.dumps(doc))
    assert result.status_code==400,result.text
    assert not result.json()['detail']['conversion']['convertible']
    assert not list(tmp.rglob('receipt.json'))
    assert not list(tmp.rglob('upload.zip'))


@pytest.mark.parametrize('document',[{'nodes':[{'id':1,'type':{},'inputs':[]}],'links':[]},
                                     {'nodes':[{'id':1,'type':'UNETLoader','inputs':None}],'links':[]}])
def test_malformed_ui_schema_is_a_client_error_on_both_transports(api,document):
    client,_=api
    assert client.post('/api/video/workflows/convert-comfyui',json={'workflow':document}).status_code==400
    assert client.post('/api/video/h3/workflow/import',content=json.dumps(document)).status_code==400
