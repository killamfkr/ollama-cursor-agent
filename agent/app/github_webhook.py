from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from .config import settings
from .github_prefs import effective_github_repo, token_configured
from .github_jobs import job_store


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    secret = settings.github_webhook_secret.strip()
    if not secret:
        # Dev mode: accept if no secret configured (document risk in README)
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    got = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, got)


async def handle_github_event(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not token_configured() or not effective_github_repo():
        return {"ignored": True, "reason": "github not configured"}

    repo_full = effective_github_repo()
    payload_repo = (payload.get("repository") or {}).get("full_name")
    if payload_repo and payload_repo != repo_full:
        return {"ignored": True, "reason": "different repository"}

    if event_name == "issues":
        action = payload.get("action")
        issue = payload.get("issue") or {}
        if action not in ("opened", "labeled", "reopened"):
            return {"ignored": True, "action": action}
        labels = {l["name"] for l in issue.get("labels", [])}
        if action == "labeled":
            new_label = (payload.get("label") or {}).get("name")
            if new_label != settings.github_trigger_label:
                return {"ignored": True, "reason": "unrelated label"}
        elif settings.github_trigger_label not in labels:
            return {"ignored": True, "reason": "missing trigger label"}
        number = issue.get("number")
        if not number:
            return {"ignored": True}
        job = await job_store.enqueue_issue(int(number))
        return {"accepted": True, "job_id": job.id, "issue": number}

    if event_name == "issue_comment":
        action = payload.get("action")
        if action != "created":
            return {"ignored": True, "action": action}
        comment = payload.get("comment") or {}
        body = (comment.get("body") or "").lower()
        trigger = settings.github_comment_trigger.lower()
        if trigger not in body:
            return {"ignored": True, "reason": "no trigger phrase"}
        issue = payload.get("issue") or {}
        if issue.get("pull_request"):
            return {"ignored": True, "reason": "PR comment not supported yet"}
        number = issue.get("number")
        if not number:
            return {"ignored": True}
        extra = comment.get("body") or ""
        job = await job_store.enqueue_issue(int(number), extra_instruction=extra)
        return {"accepted": True, "job_id": job.id, "issue": number}

    return {"ignored": True, "event": event_name}
