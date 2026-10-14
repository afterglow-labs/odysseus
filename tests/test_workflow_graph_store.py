import copy

import pytest

from src.workflow_graph import new_document
from src.workflow_graph_store import GraphDocumentStore, GraphRevisionConflict


def test_document_roundtrip_preserves_wires_unknown_nodes_disabled_states_and_source(tmp_path):
    store = GraphDocumentStore(tmp_path)
    doc = new_document(template="media")
    doc["nodes"][1]["state"] = "bypassed"
    doc["nodes"].append({"id": "custom", "type": "unimplemented.node", "params": {"future": 17},
                         "position": {"x": 10, "y": 20}, "state": "disabled"})
    doc["source"] = {"format": "comfyui", "document": {"nodes": [], "future": [1, 2, 3]}}
    saved = store.save("corey", doc)
    assert store.get("corey", saved["id"])["document"] == doc
    doc["nodes"][0]["params"]["name"] = "changed-after-save.mp4"
    assert store.get("corey", saved["id"])["document"] != doc
    with pytest.raises(FileNotFoundError):
        store.get("other", saved["id"])
    assert store.list("other")["documents"] == []


def test_concurrent_edit_requires_expected_revision_and_delete_keeps_other_documents(tmp_path):
    store = GraphDocumentStore(tmp_path)
    first = store.save("corey", new_document(template="media"))
    other = store.save("corey", new_document(template="blank"))
    edited = copy.deepcopy(first["document"])
    edited["title"] = "My graph"
    second = store.save("corey", edited, first["id"], first["revision"])
    assert second["revision"] == 2
    with pytest.raises(GraphRevisionConflict):
        store.save("corey", first["document"], first["id"], first["revision"])
    with pytest.raises(GraphRevisionConflict):
        store.save("corey", first["document"], first["id"])
    with pytest.raises(FileNotFoundError):
        store.delete("other", first["id"])
    store.delete("corey", first["id"])
    assert [row["id"] for row in store.list("corey")["documents"]] == [other["id"]]


def test_document_paths_and_invalid_graph_do_not_publish(tmp_path):
    store = GraphDocumentStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.get("corey", "../../anything")
    doc = new_document(template="media")
    doc["nodes"][0]["position"]["x"] = float("nan")
    with pytest.raises(ValueError):
        store.save("corey", doc)
    assert store.list("corey")["total"] == 0
