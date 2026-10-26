"""Job search scopes pagination and bulk selection without hydrating misses."""
import pytest

from src import h3_video as h3
from tests.test_video_submission import backend
from tests.test_video_job_pagination import saved_job
from tests.test_h3_seed_hunter_api import hunter, preview
from tests.test_video_job_groups import refine


def describe(directory, *, name="Coast clip.mkv", prompt="A sunset by the sea", project=None):
    manifest = h3._read(directory / "manifest.json")
    manifest.update(source_name=name, input_names={"reference_videos": [name, "Additional reference.mov"]})
    manifest["config"]["prompt"] = prompt
    if project:
        manifest["project_exports"] = [{"project": project, "candidate": 1}]
    h3._write(directory / "manifest.json", manifest)


@pytest.mark.parametrize("family", ["h3", "bfs"])
def test_search_filters_before_pagination_with_anchored_all_page_selection(backend, monkeypatch, family):
    manager = backend.managers[family]
    matching = []
    for index in range(1, 34):
        directory = saved_job(manager, index)
        describe(directory, name="Coast clip.mkv" if index % 2 else "Mountain clip.mkv")
        if index % 2:
            matching.append(directory.name)
    foreign = saved_job(manager, 40, owner="other")
    describe(foreign)
    calls = []
    original_view, original_read = manager.view, h3._read
    def view(identifier, *args, **kwargs):
        calls.append(identifier)
        return original_view(identifier, *args, **kwargs)
    def read(path, *args, **kwargs):
        assert path != foreign / "manifest.json", "Never inspect another owner's searchable metadata"
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(manager, "view", view)
    monkeypatch.setattr(h3, "_read", read)
    client = backend.client()
    url = f"/api/video/{family}/jobs"
    params = {"q": "  COAST   sunset ", "page": 1, "page_size": 5}
    first = client.get(url, params=params).json()
    assert first["search_query"] == params["q"].strip()
    assert first["pagination"]["total"] == 17
    assert first["pagination"]["total_pages"] == 4
    assert first["counts"]["completed"] == first["queue"]["counts"]["completed"] == 33
    assert len(calls) == 5
    params["before"] = first["pagination"]["before"]
    newest = saved_job(manager, 45, created_at=params["before"] + 1)
    describe(newest)
    seen = [row["id"] for row in first["jobs"]]
    for page in range(2, 5):
        params["page"] = page
        result = client.get(url, params=params).json()
        assert result["pagination"]["total"] == 17
        seen.extend(row["id"] for row in result["jobs"])
    assert seen == list(reversed(matching))
    assert calls == seen
    assert newest.name not in seen


@pytest.mark.parametrize("query", ["coast", "SUNSET", "additional reference", "mix project", "completed", "00000000000000000000000000000001"])
def test_searchable_display_fields_and_unpaged_compatibility(backend, query):
    manager = backend.managers["h3"]
    directory = saved_job(manager, 1)
    describe(directory, project="Mix project")
    response = backend.client().get("/api/video/h3/jobs", params={"q": query})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["jobs"]] == [directory.name]
    assert "pagination" not in response.json()


def test_search_empty_literal_unicode_and_query_validation(backend):
    directory = saved_job(backend.managers["h3"], 1)
    describe(directory, name="Straße clip.mkv", prompt="Literal [100%] recording")
    client = backend.client()
    url = "/api/video/h3/jobs"
    for query in ["", " \t ", "STRASSE", "[100%]"]:
        assert len(client.get(url, params={"q": query, "page": 1}).json()["jobs"]) == 1
    for query in ["no match", ".*", "sunset sea"]:
        body = client.get(url, params={"q": query, "page": 8}).json()
        assert body["jobs"] == []
        assert body["pagination"]["total"] == 0
        assert body["pagination"]["page"] == 1
    assert client.get(url, params={"q": "x" * 513}).status_code == 400
    assert client.get(url, params={"q": "x" * 512}).status_code == 200
    assert client.get(url, params={"q": "clip"}, headers={"x-user": "reader"}).status_code == 403


def test_group_search_matches_child_project_and_keeps_whole_parent_context(hunter):
    job, directory = preview(hunter)
    children = [refine(hunter, job, candidate) for candidate in (1, 2)]
    child_directory = hunter.managers["h3"].directory(children[0]["id"])
    describe(child_directory, project="Coastal collection")
    client = hunter.client()
    query = {"recipe": "seed_hunter", "group_refinements": "true", "stage": "not_scout", "page": 1, "q": "coastal collection"}
    body = client.get("/api/video/h3/jobs", params=query).json()
    assert body["pagination"]["total"] == 1
    assert [row["id"] for row in body["jobs"]] == [job["id"]]
    assert {row["id"] for row in body["jobs"][0]["refinements"]} == {row["id"] for row in children}
    # Select all uses the same query at a larger page size.
    all_matches = client.get("/api/video/h3/jobs", params={**query, "page_size": 50}).json()
    assert [row["id"] for row in all_matches["jobs"]] == [job["id"]]
    foreign = client.get("/api/video/h3/jobs", params=query, headers={"x-user": "other"}).json()
    assert foreign["jobs"] == []
    assert body["queue"]["queued"] == 2


def test_search_respects_recipe_and_scout_stage(backend):
    manager = backend.managers["h3"]
    normal = saved_job(manager, 1)
    hunter_job = saved_job(manager, 2, recipe="seed_hunter")
    scout_job = saved_job(manager, 3, recipe="seed_hunter")
    for directory in [normal, hunter_job, scout_job]:
        describe(directory)
    manifest = h3._read(scout_job / "manifest.json")
    manifest["config"]["seed_hunter"]["stage"] = "scout"
    h3._write(scout_job / "manifest.json", manifest)
    for stage, expected in [("scout", scout_job.name), ("not_scout", hunter_job.name)]:
        body = backend.client().get("/api/video/h3/jobs", params={"recipe": "seed_hunter", "stage": stage, "q": "coast", "page": 1}).json()
        assert [row["id"] for row in body["jobs"]] == [expected]
