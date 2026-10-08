"""Portable transfers never execute jobs, escape paths, or buffer whole bundles."""
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

from src import h3_video as h3
from src import video_workflow as workflow


def weights(path, payload=b"0000"):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = json.dumps({"model.weight": {"dtype": "U8", "shape": [len(payload)], "data_offsets": [0, len(payload)]}}).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header + payload)
    return path


def manager(root):
    instance = h3.H3JobManager(root / "jobs", project=root, gallery_directory=root / "gallery")
    cache = root / "cache/huggingface/hub"
    instance.inventory = lambda: {"components": h3.discover_components([cache]), "gpus": [],
                                   "runtime_ready": False, "runtime_error": "No GPU needed for transfer", "defaults": {}}
    return instance


def document(**values):
    return {"format": workflow.FORMAT, "version": 1, "family": "h3",
            "config": {"mode": "t2va", "prompt": "Preserve exact dialogue: hello."},
            "components": {}, "attachments": []} | values


def archive(path, doc, entries):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("workflow.json", json.dumps(doc))
        for name, data in entries:
            output.writestr(name, data)
    return path


def transfer(tmp_path):
    service = workflow.WorkflowTransfers(manager(tmp_path / "target"), "h3")
    directory = service.stage("corey")
    return service, directory


def test_weight_roundtrip_is_portable_content_addressed_and_never_overwrites(tmp_path):
    source = manager(tmp_path / "source")
    original = weights(source.project / "cache/huggingface/hub/models--author--MiniMax-H3/snapshots/rev1/MiniMax_H3_FL2VA.safetensors")
    selected = source.inventory()["components"][0]
    exports = workflow.WorkflowTransfers(source, "h3")
    directory = exports.stage("corey")
    receipt = exports.finish_export(directory, "corey",
        {"mode": "t2va", "prompt": " Exact prompt\n", "gpu": "GPU-private", "vae_gpu": "GPU-other", "model": selected["id"]},
        source.inventory(), {}, workflow.export_options({"include_weights": True}))
    assert not list(directory.glob("*.safetensors")), "Export must not stage a second copy of weights"
    doc, handles = exports.export(directory.name, "corey")
    assert doc["config"] == {"mode": "t2va", "prompt": " Exact prompt\n"}
    assert doc["components"]["model"]["revision"] == "rev1"
    raw = b"".join(workflow.stream_zip(doc, handles))
    assert b"GPU-private" not in raw and str(source.project).encode() not in raw
    service, destination = transfer(tmp_path)
    existing = weights(service.cache / "models--author--MiniMax-H3/snapshots/rev1/MiniMax_H3_FL2VA.safetensors", b"existing")
    upload = destination / "upload.zip"
    upload.write_bytes(raw)
    result = service.import_file(destination, "corey", upload)
    assert existing.read_bytes().endswith(b"existing")
    identifier = result["resolved_components"]["model"]
    imported = next(c for c in result["inventory"]["components"] if c["id"] == identifier)
    assert Path(imported["path"]).read_bytes() == original.read_bytes()
    assert "odysseus-" in imported["path"] and service.cache in Path(imported["path"]).parents
    assert result["workflow"]["config"] == doc["config"]
    # A -> B (weights), then B -> C (references only) preserves the actual HF
    # commit rather than publishing B's synthetic local-cache revision.
    provenance = workflow.component_reference(imported["path"])
    assert provenance == {"name": original.name, "repository": "author/MiniMax-H3",
                          "relative_path": original.name, "revision": "rev1"}
    third = manager(tmp_path / "third")
    weights(third.project / "cache/huggingface/hub/models--author--MiniMax-H3/snapshots/rev1" / original.name)
    service_c = workflow.WorkflowTransfers(third, "h3")
    directory_c = service_c.stage("corey")
    portable, _ = workflow.make_recipe(result["workflow"]["config"], {"model": imported["path"]}, {}, "h3", workflow.export_options({}))
    json_file = directory_c / "workflow.json"
    json_file.write_text(json.dumps(portable))
    restored = service_c.import_file(directory_c, "corey", json_file)
    assert "model" in restored["resolved_components"]


@pytest.mark.parametrize("change", [
    {"version": 2}, {"family": "bfs"}, {"config": {"gpu": "GPU-elsewhere"}},
    {"config": {"prompt": {"execute": "python"}}},
    {"components": {"model": {"name": "../model.safetensors"}}},
    {"components": {"model": {"name": "model.safetensors", "repository": "../../escape"}}},
    {"components": {"model": {"name": "model.safetensors", "relative_path": "/tmp/model.safetensors"}}},
    {"components": {"model": {"name": "model.safetensors", "path": "/etc/passwd"}}},
])
def test_hostile_json_rejected_before_any_model_publication(tmp_path, change):
    service, directory = transfer(tmp_path)
    upload = directory / "upload.zip"
    upload.write_text(json.dumps(document(**change)))
    with pytest.raises(ValueError):
        service.import_file(directory, "corey", upload)
    assert not service.cache.exists()


@pytest.mark.parametrize("name", ["../outside.py", "/tmp/outside", "weights/../outside", "attachments\\evil", "run.py"])
def test_zip_path_and_undeclared_executable_rejection(tmp_path, name):
    service, directory = transfer(tmp_path)
    upload = archive(directory / "upload.zip", document(), [(name, b"execute")])
    with pytest.raises(ValueError):
        service.import_file(directory, "corey", upload)
    assert not service.cache.exists()


def test_symlink_archive_member_rejected(tmp_path):
    service, directory = transfer(tmp_path)
    item = zipfile.ZipInfo("attachments/link.png")
    item.create_system = 3
    item.external_attr = (stat.S_IFLNK | 0o777) << 16
    upload = archive(directory / "upload.zip", document(), [(item, b"/etc/passwd")])
    with pytest.raises(ValueError, match="non-regular"):
        service.import_file(directory, "corey", upload)


def test_invalid_later_weight_does_not_publish_earlier_valid_weight(tmp_path):
    service, directory = transfer(tmp_path)
    valid = weights(tmp_path / "MiniMax_H3_FL2VA.safetensors").read_bytes()
    doc = document(components={
        "model": {"name": "MiniMax_H3_FL2VA.safetensors", "archive_path": "weights/model/MiniMax_H3_FL2VA.safetensors"},
        "encoder": {"name": "qwen_h3.safetensors", "archive_path": "weights/encoder/qwen_h3.safetensors"}})
    upload = archive(directory / "upload.zip", doc, [
        ("weights/model/MiniMax_H3_FL2VA.safetensors", valid),
        ("weights/encoder/qwen_h3.safetensors", b"not a tensor file")])
    with pytest.raises(ValueError, match="safetensors"):
        service.import_file(directory, "corey", upload)
    assert not service.cache.exists()


def test_existing_content_addressed_path_is_never_overwritten(tmp_path):
    service, directory = transfer(tmp_path)
    valid = weights(tmp_path / "MiniMax_H3_FL2VA.safetensors").read_bytes()
    digest = hashlib.sha256(valid).hexdigest()
    target = service.cache / "models--odysseus-imports--h3/snapshots" / ("odysseus-" + digest[:32]) / "MiniMax_H3_FL2VA.safetensors"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep-existing")
    doc = document(components={"model": {"name": target.name, "archive_path": "weights/model/" + target.name}})
    upload = archive(directory / "upload.zip", doc, [("weights/model/" + target.name, valid)])
    with pytest.raises(ValueError, match="not overwritten"):
        service.import_file(directory, "corey", upload)
    assert target.read_bytes() == b"keep-existing"


def test_inflated_size_checked_before_extraction(tmp_path, monkeypatch):
    service, directory = transfer(tmp_path)
    name = "MiniMax_H3_FL2VA.safetensors"
    data = weights(tmp_path / name, b"\0" * 100_000).read_bytes()
    doc = document(components={"model": {"name": name, "archive_path": "weights/model/" + name}})
    upload = archive(directory / "upload.zip", doc, [("weights/model/" + name, data)])
    assert upload.stat().st_size < 2000
    monkeypatch.setattr(workflow.shutil, "disk_usage", lambda _: SimpleNamespace(free=workflow.RESERVE_BYTES + 4000))
    with pytest.raises(OSError, match="disk space"):
        service.import_file(directory, "corey", upload)
    assert not (directory / "weights").exists()


def test_excessive_zip_directory_rejected_before_zipfile_allocates_entries(tmp_path, monkeypatch):
    service, directory = transfer(tmp_path)
    upload = archive(directory / "upload.zip", document(), [])
    raw = bytearray(upload.read_bytes())
    end = raw.rfind(b"PK\x05\x06")
    struct.pack_into("<H", raw, end + 10, workflow.MAX_ENTRIES + 1)
    upload.write_bytes(raw)
    monkeypatch.setattr(workflow.zipfile, "ZipFile", lambda *_: pytest.fail("Excessive directory parsed"))
    with pytest.raises(ValueError, match="directory is too large"):
        service.import_file(directory, "corey", upload)


def test_json_references_only_and_ambiguous_names_do_not_autoselect(tmp_path):
    service, directory = transfer(tmp_path)
    for repo in ("one", "two"):
        weights(service.cache / ("models--" + repo + "--MiniMax-H3") / "snapshots/rev/MiniMax_H3_FL2VA.safetensors")
    upload = directory / "upload.zip"
    upload.write_text(json.dumps(document(components={"model": {"name": "MiniMax_H3_FL2VA.safetensors"}})))
    result = service.import_file(directory, "corey", upload)
    assert result["resolved_components"] == {} and result["attachments"] == []


def test_json_extracted_from_bundle_restores_draft_and_warns_files_not_present(tmp_path):
    service, directory = transfer(tmp_path)
    doc = document(config={"mode": "fl2va", "prompt": "Preserve this draft"},
        components={"model": {"name": "MiniMax_H3_FL2VA.safetensors", "archive_path": "weights/model/MiniMax_H3_FL2VA.safetensors"}},
        attachments=[{"field": "first_frame", "name": "first.png", "path": "attachments/first.png", "size": 123}])
    upload = directory / "upload.zip"
    upload.write_text(json.dumps(doc))
    result = service.import_file(directory, "corey", upload)
    assert result["workflow"]["config"] == doc["config"]
    assert result["attachments"] == result["workflow"]["attachments"] == []
    assert "archive_path" not in result["workflow"]["components"]["model"]
    assert "Import the ZIP" in result["warnings"][0]
    assert not service.cache.exists(), "JSON never installs missing weights"


def test_owner_and_expiry_guards_cover_export_tickets_and_import_attachments(tmp_path):
    service, directory = transfer(tmp_path)
    service.finish_export(directory, "corey", {"mode": "t2va", "prompt": "draft"},
                          {"components": []}, {}, workflow.export_options({}))
    with pytest.raises(FileNotFoundError):
        service.export(directory.name, "other")
    receipt = h3._read(directory / "receipt.json")
    receipt["expires"] = time.time() - 1
    h3._write(directory / "receipt.json", receipt)
    with pytest.raises(FileNotFoundError):
        service.export(directory.name, "corey")
    service.cleanup()
    assert not directory.exists()


def test_stream_cancellation_closes_sources_without_reading_full_weights():
    class Source:
        closed = threading.Event()
        count = 0
        def read(self, size):
            assert size <= workflow.CHUNK
            self.count += 1
            return b"x" * size if self.count <= 100 else b""
        def close(self):
            self.closed.set()
    source = Source()
    iterator = workflow.stream_zip(document(), [({"archive_path": "weights/model/large.safetensors", "size": 100 * workflow.CHUNK}, source)])
    next(iterator)
    iterator.close()
    assert source.closed.wait(2)
    assert source.count < 10, "A disconnected client must not trigger a full weight read"


@pytest.mark.parametrize("failure", ["shrink", "grow", "read_error"])
def test_failed_stream_never_finishes_a_valid_archive(failure):
    class Source:
        def __init__(self, values):
            self.values = iter(values)
            self.closed = threading.Event()
        def read(self, size):
            value = next(self.values)
            if isinstance(value, Exception):
                raise value
            return value
        def close(self):
            self.closed.set()

    # In the growth case all expected bytes were already emitted before the
    # extra byte is discovered. ZIP cleanup must still withhold its end record.
    values = {"shrink": [b"abc", b""], "grow": [b"abcdef", b"x", b""],
              "read_error": [b"abc", OSError("source read failed")]}
    source = Source(values[failure])
    unread = Source([b"unused", b""])
    handles = [({"archive_path": "weights/model/source.safetensors", "size": 6}, source),
               ({"archive_path": "weights/encoder/unused.safetensors", "size": 6}, unread)]
    chunks = []
    error, message = (OSError, "source read failed") if failure == "read_error" else (ValueError, "changed during download")
    with pytest.raises(error, match=message):
        for chunk in workflow.stream_zip(document(), handles):
            chunks.append(chunk)
    raw = b"".join(chunks)
    assert raw and b"PK\x05\x06" not in raw
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(raw))
    assert source.closed.wait(2) and unread.closed.wait(2)


def test_active_job_export_does_not_deadlock_or_include_local_paths(tmp_path):
    owner = manager(tmp_path)
    directory = owner.stage()
    h3._write(directory / "state.json", {"id": directory.name, "owner": "corey", "status": "queued", "created_at": time.time()})
    h3._write(directory / "manifest.json", {"config": {"mode": "t2va", "prompt": "queued", "gpu": "GPU-private",
        "model": "/missing/models--author--MiniMax-H3/snapshots/rev/MiniMax_H3_FL2VA.safetensors"}})
    done, result = threading.Event(), []
    def export():
        try:
            result.append(workflow.job_export(owner, directory.name, "corey", "h3", workflow.export_options({})))
        finally:
            done.set()
    threading.Thread(target=export, daemon=True).start()
    assert done.wait(2), "An active-job export must not reacquire its own job.lock"
    doc, handles = result[0]
    assert handles == [] and doc["config"] == {"mode": "t2va", "prompt": "queued"}
    assert doc["components"]["model"]["repository"] == "author/MiniMax-H3"
