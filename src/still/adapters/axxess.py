"""AXXESS adapter: turns STILL events into AXXESS MCP tool calls.

AXXESS exposes POST /api/agents/mcp (JSON-RPC 2.0 over Streamable HTTP) with a
Bearer agent API key issued in AXXESS. Tools marked critical there return
pending_approval instead of running; create_task is auto.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

PRIORITY_BY_URGENCY = {"high": "high", "medium": "medium", "low": "low", "none": "low"}
TITLE_LIMIT = 200
ACTIONABLE_CATEGORIES = {"lead", "important"}


class AxxessError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def wants_task(data: dict) -> bool:
    triage = data["triage"]
    return triage["category"] in ACTIONABLE_CATEGORIES and triage["needs_decision"] is True


def task_arguments(envelope: dict) -> dict:
    """create_task arguments for a triaxis.callismatic lead.triaged.v1 envelope."""
    data = envelope["data"]
    triage = data["triage"]
    title = (triage.get("suggested_action") or triage["summary"]).strip()
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT - 3].rstrip() + "..."
    lines = [triage["summary"].strip()]
    if data.get("caller_number") and data["caller_number"] != "unknown":
        lines.append(f"Caller: {data['caller_number']}")
    if triage.get("decision_reason"):
        lines.append(f"Why it needs you: {triage['decision_reason'].strip()}")
    if triage["key_facts"]:
        lines.append("Key facts:")
        lines.extend(f"- {k}: {v}" for k, v in triage["key_facts"].items())
    lines.append(
        f"From Callismatic ({triage['category']}, {data.get('channel', 'unknown')}) "
        f"item {data['source_ref']}, triaged {data['triaged_at']}. STILL event {envelope['id']}."
    )
    return {
        "title": title,
        "description": "\n".join(lines),
        "priority": PRIORITY_BY_URGENCY[triage["urgency"]],
    }


def completion_task_arguments(envelope: dict) -> dict:
    """create_task arguments for a triaxis.omnidamus conversation.completed.v1 envelope."""
    data = envelope["data"]
    facts = "; ".join(f"{k}: {v}" for k, v in data["key_facts"].items())
    title = f"Omnidamus: {data.get('context') or 'conversation'} completed"
    if facts:
        title = f"{title} - {facts}"
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT - 3].rstrip() + "..."
    lines = [f"Agent summary: {data['summary'].strip()}"]
    contact = data["customer_phone"]
    if data.get("customer_email"):
        contact = f"{contact}, {data['customer_email']}"
    lines.append(f"Customer: {contact}")
    if data["key_facts"]:
        lines.append("Key facts:")
        lines.extend(f"- {k}: {v}" for k, v in data["key_facts"].items())
    lines.append(f"Receipt email sent: {'yes' if data['receipt_sent'] else 'no'}")
    lines.append(
        f"From Omnidamus conversation {data['conversation_id']} (closed on {data['channel']}, "
        f"{data['completed_at']}). STILL event {envelope['id']}."
    )
    return {"title": title, "description": "\n".join(lines), "priority": "medium"}


class AxxessMcpClient:
    def __init__(self, url: str, api_key: str, timeout: float = 20.0):
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise ValueError("AXXESS MCP URL must be https")
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    def call_tool(self, name: str, arguments: dict, request_id: str) -> dict:
        """Returns the tool's JSON payload. Raises AxxessError on any failure."""
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }).encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            # 401/403/413 will not fix themselves; 429 and 5xx may.
            retryable = error.code == 429 or error.code >= 500
            raise AxxessError(f"AXXESS MCP HTTP {error.code}", retryable=retryable) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise AxxessError(f"AXXESS MCP unreachable: {error}", retryable=True) from error
        except json.JSONDecodeError as error:
            raise AxxessError("AXXESS MCP returned non-JSON", retryable=True) from error

        if "error" in reply:
            code = reply["error"].get("code")
            # -32603 is a handler exception on the AXXESS side; the rest are caller errors.
            raise AxxessError(f"AXXESS MCP error {code}: {reply['error'].get('message')}",
                              retryable=code == -32603)
        result = reply.get("result") or {}
        text = (result.get("content") or [{}])[0].get("text", "")
        if result.get("isError"):
            raise AxxessError(f"AXXESS tool {name} failed: {text}", retryable=False)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
