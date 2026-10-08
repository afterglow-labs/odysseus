import hashlib
import json
import os
from pathlib import Path

import pytest

from src import model_library_migration as migration


def make_cache(root, repo="test/other", revision="a" * 40, filename="model.gguf", payload=b"model payload", *, tree=True):
    directory = root / ("models--" + repo.replace("/", "--"))
    digest = hashlib.sha256(payload).hexdigest()
    blob = root / "blobs" / digest[:2] / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)
    snapshot = directory / "snapshots" / revision / filename
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.symlink_to(blob)
    if tree:
        tree_path = directory / "trees" / (revision + ".json")
        tree_path.parent.mkdir(parents=True, exist_ok=True)
        tree_path.write_text(json.dumps({"files": {filename: {"size": len(payload), "lfs_sha256": digest, "xet_hash": digest}}}))
    (directory / "refs").mkdir(exist_ok=True)
    (directory / "refs/main").write_text(revision)
    return snapshot, blob


def test_h3_classification_keeps_encoder_and_splits_mixed_bfs():
    assert migration.h3_file("sakamakismile/Qwen3-VL-32B-Heretic-MiniMax-H3-NVFP4", "encoder.safetensors")
    assert migration.h3_file("Abiray/MiniMax-H3-Pruned-GGUF", "model.gguf")
    assert migration.h3_file("Alissonerdx/BFS-Best-Face-Swap-Video", "h3/head.safetensors")
    assert not migration.h3_file("Alissonerdx/BFS-Best-Face-Swap-Video", "ltx-2.5/head.safetensors")
    assert not migration.h3_file("DavidAU/Qwen3", "model.gguf")


def test_plan_recovers_blob_without_snapshot_and_leaves_unknown_data(tmp_path):
    root = tmp_path / "hub"
    snapshot, blob = make_cache(root)
    snapshot.unlink()
    unknown = root / "blobs/unknown"
    unknown.write_bytes(b"unknown")
    partial = root / "blobs/partial.incomplete"
    partial.write_bytes(b"partial")
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    assert len(plan["files"]) == 1
    assert str(blob) in plan["files"][0]["source_paths"]
    assert str(unknown) in {entry["path"] for entry in plan["unmapped"]}
    assert partial.exists()


def test_same_filesystem_migration_preserves_old_aliases_and_inode(tmp_path):
    root = tmp_path / "hub"
    snapshot, blob = make_cache(root, repo="test/MiniMax-H3")
    inode = blob.stat().st_ino
    backup = tmp_path / "backup/blob"
    backup.parent.mkdir()
    os.link(blob, backup)
    plan = migration.build_plan([root, backup.parent], tmp_path / "linux", tmp_path / "external")
    row = plan["files"][0]
    journal = migration.execute_plan(plan, tmp_path / "journal.json")
    destination = Path(row["destination"])
    assert destination.is_file() and not destination.is_symlink()
    assert destination.stat().st_ino == inode
    assert blob.is_symlink() and backup.is_symlink()
    assert snapshot.read_bytes() == destination.read_bytes() == b"model payload"
    metadata = json.loads((Path(row["directory"]) / ".odysseus-model.json").read_text())
    assert str(blob) in metadata["files"]["model.gguf"]["source_paths"]
    assert journal["files"][row["id"]]["status"] == "complete"
    # Resume does not rehash completed, unchanged files.
    def fail(*args, **kwargs):
        pytest.fail("Unchanged completed row was processed again")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(migration, "_digest", fail)
        migration.execute_plan(plan, tmp_path / "journal.json")


def test_file_without_tree_metadata_is_preserved(tmp_path):
    root = tmp_path / "hub"
    snapshot, _ = make_cache(root, repo="lightx2v/Minimax-h3-Turbo", tree=False)
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    assert plan["files"][0]["storage"] == "linux"
    migration.execute_plan(plan, tmp_path / "journal.json")
    assert snapshot.read_bytes() == b"model payload"


def test_wrong_expected_digest_never_replaces_sources(tmp_path):
    root = tmp_path / "hub"
    snapshot, blob = make_cache(root)
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    plan["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        migration.execute_plan(plan, tmp_path / "journal.json")
    assert not blob.is_symlink()
    assert snapshot.read_bytes() == b"model payload"
    assert not Path(plan["files"][0]["destination"]).exists()


def test_destination_conflict_preserves_both_files(tmp_path):
    root = tmp_path / "hub"
    _, blob = make_cache(root)
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    destination = Path(plan["files"][0]["destination"])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"other payload")
    with pytest.raises(ValueError, match="differs"):
        migration.execute_plan(plan, tmp_path / "journal.json")
    assert destination.read_bytes() == b"other payload"
    assert blob.read_bytes() == b"model payload" and not blob.is_symlink()


def test_previous_revision_gets_separate_named_directory(tmp_path):
    root = tmp_path / "hub"
    make_cache(root, revision="a" * 40, payload=b"old revision")
    make_cache(root, revision="b" * 40, payload=b"new revision")
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    assert len({row["destination"] for row in plan["files"]}) == 2
    older = next(row for row in plan["files"] if row["revision"] == "a" * 40)
    assert "--revision-aaaaaaaaaaaa" in older["destination"]
    migration.execute_plan(plan, tmp_path / "journal.json")


def test_unsupported_symlink_keeps_source(tmp_path, monkeypatch):
    root = tmp_path / "hub"
    _, blob = make_cache(root)
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    monkeypatch.setattr(migration, "_can_symlink", lambda directory: False)
    journal = migration.execute_plan(plan, tmp_path / "journal.json")
    assert not blob.is_symlink()
    assert str(blob) in journal["files"][plan["files"][0]["id"]]["retained_sources"]


def test_copy_resumes_verified_prefix_and_checks_readback(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "RESERVE_BYTES", 0)
    source = tmp_path / "source"
    source.write_bytes(b"0123456789" * 100)
    destination = tmp_path / "model.gguf"
    partial = tmp_path / ".model.gguf.odysseus-migration-partial"
    partial.write_bytes(source.read_bytes()[:61])
    result, digest = migration._copy_resumable(source, destination)
    assert result == partial
    assert result.read_bytes() == source.read_bytes()
    assert digest == hashlib.sha256(source.read_bytes()).hexdigest()


def test_copy_rejects_different_prefix_without_removing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "RESERVE_BYTES", 0)
    source = tmp_path / "source"
    source.write_bytes(b"0123456789")
    (tmp_path / ".model.gguf.odysseus-migration-partial").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="prefix differs"):
        migration._copy_resumable(source, tmp_path / "model.gguf")
    assert source.read_bytes() == b"0123456789"


def test_path_traversal_metadata_is_ignored(tmp_path):
    root = tmp_path / "hub"
    make_cache(root)
    tree = next(root.glob("models--*/trees/*.json"))
    data = json.loads(tree.read_text())
    data["files"]["../../outside.gguf"] = next(iter(data["files"].values()))
    tree.write_text(json.dumps(data))
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    assert len(plan["files"]) == 1
    assert plan["warnings"]


def test_unmounted_destination_fails_without_moving_source(tmp_path):
    root = tmp_path / "hub"
    _, blob = make_cache(root)
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    plan["storage_devices"]["external"] = -1
    with pytest.raises(OSError, match="not mounted"):
        migration.execute_plan(plan, tmp_path / "journal.json")
    assert not blob.is_symlink()
    assert not (tmp_path / "external").exists()


def test_cross_filesystem_copy_with_verified_alias_replacement(tmp_path, monkeypatch):
    import errno
    root = tmp_path / "hub"
    snapshot, blob = make_cache(root)
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    def no_links(*args, **kwargs):
        raise OSError(errno.EXDEV, "Cross-device link")
    monkeypatch.setattr(os, "link", no_links)
    monkeypatch.setattr(migration, "RESERVE_BYTES", 0)
    migration.execute_plan(plan, tmp_path / "journal.json")
    assert blob.is_symlink()
    destination = Path(plan["files"][0]["destination"])
    assert not destination.is_symlink()
    assert destination.read_bytes() == snapshot.read_bytes() == b"model payload"


def test_atomic_publish_never_overwrites_concurrent_destination(tmp_path):
    source, destination = tmp_path / "temporary", tmp_path / "destination"
    source.write_bytes(b"migrated")
    destination.write_bytes(b"concurrent download")
    with pytest.raises(FileExistsError):
        migration._publish_no_replace(source, destination)
    assert destination.read_bytes() == b"concurrent download"
    assert source.read_bytes() == b"migrated"


def test_malformed_repository_directory_cannot_escape_destination(tmp_path):
    root = tmp_path / "hub"
    directory = root / "models--..--escaped" / "snapshots" / ("a" * 40)
    directory.mkdir(parents=True)
    (directory / "model.gguf").write_bytes(b"test")
    plan = migration.build_plan([root], tmp_path / "linux", tmp_path / "external")
    assert plan["files"] == []
    assert plan["warnings"]
