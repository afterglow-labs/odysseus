"""Saved executable workflows; the same graph API serves every editor."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from routes.h3_video_routes import _owner, _limited_request
from src.constants import DATA_DIR
from src.h3_video import H3JobManager
from src.bfs_video import BFSJobManager
from src.upload_limits import get_chat_upload_max_bytes
from src.video_server_files import open_server_video_preview
from src.video_submission import complete_operation, store_upload_chunk, UploadSpaceError
from src.workflow_graph_store import GraphDocumentStore, GraphRevisionConflict

JSON_LIMIT = 4 * 1024 * 1024
EXTENSIONS = {
    "video": {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp"},
    "audio": {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"},
    "refmod": {".safetensors"},
}


def parse_json(raw):
    def reject(_):
        raise ValueError("Workflow numbers must be finite")
    return json.loads(raw, parse_constant=reject)


async def json_body(request, *, limit=JSON_LIMIT):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, f"JSON exceeds {limit // (1024 * 1024)} MiB")
    value = parse_json(body)
    if not isinstance(value, dict):
        raise ValueError("Supply a JSON object")
    return value


@asynccontextmanager
async def stage_bindings(root, document, bindings, form, limit):
    """Convert only uploaded bytes or approved server descriptors to trusted paths."""
    if not isinstance(bindings, dict) or len(bindings) > 64:
        raise ValueError("Choose up to 64 media inputs")
    nodes = {node["id"]: node for node in document["nodes"]}
    from src.workflow_graph import node_registry
    registry = node_registry()
    count = sum(len(rows) for rows in bindings.values() if isinstance(rows, list))
    if count > 64:
        raise ValueError("Choose up to 64 media files per workflow run")
    directory = Path(root) / ".uploads"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="graph-", dir=directory) as temporary:
        media, total = {}, 0
        for identity, rows in bindings.items():
            if not isinstance(identity, str):
                raise ValueError("Media binding IDs must be strings")
            node = nodes.get(identity)
            kind = node.get("type", "").split(".")[-1] if node else None
            native = node and node["type"] in {"input.video", "input.image", "input.audio", "input.refmod"}
            if not native:
                node_id, separator, parameter = identity.rpartition('::')
                node = nodes.get(node_id) if separator else None
                descriptor = registry.get(node['type'], {}) if node else {}
                fields = [p for p in descriptor.get('params',[]) if p.get('id')==parameter and p.get('editor')=='media']
                kind = fields[0].get('media_kind') if len(fields)==1 and descriptor.get('execution')=='comfy' else None
            if kind not in EXTENSIONS:
                raise ValueError("Media must be attached to an input node")
            if not isinstance(rows, list) or not rows or len(rows) > 12:
                raise ValueError("Each media input needs 1–12 files")
            if not native and len(rows) != 1:
                raise ValueError("Choose one file per node media field")
            media[identity] = []
            for row in rows:
                if not isinstance(row, dict) or set(row) not in ({"upload"}, {"server"}):
                    raise ValueError("Choose an uploaded file or a server file")
                owned = "server" in row
                if owned:
                    if kind != "video":
                        raise ValueError("Server browsing currently selects video files")
                    selection = row["server"]
                    # Pickers may wrap their descriptor; never accept a raw file path.
                    if isinstance(selection, dict) and "server_file" in selection:
                        selection = selection["server_file"]
                    upload = await asyncio.to_thread(open_server_video_preview, selection)
                else:
                    key = row["upload"]
                    if not isinstance(key, str) or len(key) > 100:
                        raise ValueError("Invalid upload field")
                    values = form.getlist(key) if form is not None else []
                    if len(values) != 1 or not isinstance(values[0], UploadFile):
                        raise ValueError("An input file needs to be attached again")
                    upload = values[0]
                try:
                    name = str(upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
                    suffix = Path(name).suffix.lower()
                    if suffix not in EXTENSIONS[kind]:
                        raise ValueError(f"Choose a supported {kind} file")
                    if type(upload.size) is int and total + upload.size > limit:
                        raise HTTPException(413, "Workflow inputs exceed the configured upload limit")
                    target = Path(temporary) / (uuid.uuid4().hex + suffix)
                    await upload.seek(0)
                    with target.open("xb") as stream:
                        target.chmod(0o600)
                        while chunk := await upload.read(1024 * 1024):
                            total += len(chunk)
                            if total > limit:
                                raise HTTPException(413, "Workflow inputs exceed the configured upload limit")
                            await store_upload_chunk(stream, directory, chunk)
                    if not target.stat().st_size:
                        raise ValueError("An input file is empty")
                    if kind == "refmod":
                        from src.video_workflow import validate_safetensors
                        await asyncio.to_thread(validate_safetensors, target)
                    if hasattr(upload, "check_unchanged"):
                        await asyncio.to_thread(upload.check_unchanged)
                    media[identity].append({"path": str(target), "name": name[:255]})
                finally:
                    if owned:
                        await upload.close()
        yield media


def setup_workflow_graph_routes(store=None, runs=None, managers=None):
    from src.workflow_graph import catalog, compile_graph, new_document, validate_document
    from src.workflow_graph_import import import_comfyui, from_native_config
    from src.workflow_graph_setup import configure_workflow
    from src.workflow_graph_runs import GraphRunManager
    from src.comfy_workflow import ComfyJobManager
    managers = managers or {"h3": H3JobManager(), "bfs": BFSJobManager(), "comfy": ComfyJobManager()}
    root = Path(DATA_DIR) / "workflow_graphs"
    store = store or GraphDocumentStore(root / "documents")
    runs = runs or GraphRunManager(root / "runs", managers)
    router = APIRouter(prefix="/api/workflow-graphs", tags=["workflows"])

    @router.on_event("startup")
    async def recover():
        await asyncio.to_thread(runs.recover)

    async def inventories():
        values = await asyncio.gather(*(asyncio.to_thread(manager.submission_inventory) for manager in managers.values()))
        return dict(zip(managers, values))

    def failure(exc):
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, FileNotFoundError):
            return HTTPException(404, str(exc))
        if isinstance(exc, GraphRevisionConflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, UploadSpaceError):
            return HTTPException(507, str(exc))
        if isinstance(exc, RuntimeError):
            return HTTPException(409, str(exc))
        return HTTPException(400, str(exc) if isinstance(exc, ValueError) else "Invalid workflow data")

    @router.get("/catalog")
    async def get_catalog(request: Request):
        _owner(request)
        data = catalog()
        available = await inventories()
        templates = []
        for row in data.get("templates", []):
            document, notes = configure_workflow(new_document(title=row["title"], template=row["id"]),
                                                 available, template=True)
            templates.append({**row, "document": document, "setup_notes": notes})
        return {**data, "templates": templates, "inventory": available}

    @router.get("/documents")
    async def documents(request: Request, offset: int = 0, limit: int = 50):
        try:
            return await asyncio.to_thread(store.list, _owner(request), offset, limit)
        except (ValueError, FileNotFoundError) as exc:
            raise failure(exc) from exc

    @router.post("/documents")
    async def save_document(request: Request):
        owner = _owner(request)
        try:
            body = await json_body(request)
            if set(body) - {"document", "id", "revision"} or "document" not in body:
                raise ValueError("Supply a workflow document and optional saved ID/revision")
            return await asyncio.to_thread(store.save, owner, body["document"], body.get("id"), body.get("revision"))
        except (ValueError, TypeError, RecursionError, FileNotFoundError) as exc:
            raise failure(exc) from exc

    @router.get("/documents/{identity}")
    async def get_document(identity: str, request: Request):
        try:
            return await asyncio.to_thread(store.get, _owner(request), identity)
        except FileNotFoundError as exc:
            raise failure(exc) from exc

    @router.delete("/documents/{identity}")
    async def delete_document(identity: str, request: Request):
        try:
            return await asyncio.to_thread(store.delete, _owner(request), identity)
        except FileNotFoundError as exc:
            raise failure(exc) from exc

    @router.post("/validate")
    async def validate(request: Request):
        _owner(request)
        try:
            body = await json_body(request)
            if set(body) != {"document"}:
                raise ValueError("Supply a workflow document")
            validate_document(body["document"])
            available = await inventories()
            compiled = await asyncio.to_thread(compile_graph, body["document"], inventory=available)
            return await asyncio.to_thread(runs.validate_plan, compiled, available)
        except (ValueError, TypeError, RecursionError) as exc:
            raise failure(exc) from exc

    @router.post("/import")
    async def import_document(request: Request):
        _owner(request)
        try:
            body = await json_body(request)
            if set(body) - {"workflow", "title"} or "workflow" not in body:
                raise ValueError("Supply exported workflow JSON")
            value = body["workflow"]
            available = await inventories()
            if isinstance(value, dict) and value.get("format") == "odysseus-workflow-graph":
                validate_document(value)
                result = {"document": value, "issues": []}
            else:
                converted = await asyncio.to_thread(import_comfyui, value, title=body.get("title"), inventory=available)
                result = converted if "document" in converted else {"document": converted, "issues": converted.get("import_issues", [])}
            document, notes = configure_workflow(result["document"], available)
            return {**result, "document": document, "setup_notes": notes}
        except (ValueError, TypeError, RecursionError) as exc:
            raise failure(exc) from exc

    @router.post("/from-draft")
    async def from_draft(request: Request):
        _owner(request)
        try:
            body = await json_body(request)
            if set(body) - {"config", "family", "input_counts", "title"}:
                raise ValueError("Unknown draft option")
            document = from_native_config(body.get("config"), body.get("family", "h3"),
                body.get("input_counts"), title=body.get("title", "My workflow"), inventory=await inventories())
            return {"document": document}
        except (ValueError, TypeError, RecursionError) as exc:
            raise failure(exc) from exc

    @router.get("/runs")
    async def list_runs(request: Request):
        return {"runs": await asyncio.to_thread(runs.list, _owner(request))}

    @router.post("/runs", status_code=201)
    async def create_run(request: Request):
        owner = _owner(request)
        try:
            header = request.headers.get("X-Odysseus-Submission-Id")
            if header:
                uuid.UUID(header)
                existing = await asyncio.to_thread(runs.existing, owner, header)
                if existing:
                    return existing
            limit = get_chat_upload_max_bytes()

            async def accept(body, form=None):
                if set(body) - {"document", "bindings", "request_id"}:
                    raise ValueError("Unknown workflow run option")
                document = body.get("document")
                validate_document(document)
                key = body.get("request_id", header)
                if not isinstance(key, str):
                    raise ValueError("A request_id UUID is required")
                key = uuid.UUID(key).hex
                if header and uuid.UUID(header).hex != key:
                    raise ValueError("Submission IDs do not match")
                existing = await asyncio.to_thread(runs.existing, owner, key)
                if existing:
                    return existing
                compiled = await asyncio.to_thread(compile_graph, document, inventory=await inventories())
                if not compiled.get("valid"):
                    raise HTTPException(422, {"message": "Fix the workflow connections or settings before running", "issues": compiled.get("issues", [])})
                bindings = body.get("bindings", {})
                if not isinstance(bindings, dict):
                    raise ValueError("Supply workflow media bindings")
                required = {ref for step in compiled.get("plan", [])
                            for refs in step.get("inputs", {}).values() for ref in refs}
                bindings = {key: value for key, value in bindings.items() if key in required}
                async with stage_bindings(runs.root, document, bindings, form, limit) as media:
                    return await complete_operation(runs.accept, owner, document, compiled, media, key)

            if request.headers.get("content-type", "").startswith("multipart/form-data"):
                limited = _limited_request(request, limit + JSON_LIMIT + 65536, space_directory=runs.root)
                async with limited.form(max_files=64, max_fields=3, max_part_size=JSON_LIMIT) as form:
                    if any(not isinstance(value, UploadFile) and key not in {"document", "bindings", "request_id"} for key, value in form.multi_items()):
                        raise ValueError("Unknown workflow upload field")
                    for key in ("document", "bindings", "request_id"):
                        if len(form.getlist(key)) > 1:
                            raise ValueError("Repeated workflow metadata field")
                    document = form.get("document")
                    if not isinstance(document, str):
                        raise ValueError("Supply workflow document JSON")
                    return await accept({"document": parse_json(document), "bindings": parse_json(form.get("bindings", "{}")),
                                         "request_id": form.get("request_id", header)}, form)
            return await accept(await json_body(request))
        except (ValueError, TypeError, RecursionError, FileNotFoundError, RuntimeError, UploadSpaceError) as exc:
            raise failure(exc) from exc

    @router.get("/runs/{identity}")
    async def get_run(identity: str, request: Request):
        try:
            return await asyncio.to_thread(runs.detail, identity, _owner(request))
        except FileNotFoundError as exc:
            raise failure(exc) from exc

    @router.post("/runs/{identity}/cancel")
    async def cancel_run(identity: str, request: Request):
        try:
            return await asyncio.to_thread(runs.cancel, identity, _owner(request))
        except (FileNotFoundError, RuntimeError) as exc:
            raise failure(exc) from exc

    @router.get("/runs/{identity}/outputs/{output_id}")
    async def output(identity: str, output_id: str, request: Request, download: bool = False):
        try:
            path, name = await asyncio.to_thread(runs.output_path, identity, output_id, _owner(request))
            zipped = path.suffix.lower() == '.zip'
            return FileResponse(path, filename=name, media_type="application/zip" if zipped else "video/mp4",
                                content_disposition_type="attachment" if download or zipped else "inline",
                                headers={"Cache-Control": "private, no-store"})
        except (FileNotFoundError, ValueError) as exc:
            raise failure(exc) from exc

    from routes.comfy_node_package_routes import add_comfy_node_package_routes
    async def metadata_body(request):
        return await json_body(request, limit=12 * 1024 * 1024 + 1024)
    add_comfy_node_package_routes(router, _owner, metadata_body)
    return router
