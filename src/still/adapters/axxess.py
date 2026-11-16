"""AXXESS adapter: turns STILL events into AXXESS MCP tool calls.

AXXESS exposes POST /api/agents/mcp (JSON-RPC 2.0 over Streamable HTTP) with a
Bearer agent API key issued in AXXESS. Tools marked critical there return
pending_approval instead of running; create_task is auto.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

PRIORITY_BY_URGENCY = {"high": "high", "medium": "medium", "low": "low", "none": "low"}
TITLE_LIMIT = 200

# Automation plan Phase 2 (2026-11-16): Callismatic leads become AXXESS CRM leads through the lead
# context tools (upsert_lead, log_conversation), not only tasks. Off unless this is "true", so STILL can
# be deployed before AXXESS has the tools and before STILL's key is granted them.
LEAD_SYNC_ENV = "STILL_AXXESS_LEAD_SYNC"
# JSON-RPC codes that mean "this AXXESS or this key cannot do lead sync yet": the task still goes out.
LEAD_SYNC_UNAVAILABLE_CODES = {-32001, -32601}
SUMMARY_LIMIT = 2000
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
CONVERSATION_CHANNEL = {"voicemail": "voice", "sms": "sms", "whatsapp": "whatsapp", "text": "sms", "unknown": "other"}
NAME_KEYS = ("name", "caller_name", "contact_name", "full_name")
COMPANY_KEYS = ("company", "business", "organization", "organisation", "company_name")
EMAIL_KEYS = ("email", "email_address")


class AxxessError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


def lead_sync_enabled() -> bool:
    return os.environ.get(LEAD_SYNC_ENV, "").strip().lower() == "true"


def _first_fact(facts: dict, keys: tuple[str, ...]) -> str | None:
    lowered = {k.lower(): v for k, v in facts.items()}
    for key in keys:
        value = (lowered.get(key) or "").strip()
        if value:
            return value
    return None


def lead_upsert_arguments(envelope: dict) -> dict | None:
    """upsert_lead arguments for a Callismatic lead with a callable number, else None.
    Only category "lead": an "important" item is often a supplier or a bank, not a buyer."""
    data = envelope["data"]
    triage = data["triage"]
    number = (data.get("caller_number") or "").strip()
    if triage["category"] != "lead" or not E164.match(number):
        return None
    facts = triage["key_facts"]
    arguments = {"phoneE164": number, "source": f"callismatic_{data.get('channel') or 'unknown'}"}
    name = _first_fact(facts, NAME_KEYS)
    company = _first_fact(facts, COMPANY_KEYS)
    email = _first_fact(facts, EMAIL_KEYS)
    if name:
        arguments["contactName"] = name[:200]
    if company:
        arguments["organizationName"] = company[:200]
    if email and "@" in email:
        arguments["email"] = email
    return arguments


def conversation_arguments(envelope: dict, lead_id: str) -> dict:
    """log_conversation arguments: the triaged message, on the lead's history. externalId makes a retry
    a no-op in AXXESS."""
    data = envelope["data"]
    triage = data["triage"]
    lines = [triage["summary"].strip()]
    if triage["key_facts"]:
        lines.extend(f"- {k}: {v}" for k, v in triage["key_facts"].items())
    if triage.get("suggested_action"):
        lines.append(f"Suggested next step: {triage['suggested_action'].strip()}")
    summary = "\n".join(lines)
    if len(summary) > SUMMARY_LIMIT:
        summary = summary[: SUMMARY_LIMIT - 3].rstrip() + "..."
    return {
        "leadId": lead_id,
        "channel": CONVERSATION_CHANNEL.get(data.get("channel") or "unknown", "other"),
        "direction": "inbound",
        "source": "callismatic",
        "externalId": f"callismatic:{data['source_ref']}"[:200],
        "summary": summary,
        "occurredAt": data["triaged_at"],
    }


OMNIDAMUS_CHANNEL = {"sms_rcs": "sms", "whatsapp": "whatsapp", "email": "email"}


def omnidamus_lead_upsert_arguments(envelope: dict) -> dict | None:
    """Phase 3 (2026-11-16): upsert_lead arguments for an Omnidamus completed conversation, or None when
    the customer has neither a valid E.164 number nor an email. Everyone Omnidamus talks to wrote to us
    or was started on purpose, so there is no category filter as for Callismatic."""
    data = envelope["data"]
    phone = (data.get("customer_phone") or "").strip()
    email = (data.get("customer_email") or "").strip()
    has_phone = bool(E164.match(phone))
    has_email = "@" in email
    if not has_phone and not has_email:
        return None
    arguments = {"source": f"omnidamus_{data['channel']}"}
    if has_phone:
        arguments["phoneE164"] = phone
    if has_email:
        arguments["email"] = email
    facts = data.get("key_facts") or {}
    name = _first_fact(facts, NAME_KEYS)
    company = _first_fact(facts, COMPANY_KEYS)
    if name:
        arguments["contactName"] = name[:200]
    if company:
        arguments["organizationName"] = company[:200]
    return arguments


def omnidamus_conversation_arguments(envelope: dict, lead_id: str) -> dict:
    """log_conversation arguments for an Omnidamus completed conversation. The externalId is the STILL
    event id: stable across redeliveries, and it carries no phone number."""
    data = envelope["data"]
    lines = [data["summary"].strip()]
    if data.get("key_facts"):
        lines.extend(f"- {k}: {v}" for k, v in data["key_facts"].items())
    if data.get("context"):
        lines.append(f"Why the conversation was opened: {data['context'].strip()}")
    summary = "\n".join(lines)
    if len(summary) > SUMMARY_LIMIT:
        summary = summary[: SUMMARY_LIMIT - 3].rstrip() + "..."
    return {
        "leadId": lead_id,
        "channel": OMNIDAMUS_CHANNEL.get(data["channel"], "other"),
        "direction": "inbound",
        "source": "omnidamus",
        "externalId": f"omnidamus:{envelope['id']}",
        "summary": summary,
        "occurredAt": data["completed_at"],
    }


def wants_task(data: dict) -> bool:
    triage = data["triage"]
    # Every lead becomes a task: the model often leaves needs_decision false on a real buyer,
    # and a few extra tasks cost less than one lost lead.
    return triage["category"] == "lead" or (triage["category"] == "important" and triage["needs_decision"] is True)


def task_arguments(envelope: dict, lead_id: str | None = None) -> dict:
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
    if lead_id:
        lines.append(f"AXXESS lead: {lead_id} (history on the Leads page).")
    lines.append(
        f"From Callismatic ({triage['category']}, {data.get('channel', 'unknown')}) "
        f"item {data['source_ref']}, triaged {data['triaged_at']}. STILL event {envelope['id']}."
    )
    return {
        "title": title,
        "description": "\n".join(lines),
        "priority": PRIORITY_BY_URGENCY[triage["urgency"]],
    }


def completion_task_arguments(envelope: dict, lead_id: str | None = None) -> dict:
    """create_task arguments for a triaxis.omnidamus conversation.completed.v1 envelope."""
    data = envelope["data"]
    # The facts (e.g. demo_time) are what the founder acts on, so they lead the title.
    # context is often a long agent instruction, so it goes in the description instead.
    facts = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in data["key_facts"].items())
    title = f"Omnidamus follow-up: {facts}" if facts else "Omnidamus: conversation completed"
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT - 3].rstrip() + "..."
    lines = [f"Agent summary: {data['summary'].strip()}"]
    if data.get("context"):
        lines.append(f"Conversation context: {data['context'].strip()}")
    contact = data["customer_phone"]
    if data.get("customer_email"):
        contact = f"{contact}, {data['customer_email']}"
    lines.append(f"Customer: {contact}")
    if data["key_facts"]:
        lines.append("Key facts:")
        lines.extend(f"- {k}: {v}" for k, v in data["key_facts"].items())
    lines.append(f"Receipt email sent: {'yes' if data['receipt_sent'] else 'no'}")
    if lead_id:
        lines.append(f"AXXESS lead: {lead_id} (history on the Leads page).")
    lines.append(
        f"From Omnidamus conversation {data['conversation_id']} (closed on {data['channel']}, "
        f"{data['completed_at']}). STILL event {envelope['id']}."
    )
    return {"title": title, "description": "\n".join(lines), "priority": "medium"}


def agent_call_arguments(envelope: dict) -> dict:
    """log_agent_call arguments for a triaxis.callismatic call.completed.v1 envelope (an AXXESS
    "Call with agent" job). AXXESS records the result, moves the lead on and leaves a follow-up."""
    data = envelope["data"]
    if data["status"] == "failed":
        return {"jobId": data["job_id"], "status": "failed", "failureReason": data.get("failure_reason") or "unknown"}
    arguments = {
        "jobId": data["job_id"],
        "status": "completed",
        "outcome": data["outcome"],
        "summary": (data.get("summary") or "").strip(),
        "actionItems": data["action_items"],
        "optedOut": data["opted_out"],
    }
    if data.get("follow_up_at"):
        arguments["followUpAt"] = data["follow_up_at"]
    return arguments


def demo_meeting_arguments(envelope: dict) -> dict | None:
    """create_meeting arguments when the contact accepted a demo slot on the call, else None.
    create_meeting is critical in AXXESS, so it waits for the founder's approval."""
    data = envelope["data"]
    slot = data.get("accepted_slot")
    if data["status"] != "completed" or not slot or data["opted_out"]:
        return None
    title = f"Demo: {data['title'].strip()}"
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT - 3].rstrip() + "..."
    notes = (data.get("summary") or "").strip()
    return {
        "title": title,
        "startsAt": slot["starts_at"],
        "endsAt": slot["ends_at"],
        "notes": "\n".join(filter(None, [
            notes,
            f"Booked by the voice agent on AXXESS agent call job {data['job_id']}. STILL event {envelope['id']}.",
        ])),
    }


class AxxessMcpClient:
    def __init__(self, url: str, api_key: str, timeout: float = 20.0, bypass_secret: str | None = None):
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise ValueError("AXXESS MCP URL must be https")
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self.bypass_secret = bypass_secret

    def call_tool(self, name: str, arguments: dict, request_id: str) -> dict:
        """Returns the tool's JSON payload. Raises AxxessError on any failure."""
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }).encode()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        # Staging only: passes Vercel's deployment protection. Production never configures it.
        if self.bypass_secret:
            headers["x-vercel-protection-bypass"] = self.bypass_secret
        request = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
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
                              retryable=code == -32603, code=code)
        result = reply.get("result") or {}
        text = (result.get("content") or [{}])[0].get("text", "")
        if result.get("isError"):
            raise AxxessError(f"AXXESS tool {name} failed: {text}", retryable=False)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
