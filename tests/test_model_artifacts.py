import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

from routes.cookbook_helpers import _cached_model_scan_script
from src.model_artifacts import scan_model_artifacts


FILES = {
    "h3/minimax_h3_head_swap_v1.0_r32.safetensors": "MiniMax H3",
    "ltx-2/head_swap_v1_13500_first_frame.safetensors": "LTX-2",
    "ltx-2/head_swap_v1_8750_first_and_last_frame.safetensors": "LTX-2",
    "ltx-2/head_swap_v2_multimodes.safetensors": "LTX-2",
    "ltx-2.3/head_swap_v3_rank_64.safetensors": "LTX-2.3",
    "ltx-2.3/head_swap_v3_rank_adaptive_fro_098.safetensors": "LTX-2.3",
    "ltx-2.5/head_swap_ltx25_r128_v1.1.safetensors": "LTX-2.5",
    "ltx-2.5/head_swap_ltx25_r128_v1.safetensors": "LTX-2.5",
    "ltx-2.5/head_swap_ltx25_r64_v1.1.safetensors": "LTX-2.5",
    "ltx-2.5/head_swap_ltx25_r64_v1.safetensors": "LTX-2.5",
    "wan22/headswap_bernini_r64_73f640_step3000_high_noise.safetensors": "Wan 2.2",
    "wan22/headswap_bernini_r64_73f640_step3000_low_noise.safetensors": "Wan 2.2",
}


def tensor(path, key):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = json.dumps({key: {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header + b"\x00" * 4)


def collection(directory):
    for index, name in enumerate(FILES):
        key = "diffusion_model.block.lora_A.weight" if index % 2 else "diffusion_model.block.lora_down.weight"
        tensor(directory / name, key)
    (directory / "README.md").write_text("---\nbase_model:\n- Lightricks/LTX-2.3\n- Lightricks/LTX-2.5\nlibrary_name: diffusers\n---\n")
    workflow = directory / "workflows/workflow_ltx2_head_swap_v3.json"
    workflow.parent.mkdir()
    workflow.write_text(json.dumps({"nodes": [{"type": "UNETLoader", "widgets_values": ["ltx-2.3-22b-distilled.safetensors"]}]}))
    (directory / "examples").mkdir()
    (directory / "examples/example.mp4").write_bytes(b"example, not a model")


def test_lora_tensor_headers_classify_all_twelve_nested_files_without_name_hints(tmp_path):
    collection(tmp_path)
    data = scan_model_artifacts(tmp_path)
    assert data["adapter_only"] is True
    assert data["is_video"] is True
    assert data["is_diffusion"] is True
    assert {f["rel_path"]: f["family"] for f in data["adapter_files"]} == FILES
    assert data["base_models"] == ["Lightricks/LTX-2.3", "Lightricks/LTX-2.5"]
    assert data["workflow_files"][0]["family"] == "LTX-2.3"
    for file in data["adapter_files"]:
        expected = ["Lightricks/" + file["family"]] if file["family"] in {"LTX-2.3", "LTX-2.5"} else []
        assert file["base_models"] == expected, "Never apply the repository's LTX base to H3/Wan adapters"


def test_base_weights_are_not_adapters_and_mixed_repos_can_still_launch(tmp_path):
    tensor(tmp_path / "model.safetensors", "transformer.block.attn.weight")
    assert scan_model_artifacts(tmp_path)["adapter_files"] == []
    tensor(tmp_path / "style.safetensors", "lora_unet_blocks_0.lora_up.weight")
    mixed = scan_model_artifacts(tmp_path)
    assert mixed["is_adapter"] is True
    assert mixed["adapter_only"] is False
    (tmp_path / "model.safetensors").unlink()
    (tmp_path / "model_index.json").write_text('{"_class_name":"FluxPipeline"}')
    assert scan_model_artifacts(tmp_path)["adapter_only"] is False


def test_truncated_or_oversized_headers_are_not_read_as_tensor_payloads(tmp_path):
    (tmp_path / "broken.safetensors").write_bytes(b"bad")
    (tmp_path / "oversized.safetensors").write_bytes(struct.pack("<Q", 2**60))
    assert scan_model_artifacts(tmp_path)["adapter_files"] == []


def test_generated_scanner_preserves_nested_paths_and_both_cache_roots(tmp_path):
    roots = [tmp_path / "linux", tmp_path / "windows"]
    repo = "Example/Head-Swap-Video"
    for root in roots:
        directory = root / "models--Example--Head-Swap-Video/snapshots/revision"
        directory.mkdir(parents=True)
        collection(directory)
        metadata_only = directory.parent / "older-metadata-only"
        metadata_only.mkdir()
        (metadata_only / "README.md").write_text("Older model card without downloaded weights")
    # No project imports can be resolved here: the remote scanner is standalone.
    result = subprocess.run([sys.executable, "-I", "-"],
                            input=_cached_model_scan_script([str(root) for root in roots]),
                            text=True, capture_output=True, check=True, cwd=tmp_path,
                            env={**os.environ, "HOME": str(tmp_path)})
    rows = [row for row in json.loads(result.stdout) if row["repo_id"] == repo]
    assert len(rows) == 2
    assert {row["path"] for row in rows} == {str(root) for root in roots}
    for row in rows:
        assert row["adapter_only"] is True
        for file in row["adapter_files"]:
            assert file["repo_path"] in FILES
            assert file["rel_path"] == "revision/" + file["repo_path"]
            assert (Path(row["path"]) / "models--Example--Head-Swap-Video/snapshots" / file["rel_path"]).is_file()


def test_plain_model_directory_gets_the_same_adapter_files(tmp_path):
    directory = tmp_path / "Head-Swap-Video"
    directory.mkdir()
    collection(directory)
    result = subprocess.run([sys.executable, "-I", "-"], input=_cached_model_scan_script([str(tmp_path)]),
                            text=True, capture_output=True, check=True, cwd=tmp_path,
                            env={**os.environ, "HOME": str(tmp_path)})
    row = next(row for row in json.loads(result.stdout) if row["repo_id"] == "Head-Swap-Video")
    assert row["is_local_dir"] and row["adapter_only"] and row["is_video"]
    assert {file["rel_path"] for file in row["adapter_files"]} == set(FILES)


def test_image_adapter_choices_use_exact_files_and_exclude_base_and_video_models():
    if not shutil.which("node"):
        pytest.skip("Node is required for adapter picker behavior tests")
    script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const context = vm.createContext({document:{addEventListener(){}},console});
const deps = {
 './ui.js':{default:{}},'./spinner.js':{default:{}},'./providers.js':{providerLogo(){}},
 './chatRenderer.js':{modelColor(){}},'./escMenuStack.js':{bindMenuDismiss(){},dismissOrRemove(){}},
 './cookbook-diagnosis.js':{openCookbookDependencies(){}},'./cookbook-hwfit.js':{_hwfitCache:null},
 './toolWindowZOrder.js':{topPortalZ(){}},'./cookbookGpu.js':{clearGpuMemory(){}},
 './h3Video.js':{isH3VideoComponent(){return false;},showH3Video(){}},
};
const source=fs.readFileSync('static/js/cookbookServe.js','utf8')+`
export { _adapterOptions, _cachedArtifactPath, _artifactRepoURL };
export function setModels(rows) { _cachedAllModels=rows; }
`;
const mod=new vm.SourceTextModule(source,{context});
await mod.link(name=>new vm.SyntheticModule(Object.keys(deps[name]),function(){
 for(const [key,value] of Object.entries(deps[name])) this.setExport(key,value);
},{context}));
await mod.evaluate();
const {setModels,_adapterOptions:options,_artifactRepoURL:url}=mod.namespace;
const adapters={repo_id:'Example/Styles',path:'/cache/hub',status:'ready',is_adapter:true,is_diffusion:true,
 adapter_files:[{rel_path:'rev/folder/first.safetensors'},{rel_path:'rev/folder/second.safetensors'}]};
setModels([
 {repo_id:'Example/Base',status:'ready',is_diffusion:true}, adapters,
 {repo_id:'Example/Video',status:'ready',is_adapter:true,is_diffusion:true,is_video:true},
 {repo_id:'Example/LLM-adapter',status:'ready',is_adapter:true},
]);
assert.deepEqual(Array.from(options('diff_lora','Example/Base'),row=>row.value),[
 '/cache/hub/models--Example--Styles/snapshots/rev/folder/first.safetensors',
 '/cache/hub/models--Example--Styles/snapshots/rev/folder/second.safetensors']);
assert.equal(options('diff_lora','Example/Styles').length,2,'Bundled adapters can be selected on their base model');
assert.deepEqual(Array.from(options('vllm_lora_modules'),row=>row.value),['Example/LLM-adapter']);
adapters.is_local_dir=true; adapters.path='/mounted/models';
assert.equal(options('diff_lora')[0].value,'/mounted/models/Example/Styles/rev/folder/first.safetensors');
const workflow={repo_path:'workflows/test with spaces.json',revision:'commit123'};
assert.equal(url(adapters,workflow),'','Local folders do not invent remote repo URLs');
adapters.is_local_dir=false;
assert.equal(url(adapters,workflow),'https://huggingface.co/Example/Styles/blob/commit123/workflows/test%20with%20spaces.json');
'''
    result = subprocess.run(["node", "--experimental-vm-modules", "--input-type=module", "-e", script],
                            capture_output=True, text=True, timeout=20,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr
