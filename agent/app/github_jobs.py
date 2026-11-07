from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .agent import run_agent_turn, store
from .config import settings
from .git_ops import default_feature_branch, git_checkout_branch, git_fetch
from .github_client import GitHubClient, GitHubError
from .github_prefs import effective_github_repo


@dataclass
class GitHubJob:
    id: str
    issue_number: int
    title: str
    status: str  # queued | running | done | failed
    session_id: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None


class JobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, GitHubJob] = {}
        self._lock = asyncio.Lock()

    def list_jobs(self) -> list[GitHubJob]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    async def enqueue_issue(
        self,
        issue_number: int,
        *,
        model: str | None = None,
        extra_instruction: str = "",
    ) -> GitHubJob:
        gh = GitHubClient()
        issue = await gh.get_issue(issue_number)
        job_id = str(uuid.uuid4())
        branch = default_feature_branch(issue["title"])
        job = GitHubJob(
            id=job_id,
            issue_number=issue_number,
            title=issue["title"],
            status="queued",
            branch=branch,
        )
        async with self._lock:
            self.jobs[job_id] = job
        asyncio.create_task(
            _run_job(job, issue, model=model, extra_instruction=extra_instruction)
        )
        return job


job_store = JobStore()


def _build_issue_prompt(issue: dict[str, Any], branch: str, extra: str) -> str:
    comments_txt = "\n".join(
        f"- {c['user']}: {c['body'][:500]}" for c in issue.get("comments", [])[-8:]
    )
    base = settings.github_default_branch
    repo = effective_github_repo()
    parts = [
        f"GitHub issue #{issue['number']} on {repo}",
        f"Title: {issue['title']}",
        f"Issue URL: {issue.get('html_url', '')}",
        "",
        "Issue body:",
        issue.get("body") or "(empty)",
    ]
    if comments_txt:
        parts.extend(["", "Recent comments:", comments_txt])
    parts.extend(
        [
            "",
            "Workflow (like Cursor Cloud Agents):",
            f"1. Create/checkout branch `{branch}` from `{base}` (use git_checkout_branch).",
            "2. Implement the fix or feature in the workspace.",
            "3. Run relevant tests with run_shell.",
            "4. git_commit with a clear message, then git_push.",
            f"5. github_create_pull_request — title/body should reference Fixes #{issue['number']}.",
            f"6. github_post_comment on the issue with a short summary and PR link.",
            "",
            extra.strip(),
        ]
    )
    return "\n".join(p for p in parts if p is not None)


async def _run_job(
    job: GitHubJob,
    issue: dict[str, Any],
    *,
    model: str | None,
    extra_instruction: str,
) -> None:
    gh = GitHubClient()
    job.status = "running"
    try:
        await gh.create_comment(
            issue["number"],
            f"🤖 **Ollama Agent** started on branch `{job.branch}`. Session will push a PR when done.",
        )
    except GitHubError:
        pass

    try:
        await git_fetch()
        await git_checkout_branch(job.branch or default_feature_branch(issue["title"]))
    except Exception as e:
        job.status = "failed"
        job.error = f"git setup: {e}"
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return

    chosen_model = model or settings.default_model
    session = store.create(model=chosen_model, title=f"Issue #{issue['number']}: {issue['title']}")
    job.session_id = session.id

    prompt = _build_issue_prompt(issue, job.branch or "", extra_instruction)
    try:
        await run_agent_turn(session, prompt)
        job.status = "done"
        # Try to surface PR URL from tool events
        for ev in reversed(session.events):
            if ev.type == "tool_result" and ev.data.get("name") == "github_create_pull_request":
                import json

                try:
                    payload = json.loads(ev.data.get("result", "{}"))
                    job.pr_url = payload.get("html_url")
                except json.JSONDecodeError:
                    pass
                break
    except Exception as e:
        job.status = "failed"
        job.error = str(e)
        try:
            await gh.create_comment(
                issue["number"],
                f"🤖 **Ollama Agent** failed: {e}",
            )
        except GitHubError:
            pass
    finally:
        job.finished_at = datetime.now(timezone.utc).isoformat()
