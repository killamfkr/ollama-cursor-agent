from __future__ import annotations

import base64
from typing import Any

import httpx

from .config import settings
from .github_prefs import effective_github_repo, token_configured

API = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class GitHubClient:
    def __init__(self, token: str | None = None, repo: str | None = None) -> None:
        self.token = (token or settings.github_token).strip()
        self.repo = (repo or effective_github_repo()).strip()

    @property
    def token_ok(self) -> bool:
        return token_configured()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.repo and "/" in self.repo)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise GitHubError(401, "GITHUB token not configured")
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{API}{path}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.request(
                method, url, headers=self._headers(), json=json, params=params
            )
        if r.status_code >= 400:
            try:
                detail = r.json().get("message", r.text)
            except Exception:
                detail = r.text
            raise GitHubError(r.status_code, detail)
        if r.status_code == 204:
            return {}
        return r.json()

    async def verify_auth(self) -> dict[str, Any]:
        user = await self._request("GET", "/user")
        out: dict[str, Any] = {"login": user.get("login"), "ok": True}
        if self.repo and "/" in self.repo:
            repo = await self._request("GET", f"/repos/{self.repo}")
            out["repo"] = {
                "full_name": repo.get("full_name"),
                "default_branch": repo.get("default_branch"),
                "private": repo.get("private"),
            }
        return out

    async def get_readme(self) -> dict[str, Any]:
        if not self.repo or "/" not in self.repo:
            raise GitHubError(400, "AGENT_GITHUB_REPO not set")
        try:
            data = await self._request("GET", f"/repos/{self.repo}/readme")
        except GitHubError as e:
            if e.status == 404:
                return {"found": False, "repo": self.repo}
            raise
        raw = data.get("content") or ""
        try:
            text = base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return {
            "found": True,
            "repo": self.repo,
            "path": data.get("path"),
            "html_url": data.get("html_url"),
            "content": text[:12_000],
        }

    async def list_open_issues(self, limit: int = 30) -> list[dict[str, Any]]:
        if not self.configured:
            raise GitHubError(400, "AGENT_GITHUB_REPO not set")
        items = await self._request(
            "GET",
            f"/repos/{self.repo}/issues",
            params={"state": "open", "per_page": min(limit, 100)},
        )
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "labels": [l["name"] for l in i.get("labels", [])],
                "user": (i.get("user") or {}).get("login"),
                "html_url": i.get("html_url"),
            }
            for i in items
            if "pull_request" not in i
        ]

    async def get_issue(self, number: int) -> dict[str, Any]:
        if not self.configured:
            raise GitHubError(400, "AGENT_GITHUB_REPO not set")
        issue = await self._request("GET", f"/repos/{self.repo}/issues/{number}")
        comments = await self._request(
            "GET", f"/repos/{self.repo}/issues/{number}/comments"
        )
        return {
            "number": issue["number"],
            "title": issue["title"],
            "body": issue.get("body") or "",
            "state": issue.get("state"),
            "labels": [l["name"] for l in issue.get("labels", [])],
            "html_url": issue.get("html_url"),
            "comments": [
                {
                    "user": (c.get("user") or {}).get("login"),
                    "body": c.get("body") or "",
                    "created_at": c.get("created_at"),
                }
                for c in comments
            ],
        }

    async def create_comment(self, number: int, body: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/repos/{self.repo}/issues/{number}/comments",
            json={"body": body},
        )

    async def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        draft: bool = False,
    ) -> dict[str, Any]:
        base_branch = base or settings.github_default_branch
        return await self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base_branch,
                "draft": draft,
            },
        )

    async def update_pull_request(
        self,
        pr_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        return await self._request(
            "PATCH", f"/repos/{self.repo}/pulls/{pr_number}", json=payload
        )

    async def find_pr_by_branch(self, branch: str) -> dict[str, Any] | None:
        pulls = await self._request(
            "GET",
            f"/repos/{self.repo}/pulls",
            params={"head": f"{self.repo.split('/')[0]}:{branch}", "state": "open"},
        )
        if pulls:
            p = pulls[0]
            return {
                "number": p["number"],
                "title": p["title"],
                "html_url": p["html_url"],
                "state": p["state"],
            }
        return None

    async def list_repositories(
        self, *, limit: int = 100, affiliation: str = "owner,collaborator,organization_member"
    ) -> list[dict[str, Any]]:
        if not self.token:
            raise GitHubError(401, "GITHUB token not configured")
        per_page = min(max(limit, 1), 100)
        items = await self._request(
            "GET",
            "/user/repos",
            params={
                "sort": "updated",
                "direction": "desc",
                "per_page": per_page,
                "affiliation": affiliation,
            },
        )
        if not isinstance(items, list):
            return []
        return [
            {
                "full_name": r.get("full_name"),
                "name": r.get("name"),
                "owner": (r.get("owner") or {}).get("login"),
                "private": r.get("private"),
                "default_branch": r.get("default_branch"),
                "html_url": r.get("html_url"),
            }
            for r in items
            if r.get("full_name")
        ]
