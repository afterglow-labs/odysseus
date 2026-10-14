"""Graph connections are executable data, independent from visual layout."""
import copy
import json
from pathlib import Path

import pytest

from src.workflow_graph import (catalog, compile_graph, make_edge, make_node,
                                new_document, validate_document)
from src.workflow_graph_import import import_comfyui, from_native_config

FIXTURES = Path(__file__).parent / "fixtures"


def ready(template="h3"):
    doc = new_document(template=template)
    for node in doc["nodes"]:
        if node["type"].startswith("component."):
            node["params"]["component"] = node["id"]
        if node["type"] == "input.prompt":
            node["params"]["text"] = "A neutral sphere rolls."
    return doc


def get(doc, identifier):
    return next(n for n in doc["nodes"] if n["id"] == identifier)


def wire(doc, a, ap, b, bp):
    doc["edges"].append(make_edge(a,ap,b,bp))


def test_layout_and_titles_do_not_change_execution():
    doc=ready()
    first=compile_graph(doc)
    assert first["valid"], first["issues"]
    for node in doc["nodes"]:
        node["position"]={"x":999,"y":-100}; node["title"]="Renamed"
    second=compile_graph(doc)
    assert first["plan"]==second["plan"]
    assert first["plan"][0]["config"]["prompt"]=="A neutral sphere rolls."


def test_rewiring_prompt_changes_the_executable_request():
    doc=ready()
    doc["nodes"].append(make_node("other", "input.prompt",params={"text":"New connected instruction"}))
    for edge in doc["edges"]:
        if edge["target"]=={"node":"generate","port":"prompt"}:edge["source"]["node"]="other"
    result=compile_graph(doc)
    assert result["valid"]
    assert result["plan"][0]["config"]["prompt"]=="New connected instruction"


def test_ordered_lora_chain_signed_strength_and_bypass_are_real():
    doc=ready()
    for name, strength in (("style", .7),("age",-3)):
        doc["nodes"].append(make_node(name,"model.lora",params={"component":name,"strength":strength}))
    doc["edges"]=[e for e in doc["edges"] if e["target"]!={"node":"setup","port":"model"}]
    wire(doc,"model","model","style","model");wire(doc,"style","model","age","model");wire(doc,"age","model","setup","model")
    assert compile_graph(doc)["plan"][0]["config"]["loras"]==[{"id":"style","strength":.7},{"id":"age","strength":-3}]
    get(doc,"style")["state"]="bypassed"
    assert compile_graph(doc)["plan"][0]["config"]["loras"]==[{"id":"age","strength":-3}]
    get(doc,"age")["params"]["enabled"]=False
    assert compile_graph(doc)["plan"][0]["config"]["loras"]==[]


def test_parallel_generation_branches_share_setup_but_not_settings():
    doc=ready()
    doc["nodes"].extend([make_node("other","h3.generate",params={"steps":8,"seed":400}),make_node("out2","output.video")])
    for name in ("setup","prompt","references"):wire(doc,name,name,"other",name)
    wire(doc,"other","result","out2","result")
    result=compile_graph(doc)
    assert result["valid"]
    assert [(p["id"],p["config"]["steps"],p["config"]["seed"]) for p in result["plan"]]==[("generate",20,42),("other",8,400)]
    assert [p["id"] for p in compile_graph(doc,output_ids=["out2"])["plan"]]==["other"]


def test_unknown_operations_are_preserved_and_block_only_reachable_execution():
    doc=ready();unknown=make_node("unknown","custom.ExecutePython",params={"code":"raise AssertionError('NEVER RUN')"})
    doc["nodes"].append(unknown)
    result=compile_graph(doc)
    assert result["valid"] and result["issues"][0]["code"]=="unsupported_inactive"
    before=copy.deepcopy(doc)
    for edge in doc["edges"]:
        if edge["target"]=={"node":"setup","port":"model"}:edge["source"]={"node":"unknown","port":"model"}
    result=compile_graph(doc)
    assert not result["valid"] and any(i["code"]=="unsupported_node" for i in result["issues"])
    assert doc["nodes"]==before["nodes"]


def test_types_cycles_disabled_nodes_and_missing_connections_block():
    doc=ready()
    for edge in doc["edges"]:
        if edge["target"]=={"node":"setup","port":"model"}:edge["source"]={"node":"prompt","port":"prompt"}
    assert any(i["code"]=="port_type" for i in compile_graph(doc)["issues"])
    doc=ready(); get(doc,"model")["state"]="disabled"
    assert any(i["code"]=="disabled_input" for i in compile_graph(doc)["issues"])
    doc=ready();doc["nodes"].append(make_node("lora","model.lora",params={"component":"a"}))
    for edge in doc["edges"]:
        if edge["target"]=={"node":"setup","port":"model"}:edge["source"]={"node":"lora","port":"model"}
    wire(doc,"lora","model","lora","model")
    assert any(i["code"]=="cycle" for i in compile_graph(doc)["issues"])
    doc=ready(); doc["edges"]=[e for e in doc["edges"] if e["target"]["port"]!="encoder"]
    assert any(i["code"]=="missing_input" for i in compile_graph(doc)["issues"])


def test_media_pipeline_compiles_distinct_cpu_steps_and_dependencies():
    doc=new_document(template="media")
    get(doc,"trim")["params"]={"start":2,"end":5}
    result=compile_graph(doc)
    assert result["valid"]
    assert [(s["id"],s["operation"],s["dependencies"]) for s in result["plan"]]==[("trim","trim",[]),("resize","resize",["trim"]),("output","save",["resize"])]
    assert result["plan"][0]["inputs"]=={"video":["video"]}
    get(doc,"trim")["params"]["end"]=1
    assert any(i["code"]=="trim_range" for i in compile_graph(doc)["issues"])


def test_media_output_can_feed_reference_bundle_before_generation():
    doc=ready();doc["nodes"].extend([make_node("video","input.video"),make_node("trim","media.trim",params={"start":5,"end":8})])
    wire(doc,"video","video","trim","video");wire(doc,"trim","video","references","reference_videos")
    result=compile_graph(doc)
    assert result["valid"]
    assert result["plan"][-1]["inputs"]=={"reference_videos":["trim"]}
    assert result["plan"][-1]["dependencies"]==["trim"]


def test_result_selection_refinement_and_dormant_branch_settings():
    doc=ready("seed_hunter")
    result=compile_graph(doc)
    assert result["valid"] and len(result["plan"])==1
    get(doc,"refine")["state"]="active"
    get(doc,"refine")["params"].update(candidate=2,steps=4,denoise=.25)
    result=compile_graph(doc)
    final=result["plan"][-1]
    assert result["valid"] and final["operation"]=="refine"
    assert final["depends_on"]=="generate" and final["candidate"]==2
    assert final["config"]["seed_hunter"]["denoise"]==.25 and final["config"]["steps"]==4
    get(doc,"generate")["params"]["preview_count"]=1
    assert any(i["code"]=="candidate" for i in compile_graph(doc)["issues"])


def test_invalid_documents_rejected_without_mutating_input():
    doc=ready(); doc["nodes"].append(copy.deepcopy(doc["nodes"][0]))
    with pytest.raises(ValueError,match="duplicate"):validate_document(doc)
    doc=ready(); get(doc,"generate")["params"]["seed"]=float("nan")
    with pytest.raises(ValueError,match="finite"):validate_document(doc)
    doc=ready(); doc["edges"][0]["source"]["node"]="missing"
    with pytest.raises(ValueError,match="missing"):validate_document(doc)


def test_native_draft_conversion_keeps_values_multiple_loras_and_slot_counts():
    config={"mode":"ref2va","model":"m","encoder":"e","video_vae":"v","audio_vae":"a","prompt":"Specific existing instructions", "steps":12,"seed":7,
            "loras":[{"id":"style","strength":.4},{"id":"age","strength":-2}]}
    doc=from_native_config(config,input_counts={"reference_videos":2,"reference_images":3})
    compiled=compile_graph(doc)
    assert compiled["valid"]
    plan=compiled["plan"][0]
    assert plan["config"]["prompt"]==config["prompt"] and plan["config"]["loras"]==config["loras"]
    assert plan["inputs"]["reference_videos"]==["reference_videos__0","reference_videos__1"]
    assert plan["inputs"]["reference_images"]==["reference_images__0","reference_images__1","reference_images__2"]


def test_component_names_resolve_only_exact_unique_role_matches():
    doc=ready();get(doc,"model")["params"]={"component":"","name":"model.safetensors"}
    inv={"components":[{"role":r,"id":r,"name":r+".safetensors"} for r in ("model","encoder","video_vae","audio_vae")]}
    assert compile_graph(doc,inventory=inv)["valid"]
    inv["components"].append({"role":"model","id":"other","name":"model.safetensors"})
    assert any(i["code"]=="ambiguous_component" for i in compile_graph(doc,inventory=inv)["issues"])


def seed_hunter():
    # Reuse the sanitized structural fixture and populate deterministic settings.
    from tests.test_comfy_workflow_conversion import seed_hunter as fixture
    doc=fixture();nodes={n["id"]:n for n in doc["nodes"]}
    for identifier,values in {243:["upscaler.safetensors","megapixels",2,32,True,False,"cuda","fp16"],135:[444,"er_sde",2,.4],103:[999,"fixed"],
                             195:[12,6],193:[4,2048],305:[4],297:[22],540:[6],541:["simple",16,1],556:[12,3],
                             526:["Start "],571:["Dialogue"],522:[" calm"],527:[" end"],535:{"audio":"voice.wav","start_time":0,"duration":45},
                             553:[{"loras":[]}],552:[{"loras":[]}],403:["sla",10,.2,1,"",12288,256,"exact_kv_and_rows",False]}.items():
        nodes[identifier]["widgets_values"]=values
    return doc


def test_seed_hunter_import_preserves_all_branches_settings_source_and_states():
    source=seed_hunter();original=copy.deepcopy(source)
    doc=import_comfyui(source)
    assert doc["source"]["document"]==original and source==original
    assert doc["source"]["adapter"]=="seed_hunter_v251"
    assert [get(doc,f"preview_{i}")["params"]["seed"] for i in (1,2,3)]==[123,1123,2123]
    assert get(doc,"refine")["state"]=="bypassed"
    assert get(doc,"refine")["params"]["seed"]==999
    assert get(doc,"refine")["params"]["width"]==1088 and get(doc,"refine")["params"]["height"]==1952
    assert get(doc,"continuation")["state"]=="disabled" and get(doc,"dialogue")["state"]=="disabled"
    assert get(doc,"dialogue_prompt")["params"]["text"]=="Start Dialogue</d>  calm end"
    for n in doc["nodes"]:
        if n["type"].startswith("component.") or n["type"]=="model.lora": n["params"]["component"]=n["id"]
    result=compile_graph(doc)
    assert result["valid"],result["issues"]
    assert [p["id"] for p in result["plan"]]==["preview_1","preview_2","preview_3"]
    get(doc,"refine")["state"]="active";get(doc,"selection")["params"]["selection"]="third"
    final=compile_graph(doc)["plan"][-1]
    assert final["id"]=="refine" and final["depends_on"]=="preview_3"
    assert final["config"]["seed"]==999 and final["config"]["steps"]==2


def test_seed_hunter_changed_expression_remains_blocked_and_never_executed():
    source=seed_hunter()
    for group in source["definitions"]["subgraphs"]:
        for node in group["nodes"]:
            if node["id"]==127:node["widgets_values"]=["__import__('os').system('false')"]
    doc=import_comfyui(source)
    assert not compile_graph(doc)["valid"]
    assert doc["source"]["document"]==source


def test_generic_comfy_multiple_outputs_import_as_separate_executable_branches():
    source=json.loads((FIXTURES/"comfy_h3_api.json").read_text())
    source["12"]=copy.deepcopy(source["11"])
    doc=import_comfyui(source)
    for n in doc["nodes"]:
        if n["type"].startswith("component."):n["params"]["component"]=n["type"].split(".")[1]
    result=compile_graph(doc)
    assert result["valid"],result["issues"]
    assert len(result["outputs"])==2 and len(result["plan"])==2
    assert doc["source"]["document"]==source


def test_catalog_carries_real_port_types_parameter_defaults_and_new_processing_types():
    nodes={n["type"]:n for n in catalog()["node_types"]}
    assert nodes["model.lora"]["bypass"]=={"model":"model"}
    assert nodes["hub.setup"]["outputs"][0]["type"]=="SETUP"
    assert {"media.trim","media.resize","h3.seed_scout"}<=nodes.keys()
    assert any(p["id"]=="scout_timeout" and p["default"]==0 for p in nodes["h3.seed_scout"]["params"])


def test_bfs_draft_preserves_family_specific_components_and_adapter_strengths():
    source={"workflow_id":"wan22_head_swap","components":{"model":"high","model_low":"low","encoder":"umt","video_vae":"vae","lora":"high-lora","lora_low":"low-lora"},
            "prompt":"A neutral person","width":640,"height":384,"frames":73,"steps":40,"seed":3,"gpu":"0","cfg":7,"lora_scale":.8}
    doc=from_native_config(source,"bfs",{"source_video":1,"identity_image":1})
    result=compile_graph(doc)
    assert result["valid"],result["issues"]
    assert result["plan"][0]["config"]["components"]==source["components"]
    assert result["plan"][0]["config"]["lora_scale"]==.8
    assert result["plan"][0]["config"]["cfg"]==7
    assert get(doc,"setup")["type"]=="bfs.components"


def test_dormant_source_controls_keep_values_and_strip_editor_flags_for_runtime():
    source=seed_hunter();doc=import_comfyui(source)
    cfg=get(doc,"preview_1")["params"]
    assert not cfg["ff_chunk_enabled"] and cfg["ff_chunk_size"]==4
    assert not cfg["interpolation_enabled"] and cfg["interpolation_fps"]==48
    for n in doc["nodes"]:
        if n["type"].startswith("component.") or n["type"]=="model.lora":n["params"]["component"]=n["id"]
    options=compile_graph(doc)["plan"][0]["config"]["seed_hunter"]
    assert options["ff_chunk_size"]==0 and options["output_fps"]==24
    assert "ff_chunk_enabled" not in options and "interpolation_enabled" not in options
    get(doc,"preview_1")["params"].update(ff_chunk_enabled=True,interpolation_enabled=True)
    options=compile_graph(doc)["plan"][0]["config"]["seed_hunter"]
    assert options["ff_chunk_size"]==4 and options["output_fps"]==48
    get(doc,"refine")["state"]="active";get(doc,"tae_preview_override")["state"]="active"
    assert any(i["code"]=="unsupported_node" for i in compile_graph(doc)["issues"])


def test_seed_scout_template_uses_ref2va_and_interactive_waiting_controls():
    doc=ready("seed_scout")
    result=compile_graph(doc)
    assert result["valid"],result["issues"]
    config=result["plan"][0]["config"]
    assert config["recipe"]=="seed_hunter" and config["mode"]=="ref2va"
    assert config["seed_hunter"]=={"stage":"scout","shift_enabled":True,"scout_count":4,"scout_step":1,"scout_mode":"interactive","scout_timeout":0,"scout_preview_mode":"vae"}


def test_reference_video_ranges_survive_existing_draft_import():
    config={"mode":"ref2va","model":"m","encoder":"e","video_vae":"v","audio_vae":"a","prompt":"Keep the camera still", "reference_video_ranges":[{"start_seconds":22,"duration_seconds":8}]}
    result=compile_graph(from_native_config(config,input_counts={"reference_videos":1}))
    assert result["valid"]
    assert result["plan"][0]["config"]["reference_video_ranges"]==config["reference_video_ranges"]


@pytest.fixture(autouse=True)
def isolated_package_catalog(monkeypatch):
    from src import comfy_node_manifest
    monkeypatch.setattr(comfy_node_manifest, "load_manifests", lambda: [])


def package_manifest():
    from src.comfy_node_manifest import normalize_node_info
    rows={
        "GraphNumber":{"input":{"required":{"value":["INT",{"default":2}]}},"output":["INT"]},
        "GraphAdd":{"input":{"required":{"a":["INT",{"forceInput":True}],"b":["INT",{"default":3}]}},"output":["INT"]},
        "GraphSave":{"input":{"required":{"value":["INT",{"forceInput":True}]}},"output":[],"output_node":True},
        "GraphImage":{"input":{"required":{"image":[["placeholder.png"],{"image_upload":True}]}},"output":["IMAGE"]},
        "GraphSaveImage":{"input":{"required":{"images":["IMAGE"]}},"output":[],"output_node":True},
    }
    return {"status":"ready","package":{"id":"example-package","source_path":"/unused/package","fingerprint":"fixture-fingerprint"},
            "nodes":[normalize_node_info(name,info,package_id="example-package",function="run") for name,info in rows.items()]}


def test_discovered_class_metadata_produces_real_ports_and_one_shared_comfy_plan(monkeypatch):
    from src import comfy_node_manifest
    monkeypatch.setattr(comfy_node_manifest,"load_manifests",lambda:[package_manifest()])
    source={"1":{"class_type":"GraphNumber","inputs":{"value":8}},"2":{"class_type":"GraphAdd","inputs":{"a":["1",0],"b":4}},
            "3":{"class_type":"GraphSave","inputs":{"value":["2",0]}},"4":{"class_type":"GraphSave","inputs":{"value":["1",0]}}}
    doc=import_comfyui(source)
    result=compile_graph(doc)
    assert result["valid"],result["issues"]
    assert len(result["plan"])==1 and len(result["outputs"])==2
    job=result["plan"][0]
    assert job["family"]=="comfy" and job["operation"]=="execute"
    assert set(job["config"]["output_nodes"])=={"comfy_3","comfy_4"}
    assert job["config"]["prompt"]["comfy_2"]["inputs"]=={"a":["comfy_1",0],"b":4}
    assert job["config"]["gpu"]=="cpu"
    assert job["config"]["packages"]==[{"id":"example-package","fingerprint":"fixture-fingerprint"}]
    for edge in doc["edges"]:
        if edge["target"]["node"]=="comfy_4":edge["source"]={"node":"comfy_2","port":"output_0"}
    assert compile_graph(doc)["plan"][0]["config"]["prompt"]["comfy_4"]["inputs"]["value"]==["comfy_2",0]
    assert doc["source"]["document"]==source


def test_comfy_uploaded_file_is_an_explicit_binding_not_an_imported_path(monkeypatch):
    from src import comfy_node_manifest
    monkeypatch.setattr(comfy_node_manifest,"load_manifests",lambda:[package_manifest()])
    source={"1":{"class_type":"GraphImage","inputs":{"image":"/do/not/read/private.png"}},"2":{"class_type":"GraphSaveImage","inputs":{"images":["1",0]}}}
    result=compile_graph(import_comfyui(source))
    assert result["valid"],result["issues"]
    step=result["plan"][0]
    assert step["inputs"]=={"asset_0":["comfy_1::image"]}
    assert step["config"]["prompt"]["comfy_1"]["inputs"]=={}
    assert step["config"]["external_inputs"]=={"comfy_1":{"image":{"kind":"FILE","binding":"asset_0","media_kind":"image"}}}
    assert "/do/not/read" not in json.dumps(step)


def test_comfy_metadata_collision_and_unbound_object_info_block_execution(monkeypatch):
    from src import comfy_node_manifest
    source={"1":{"class_type":"GraphNumber","inputs":{"value":8}},"2":{"class_type":"GraphSave","inputs":{"value":["1",0]}}}
    first=package_manifest(); second=copy.deepcopy(first);second["package"]["id"]="other-package"
    monkeypatch.setattr(comfy_node_manifest,"load_manifests",lambda:[first,second])
    assert any(i["code"]=="package_collision" for i in compile_graph(import_comfyui(source))["issues"])
    first.pop("package")
    monkeypatch.setattr(comfy_node_manifest,"load_manifests",lambda:[first])
    assert any(i["code"]=="package_unbound" for i in compile_graph(import_comfyui(source))["issues"])


def test_comfy_ui_widgets_map_through_discovered_schema_and_preserve_unmapped_values(monkeypatch):
    from src import comfy_node_manifest
    monkeypatch.setattr(comfy_node_manifest,"load_manifests",lambda:[package_manifest()])
    source={"nodes":[{"id":1,"type":"GraphNumber","widgets_values":[12],"outputs":[{"name":"INT","type":"INT"}]},
                     {"id":2,"type":"GraphSave","inputs":[{"name":"value","link":1}]}],"links":[[1,1,0,2,0,"INT"]]}
    result=compile_graph(import_comfyui(source));assert result["valid"],result["issues"]
    assert result["plan"][0]["config"]["prompt"]["comfy_1"]["inputs"]["value"]==12
    source["nodes"][0]["widgets_values"].append("unknown extra widget")
    assert any(i["code"]=="widget_schema" for i in compile_graph(import_comfyui(source))["issues"])


def test_local_metadata_replaces_unbound_schema_and_preserves_widget_contract(monkeypatch):
    from src import comfy_node_manifest
    from src.workflow_graph import node_registry
    local=package_manifest();unbound=copy.deepcopy(local);unbound.pop("package")
    field=local["nodes"][0]["inputs"][0]
    field.update(default_input=True,is_list=True)
    field["metadata"].update(multiline=True,tooltip="Helpful explanation",display="slider",dynamicPrompts=False)
    local["nodes"][0]["outputs"][0]["is_list"]=True
    monkeypatch.setattr(comfy_node_manifest,"load_manifests",lambda:[unbound,local])
    descriptor=node_registry()["comfy.GraphNumber"]
    assert descriptor["execution_status"]=="available"
    assert descriptor["inputs"][0]["default_input"] and descriptor["inputs"][0]["is_list"]
    assert descriptor["outputs"][0]["is_list"]
    widget=descriptor["params"][0]
    assert widget["multiline"] and widget["description"]=="Helpful explanation"
    assert widget["metadata"]["dynamicPrompts"] is False


def test_multiple_file_widgets_have_independent_media_bindings(monkeypatch):
    from src import comfy_node_manifest
    manifest=package_manifest();loader=manifest["nodes"][3]
    other=copy.deepcopy(loader["inputs"][0]);other["name"]="mask";loader["inputs"].append(other)
    monkeypatch.setattr(comfy_node_manifest,"load_manifests",lambda:[manifest])
    source={"1":{"class_type":"GraphImage","inputs":{}},"2":{"class_type":"GraphSaveImage","inputs":{"images":["1",0]}}}
    doc=import_comfyui(source)
    doc["nodes"].append(make_node("archive","output.video"))
    doc["edges"].append(make_edge("comfy_2","result","archive","result","archive-link"))
    result=compile_graph(doc)
    assert result["valid"],result["issues"]
    assert len(result["plan"])==1  # The result archive must never be transcoded as video.
    assert result["plan"][0]["inputs"]=={"asset_0":["comfy_1::image"],"asset_1":["comfy_1::mask"]}
    assert {o["source"] for o in result["outputs"]}=={"comfy_execution"}


def test_unadapted_path_loader_and_interactive_scout_are_explicitly_blocked(monkeypatch):
    from src import comfy_node_manifest
    manifest=package_manifest()
    manifest['nodes'].append(comfy_node_manifest.normalize_node_info('LoadPrivateVideo',{'input':{'required':{'video':['STRING',{'default':'/private/source.mp4'}]}},'output':[],'output_node':True}))
    manifest['nodes'].append(comfy_node_manifest.normalize_node_info('MiniMaxH3SeedScoutSampler',{'input':{'required':{'mode':[['interactive','scout','final'],{'default':'interactive'}]}},'output':[],'output_node':True}))
    monkeypatch.setattr(comfy_node_manifest,'load_manifests',lambda:[manifest])
    for kind,code in [('LoadPrivateVideo','media_adapter_missing'),('MiniMaxH3SeedScoutSampler','interactive_adapter')]:
        result=compile_graph(import_comfyui({'1':{'class_type':kind,'inputs':{}}}))
        assert not result['valid'] and any(i['code']==code for i in result['issues'])


def test_imported_api_settings_missing_from_installed_schema_are_not_dropped(monkeypatch):
    from src import comfy_node_manifest
    monkeypatch.setattr(comfy_node_manifest,'load_manifests',lambda:[package_manifest()])
    source={'1':{'class_type':'GraphNumber','inputs':{'value':2,'unsupported_option':True}},
            '2':{'class_type':'GraphSave','inputs':{'value':['1',0]}}}
    doc=import_comfyui(source)
    assert get(doc,'comfy_1')['params']['unsupported_option'] is True
    result=compile_graph(doc)
    assert not result['valid'] and any(i['code']=='input_schema' for i in result['issues'])


@pytest.mark.parametrize('value', [2**53, -(2**53), 2**64-1, float(2**53)])
def test_comfy_integer_precision_blocks_without_clamping_import_or_schema(monkeypatch,value):
    from src import comfy_node_manifest
    from src.workflow_graph import node_registry
    manifest=package_manifest()
    manifest['nodes'][0]['inputs'][0]['metadata'].update(min=-(2**64),max=2**64-1)
    monkeypatch.setattr(comfy_node_manifest,'load_manifests',lambda:[manifest])
    source={'1':{'class_type':'GraphNumber','inputs':{'value':value}},
            '2':{'class_type':'GraphSave','inputs':{'value':['1',0]}}}
    doc=import_comfyui(source)
    before=copy.deepcopy(doc)
    result=compile_graph(doc)
    assert not result['valid'] and any(i['code']=='parameter_precision' for i in result['issues'])
    assert doc==before and doc['source']['document']==source
    assert get(doc,'comfy_1')['params']['value']==value
    assert node_registry()['comfy.GraphNumber']['params'][0]['max']==2**64-1


@pytest.mark.parametrize('value', [2**53-1, -(2**53-1)])
def test_comfy_largest_exact_integer_values_remain_executable(monkeypatch,value):
    from src import comfy_node_manifest
    monkeypatch.setattr(comfy_node_manifest,'load_manifests',lambda:[package_manifest()])
    source={'1':{'class_type':'GraphNumber','inputs':{'value':value}},
            '2':{'class_type':'GraphSave','inputs':{'value':['1',0]}}}
    result=compile_graph(import_comfyui(source))
    assert result['valid'],result['issues']
    assert result['plan'][0]['config']['prompt']['comfy_1']['inputs']['value']==value


def test_numeric_select_and_literal_integer_port_share_precision_guard(monkeypatch):
    from src import comfy_node_manifest
    manifest=package_manifest()
    manifest['nodes'].append(comfy_node_manifest.normalize_node_info('GraphNumericChoice',{'input':{'required':{'seed':[[1,2**64-1],{}]}},'output':[],'output_node':True}))
    monkeypatch.setattr(comfy_node_manifest,'load_manifests',lambda:[manifest])
    for kind,field in [('GraphNumericChoice','seed'),('GraphSave','value')]:
        doc=import_comfyui({'1':{'class_type':kind,'inputs':{field:2**64-1}}})
        result=compile_graph(doc)
        assert not result['valid'] and any(i['code']=='parameter_precision' for i in result['issues'])
        assert get(doc,'comfy_1')['params'][field]==2**64-1


def test_connected_integer_widget_does_not_execute_its_unsafe_saved_value(monkeypatch):
    from src import comfy_node_manifest
    monkeypatch.setattr(comfy_node_manifest,'load_manifests',lambda:[package_manifest()])
    source={'1':{'class_type':'GraphNumber','inputs':{'value':1}},
            '2':{'class_type':'GraphAdd','inputs':{'a':['1',0],'b':['1',0]}},
            '3':{'class_type':'GraphSave','inputs':{'value':['2',0]}}}
    doc=import_comfyui(source)
    get(doc,'comfy_2')['params']['b']=2**64-1
    result=compile_graph(doc)
    assert result['valid'],result['issues']
    assert result['plan'][0]['config']['prompt']['comfy_2']['inputs']['b']==['comfy_1',0]
