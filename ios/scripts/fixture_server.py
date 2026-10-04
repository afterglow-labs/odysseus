#!/usr/bin/env python3
"""Loopback-only API fixture for native UI tests. Never uses the desktop profile."""
import argparse
import json
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MODEL = "/desktop/models/Qwen-fixture-Q4.gguf"
ENDPOINT = "http://localhost:8001/v1/chat/completions"
TOKEN = "odysseus_session=ios-fixture-session"
lock = threading.RLock()
state = {}


def reset():
    with lock:
        state.clear()
        state.update(sessions={"existing": dict(id="existing", name="Desktop conversation", model=MODEL, endpoint_url=ENDPOINT, mode="chat")},
                     history={"existing": [{"role": "user", "content": "An existing conversation"}, {"role": "assistant", "content": "History loaded from the desktop."}]},
                     profiles={}, uploads={}, upload_data={}, runs={}, requests=[], persona={"character_name": "", "system_prompt": "", "temperature": 1, "max_tokens": 0}, templates=[])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, value, status=200, headers=None):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, val in (headers or {}).items(): self.send_header(key, val)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if TOKEN in self.headers.get("Cookie", ""): return True
        self.reply({"detail": "Not authenticated"}, 401)
        return False

    def body(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("application/json"): return json.loads(data or b"{}")
        message = BytesParser(policy=default).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + data)
        result = {}
        for part in message.iter_parts():
            key = part.get_param("name", header="content-disposition")
            if part.get_filename():
                result.setdefault("files", []).append((part.get_filename(), part.get_content_type(), part.get_payload(decode=True)))
            else: result[key] = part.get_payload(decode=True).decode()
        return result

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/test/state":
            return self.reply({"requests": state["requests"], "profiles": state["profiles"]})
        if path == "/api/auth/status":
            return self.reply({"configured": True, "authenticated": TOKEN in self.headers.get("Cookie", ""), "username": "tester", "is_admin": True})
        if not self.authorized(): return
        if path.startswith("/api/upload/"):
            fid = path.rsplit("/", 1)[-1]
            data = state["upload_data"].get(fid)
            if data is None: return self.reply({"detail": "Not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", state["uploads"][fid]["mime"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/models":
            return self.reply({"hosts": [], "items": [{"endpoint_id": "fixture", "endpoint_name": "Desktop model", "url": ENDPOINT, "models": [MODEL], "models_display": ["Qwen Fixture · Q4_K_M"], "models_extra": [], "models_extra_display": []}]})
        if path == "/api/sessions": return self.reply(list(state["sessions"].values()))
        if path.startswith("/api/history/"):
            sid = path.rsplit("/", 1)[-1]
            if sid not in state["sessions"]: return self.reply({"detail": "Not found"}, 404)
            return self.reply({**state["sessions"][sid], "history": state["history"][sid], "offset": 0, "has_more_before": False})
        if path.startswith("/api/chat/stream_status/"):
            run = state["runs"].get(path.rsplit("/", 1)[-1])
            return self.reply({"status": "streaming", "detached": True}) if run and not run["done"] else self.reply({"detail": "No active stream"}, 404)
        if path.startswith("/api/chat/resume/"):
            run = state["runs"].get(path.rsplit("/", 1)[-1])
            if not run or run["done"]: return self.reply({"detail": "No active run"}, 404)
            return self.stream(run)
        if path == "/api/prefs/model-generation": return self.reply({"key": "model-generation", "value": state["profiles"]})
        if path == "/api/presets": return self.reply({"custom": state["persona"]})
        if path == "/api/presets/templates": return self.reply(state["templates"])
        return self.reply({"detail": "Unknown fixture endpoint"}, 404)

    def do_POST(self):
        body = self.body()
        path = urlparse(self.path).path
        if path == "/test/reset": reset(); return self.reply({"ok": True})
        if path == "/api/auth/login":
            if body.get("password") != "fixture-password": return self.reply({"detail": "Invalid credentials"}, 401)
            if body.get("username") == "twofactor" and not body.get("totp_code"): return self.reply({"ok": False, "requires_totp": True})
            if body.get("username") == "twofactor" and body.get("totp_code") != "123456": return self.reply({"detail": "Invalid authenticator code"}, 401)
            return self.reply({"ok": True, "username": "tester"}, headers={"Set-Cookie": TOKEN + "; Path=/; HttpOnly; SameSite=Lax; Max-Age=3600"})
        if not self.authorized(): return
        if path == "/api/auth/logout": return self.reply({"ok": True}, headers={"Set-Cookie": "odysseus_session=; Path=/; Max-Age=0"})
        if path == "/api/session":
            sid = str(uuid.uuid4())
            row = dict(id=sid, name="Native test conversation", model=body["model"], endpoint_url=body["endpoint_url"], mode="chat")
            state["sessions"][sid] = row; state["history"][sid] = []
            return self.reply(row)
        if path == "/api/upload":
            files = []
            for name, mime, data in body.get("files", []):
                if name.startswith("slow-upload"): time.sleep(1)
                fid = str(uuid.uuid4())
                meta = dict(id=fid, name=name, mime=mime, size=len(data))
                state["uploads"][fid] = meta; state["upload_data"][fid] = data; files.append(meta)
            return self.reply({"files": files})
        if path == "/api/chat_stream":
            sid = body["session"]
            if body.get("tool_approval_id"):
                pending = state["runs"].get(sid, {}).get("approval")
                if not pending or pending["approval_id"] != body["tool_approval_id"] or body.get("tool_approval_decision") not in {"approve_task", "approve", "deny"}:
                    return self.reply({"detail": "Invalid approval continuation"}, 409)
                pending["resolved"] = body["tool_approval_decision"]
            state["requests"].append(body)
            state["sessions"][sid]["mode"] = body.get("mode", "chat")
            files = [state["uploads"][fid] for fid in json.loads(body.get("attachments", "[]"))]
            state["history"][sid].append({"role": "user", "content": body["message"], "metadata": {"attachments": files}})
            run = dict(id=uuid.uuid4().hex, events=[], done=False, stopped=False)
            state["runs"][sid] = run
            def generate():
                slow = "slow" in body["message"].lower()
                tools = []
                if body.get("mode") == "agent":
                    run["events"].extend([{"type": "tool_start", "tool": "read_file", "command": "fixture.txt"}, {"type": "tool_output", "tool": "read_file", "output": "Fixture tool output"}])
                    tools.append({"tool": "read_file", "command": "fixture.txt", "output": "Fixture tool output"})
                if "approval" in body["message"].lower():
                    approval = {"kind": "tool_approval", "approval_id": uuid.uuid4().hex, "question": "Allow this task to continue?", "description": "Fixture action", "options": [{"label": "Allow for this task", "value": "approve_task"}, {"label": "Deny", "value": "deny"}], "action": {"tool": "write_file", "content": "fixture-only.txt", "effects": ["file_write"]}}
                    run["approval"] = approval
                    run["events"].append({"type": "ask_user", "data": approval})
                    tools.append({"tool": "write_file", "ask_user": approval})
                    state["history"][sid].append({"role": "assistant", "content": "Review the requested action.", "metadata": {"tool_events": tools}})
                    run["events"].append("[DONE]"); run["done"] = True
                    return
                if body.get("tool_approval_id"): run["events"].append({"type": "tool_approval_resolved"})
                answer = ""
                for token in (["Working… "] * 40 if slow else ["Your native client ", "reached the desktop API."]):
                    if run["stopped"]: break
                    run["events"].append({"delta": token}); answer += token; time.sleep(0.3)
                state["history"][sid].append({"role": "assistant", "content": answer, "metadata": {"tool_events": tools}})
                run["events"].append("[DONE]"); run["done"] = True
            threading.Thread(target=generate, daemon=True).start()
            return self.stream(run)
        if path.startswith("/api/chat/stop/"):
            run = state["runs"].get(path.rsplit("/", 1)[-1])
            stopped = bool(run and run["id"] == self.headers.get("X-Odysseus-Run-Id") and not run["done"])
            if stopped: run["stopped"] = True
            return self.reply({"stopped": stopped})
        if path == "/api/presets/custom":
            state["persona"] = {**body, "character_name": body.get("name", "")}
            return self.reply({"success": True})
        if path == "/api/presets/templates":
            state["templates"].append(body); return self.reply({"success": True, "template": body})
        return self.reply({"detail": "Unknown fixture endpoint"}, 404)

    def do_PUT(self):
        body = self.body()
        if not self.authorized(): return
        if self.path == "/api/prefs/model-generation":
            if body["options"]: state["profiles"][body["model_key"]] = body["options"]
            else: state["profiles"].pop(body["model_key"], None)
            return self.reply({"key": "model-generation", "value": state["profiles"]})
        self.reply({"detail": "Not found"}, 404)

    def do_PATCH(self):
        body = self.body()
        if not self.authorized(): return
        sid = self.path.rsplit("/", 1)[-1]
        state["sessions"][sid].update(body)
        self.reply({"id": sid, **body})

    def stream(self, run):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-Odysseus-Run-Id", run["id"])
        self.send_header("Connection", "close")
        self.end_headers()
        cursor = 0
        try:
            while True:
                while cursor < len(run["events"]):
                    item = run["events"][cursor]; cursor += 1
                    data = item if isinstance(item, str) else json.dumps(item)
                    self.wfile.write(("data: " + data + "\n\n").encode()); self.wfile.flush()
                if run["done"]: break
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError): pass
        finally: self.close_connection = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args()
    reset()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
