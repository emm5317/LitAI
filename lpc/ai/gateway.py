"""AI gateway: the only path from the console to a model.

Every call goes through `complete()`, which
  1. refuses if the matter's client has not consented to AI use (matters.ai_permitted),
  2. refuses models that are not available under zero data retention,
  3. sends a plain Messages API request with inline content (no Files API, no batch,
     no prompt caching), and
  4. writes a row to ai_calls, whether the call succeeded, failed or was blocked.

The API key is read from Windows Credential Manager through `keyring`
(service "LawPracticeConsole", user "anthropic"), falling back to ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = os.environ.get("LPC_MODEL", "claude-opus-5-5")

# Model families that require 30-day retention and are not available under ZDR
# unless Anthropic authorizes it (see Anthropic's API and data retention docs).
ZDR_BLOCKED_PREFIXES = ("claude-fable", "claude-mythos")


class GatewayBlocked(Exception):
    """Raised when policy forbids the call. Nothing was sent."""


@dataclass
class AIRequest:
    purpose: str
    prompt: str
    system: str | None = None
    matter_id: int | None = None
    document_ids: list[int] = field(default_factory=list)
    template: str | None = None
    template_version: str | None = None
    model: str = DEFAULT_MODEL
    max_tokens: int = 4096


def get_api_key() -> str | None:
    try:
        import keyring

        key = keyring.get_password("LawPracticeConsole", "anthropic")
        if key:
            return key
    except Exception:  # keyring backend missing (e.g. headless Linux CI)
        pass
    return os.environ.get("ANTHROPIC_API_KEY")


def _log(conn: sqlite3.Connection, req: AIRequest, status: str, *, blocked_reason: str | None = None,
         error: str | None = None, usage: dict | None = None) -> int:
    usage = usage or {}
    cur = conn.execute(
        "INSERT INTO ai_calls (matter_id, purpose, template, template_version, provider, model, document_ids,"
        " input_chars, input_tokens, output_tokens, status, blocked_reason, error)"
        " VALUES (?, ?, ?, ?, 'anthropic', ?, ?, ?, ?, ?, ?, ?, ?)",
        (req.matter_id, req.purpose, req.template, req.template_version, req.model,
         json.dumps(req.document_ids), len(req.prompt) + len(req.system or ""),
         usage.get("input_tokens"), usage.get("output_tokens"), status, blocked_reason, error),
    )
    conn.commit()
    return cur.lastrowid


def check_policy(conn: sqlite3.Connection, req: AIRequest) -> str | None:
    """Return a reason string if the call must be blocked, else None."""
    if req.model.startswith(ZDR_BLOCKED_PREFIXES):
        return f"model {req.model} is not available under zero data retention"
    if req.matter_id is not None:
        row = conn.execute("SELECT ai_permitted FROM matters WHERE id = ?", (req.matter_id,)).fetchone()
        if row is None:
            return "unknown matter"
        if not row["ai_permitted"]:
            return "client has not consented to AI use on this matter"
    return None


def complete(conn: sqlite3.Connection, req: AIRequest, *, client: httpx.Client | None = None) -> str:
    reason = check_policy(conn, req)
    if reason:
        _log(conn, req, "blocked", blocked_reason=reason)
        raise GatewayBlocked(reason)
    key = get_api_key()
    if not key:
        _log(conn, req, "blocked", blocked_reason="no API key configured")
        raise GatewayBlocked("no API key configured")

    body: dict = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "messages": [{"role": "user", "content": req.prompt}],
    }
    if req.system:
        body["system"] = req.system
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}

    own_client = client is None
    client = client or httpx.Client(timeout=120)
    try:
        resp = client.post(API_URL, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # network, HTTP or decode errors are all logged the same way
        _log(conn, req, "error", error=str(exc)[:500])
        raise
    finally:
        if own_client:
            client.close()

    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    _log(conn, req, "ok", usage=data.get("usage"))
    return text
