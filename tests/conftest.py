import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def no_real_aws(monkeypatch, tmp_path):
    """Tests never touch a real AWS profile or the real bus."""
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.delenv("STILL_EVENT_BUS_ARN", raising=False)


def triage(**overrides):
    value = {
        "category": "lead",
        "summary": "Daniel Ortiz wants a quote for a kitchen remodel.",
        "key_facts": {"name": "Daniel Ortiz", "project": "kitchen remodel"},
        "needs_decision": True,
        "decision_reason": "A new client inquiry needs a personal reply.",
        "suggested_action": "Call Daniel Ortiz back about the kitchen remodel quote.",
        "callback_recommended": False,
        "callback_task": None,
        "block_recommended": False,
        "urgency": "medium",
    }
    value.update(overrides)
    return value


def lead_data(**triage_overrides):
    return {
        "source_ref": "msg-0001",
        "triaged_at": "2026-10-28T10:00:00Z",
        "channel": "text",
        "caller_number": "+15550001111",
        "triage": triage(**triage_overrides),
    }


@pytest.fixture
def fake_axxess():
    """A local stand-in for AXXESS POST /api/agents/mcp that records requests."""
    state = {"requests": [], "reply": None, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"auth": self.headers.get("Authorization"), "body": body})
            if state["status"] != 200:
                self.send_response(state["status"])
                self.end_headers()
                return
            reply = state["reply"] or {
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(
                    {"id": f"task-{len(state['requests'])}", "title": body["params"]["arguments"]["title"]})}]},
            }
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/api/agents/mcp"
    yield state
    server.shutdown()
