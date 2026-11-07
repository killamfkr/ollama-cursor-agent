from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import run_agent_turn, store, stream_agent_turn
from .config import settings
from .git_ops import configure_origin_from_settings, ensure_git_identity
from .github_client import GitHubClient, GitHubError
from .github_prefs import (
    detect_repo_from_workspace,
    effective_github_repo,
    github_env_repo_locked,
    save_selected_repo,
    selected_repo_from_store,
    token_configured,
)
from .github_jobs import job_store
from .github_webhook import handle_github_event, verify_signature
from .ollama_client import OllamaClient
from .__version__ import __version__


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_git_identity()
    await configure_origin_from_settings()
    yield


app = FastAPI(title="Ollama Cursor Agent", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_ui_assets(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html", "/app.js", "/styles.css"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    model: str | None = None
    title: str | None = None


class PullRequest(BaseModel):
    model: str


class RunIssueRequest(BaseModel):
    issue_number: int
    model: str | None = None
    instruction: str = ""


class GitHubRepoRequest(BaseModel):
    repo: str


@app.get("/api/health")
async def health() -> dict[str, Any]:
    ollama = OllamaClient()
    try:
        models = await ollama.list_models()
        return {"ok": True, "ollama": True, "version": __version__, "models": models}
    except Exception as e:
        return {"ok": False, "ollama": False, "error": str(e)}


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "version": __version__,
        "default_model": settings.default_model,
        "workspace_root": settings.workspace_root,
        "max_agent_steps": settings.max_agent_steps,
        "github_repo": effective_github_repo() or None,
        "github_repo_source": (
            "env"
            if github_env_repo_locked()
            else ("ui" if selected_repo_from_store() else ("workspace" if detect_repo_from_workspace() else None))
        ),
        "github_repo_locked": github_env_repo_locked(),
        "github_default_branch": settings.github_default_branch,
        "github_trigger_label": settings.github_trigger_label,
        "github_comment_trigger": settings.github_comment_trigger,
        "public_base_url": settings.public_base_url or None,
    }


@app.get("/api/github/status")
async def github_status() -> dict[str, Any]:
    gh = GitHubClient()
    base: dict[str, Any] = {
        "token_configured": token_configured(),
        "repo": effective_github_repo() or None,
        "repo_locked": github_env_repo_locked(),
        "repo_detected_workspace": detect_repo_from_workspace() or None,
        "repo_selected_ui": selected_repo_from_store() or None,
    }
    if not token_configured():
        return {
            **base,
            "configured": False,
            "hint": "Set AGENT_GITHUB_TOKEN on the agent container (repo can be chosen in the UI)",
        }
    try:
        auth = await gh.verify_auth()
    except GitHubError as e:
        return {
            **base,
            "configured": False,
            "ok": False,
            "error": e.message,
            "status": e.status,
        }
    if not gh.configured:
        return {
            **base,
            **auth,
            "configured": False,
            "hint": "Choose a repository in the GitHub section of the sidebar",
        }
    return {"configured": True, **base, **auth}


@app.get("/api/github/repos")
async def github_repos(limit: int = 100) -> dict[str, Any]:
    gh = GitHubClient()
    if not token_configured():
        raise HTTPException(status_code=400, detail="GitHub token not configured")
    try:
        repos = await gh.list_repositories(limit=limit)
    except GitHubError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from e
    return {"repos": repos, "selected": effective_github_repo() or None}


@app.put("/api/github/repo")
async def github_set_repo(body: GitHubRepoRequest) -> dict[str, Any]:
    if github_env_repo_locked():
        raise HTTPException(
            status_code=409,
            detail="Repository is fixed by AGENT_GITHUB_REPO; clear it to use the UI picker",
        )
    try:
        saved = save_selected_repo(body.repo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await configure_origin_from_settings()
    return {"repo": saved, "selected": saved}


@app.get("/api/github/issues")
async def github_issues() -> dict[str, Any]:
    gh = GitHubClient()
    try:
        return {"issues": await gh.list_open_issues()}
    except GitHubError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from e


@app.post("/api/github/run-issue")
async def github_run_issue(body: RunIssueRequest) -> dict[str, Any]:
    job = await job_store.enqueue_issue(
        body.issue_number, model=body.model, extra_instruction=body.instruction
    )
    return {
        "job_id": job.id,
        "issue_number": job.issue_number,
        "branch": job.branch,
        "status": job.status,
    }


@app.get("/api/github/jobs")
async def github_jobs() -> dict[str, Any]:
    return {
        "jobs": [
            {
                "id": j.id,
                "issue_number": j.issue_number,
                "title": j.title,
                "status": j.status,
                "branch": j.branch,
                "session_id": j.session_id,
                "pr_url": j.pr_url,
                "error": j.error,
                "created_at": j.created_at,
                "finished_at": j.finished_at,
            }
            for j in job_store.list_jobs()
        ]
    }


@app.post("/api/github/webhook")
async def github_webhook(request: Request) -> dict[str, Any]:
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(body, sig):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    event = request.headers.get("X-GitHub-Event", "")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid JSON") from e
    return await handle_github_event(event, payload)


@app.get("/api/models")
async def list_models() -> dict[str, Any]:
    ollama = OllamaClient()
    try:
        return {"models": await ollama.list_models()}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/models/pull")
async def pull_model(body: PullRequest) -> StreamingResponse:
    ollama = OllamaClient()

    async def gen():
        try:
            async for chunk in ollama.pull_model(body.model):
                yield json.dumps(chunk) + "\n"
        except Exception as e:
            yield json.dumps({"error": str(e)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/sessions")
async def list_sessions() -> dict[str, Any]:
    return {
        "sessions": [
            {
                "id": s.id,
                "title": s.title,
                "model": s.model,
                "message_count": len(s.messages),
            }
            for s in store.sessions.values()
        ]
    }


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "id": session.id,
        "title": session.title,
        "model": session.model,
        "messages": session.messages,
        "events": [
            {"type": e.type, "data": e.data, "ts": e.ts} for e in session.events
        ],
    }


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    model = req.model or settings.default_model
    if req.session_id:
        session = store.get(req.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        title = req.title or (req.message[:60] + ("…" if len(req.message) > 60 else ""))
        session = store.create(model=model, title=title)

    reply = await run_agent_turn(session, req.message)
    return {
        "session_id": session.id,
        "reply": reply,
        "events": [
            {"type": e.type, "data": e.data, "ts": e.ts} for e in session.events[-50:]
        ],
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    model = req.model or settings.default_model
    if req.session_id:
        session = store.get(req.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        title = req.title or (req.message[:60] + ("…" if len(req.message) > 60 else ""))
        session = store.create(model=model, title=title)

    async def event_stream():
        meta = {"type": "session", "session_id": session.id, "model": session.model}
        yield f"data: {json.dumps(meta)}\n\n"
        async for ev in stream_agent_turn(session, req.message):
            payload = {"type": ev.type, "data": ev.data, "ts": ev.ts}
            yield f"data: {json.dumps(payload)}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if not STATIC_DIR.is_dir():
    STATIC_DIR = Path("/app/static")

# Static UI mounted last
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
