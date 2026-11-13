# Unraid — Ollama Cursor Agent

Local **Cursor-style coding agent** (GitHub issues → branch → PR) with **Ollama** on Unraid.

## Option A — Private Apps templates (recommended)

Two **Community Applications** XML templates (Ollama + agent). No `docker compose` required on the host.

Unraid terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/killamfkr/ollama-cursor-agent/main/unraid/install-templates.sh | bash
```

Then **Apps → Private Apps** → install **ollama**, then **ollama-cursor-agent**.

### Install Ollama

1. **Private Apps** → **ollama** → **Install**.
2. **Models** path: e.g. `/mnt/user/appdata/ollama`.
3. **Ollama Host** must be `0.0.0.0:11434` (default in the template).
4. GPU (optional): **Extra Parameters** → `--gpus all` (NVIDIA driver + toolkit on Unraid).
5. Container **Console**:

   ```bash
   ollama pull qwen2.5-coder:7b
   ```

### Install the agent

1. **Private Apps** → **ollama-cursor-agent** → **Install**.
2. **Ollama Base URL**: `http://<UNRAID-IP>:11434` (LAN IP — not `127.0.0.1`).
3. **Workspace**: empty folder is OK — pick the repo in the Web UI and click **Clone into workspace** (or git clone manually). The agent does not read github.com; it only sees this mount.
4. Web UI: `http://<UNRAID-IP>:8080`.

Image: `ghcr.io/killamfkr/ollama-cursor-agent:latest`. If GHCR pull fails, build locally:

```bash
git clone https://github.com/killamfkr/ollama-cursor-agent.git
cd ollama-cursor-agent/agent
docker build -t ghcr.io/killamfkr/ollama-cursor-agent:latest .
```

## Option B — One-script Compose stack

Full stack under `/mnt/user/appdata/ollama-cursor-agent/` with shared Docker network `ollama-agent`:

```bash
curl -fsSL https://raw.githubusercontent.com/killamfkr/ollama-cursor-agent/main/unraid/setup.sh | \
  GITHUB_TOKEN="ghp_..." \
  GITHUB_REPO="killamfkr/ollama-cursor-agent" \
  bash
```

```bash
cd /mnt/user/appdata/ollama-cursor-agent
./manage.sh urls
./manage.sh model qwen2.5-coder:7b
git clone https://github.com/killamfkr/ollama-cursor-agent.git workspace
```

## Option C — Docker tab templates

Copy XML to `templates-user` for **Docker → Add Container**:

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
curl -fsSL https://raw.githubusercontent.com/killamfkr/ollama-cursor-agent/main/unraid/ollama.xml \
  -o /boot/config/plugins/dockerMan/templates-user/my-ollama.xml
curl -fsSL https://raw.githubusercontent.com/killamfkr/ollama-cursor-agent/main/unraid/ollama-cursor-agent.xml \
  -o /boot/config/plugins/dockerMan/templates-user/my-ollama-cursor-agent.xml
```

Compose-network variants live under [`templates/`](templates/) (`my-ollama.xml` uses network `ollama-agent`).  
Unraid 8 compose installer: [`templates/ollama-cursor-agent-stack.xml`](templates/ollama-cursor-agent-stack.xml).

## GitHub (optional)

| Variable | Purpose |
|----------|---------|
| `AGENT_GITHUB_TOKEN` | PAT with `repo` + issues |
| `AGENT_GITHUB_REPO` | Optional — leave empty and use the Web UI repo dropdown |
| Agent data path (`/data`) | Persists selected repo across container recreates |
| `AGENT_GITHUB_WEBHOOK_SECRET` | Webhook HMAC secret |

Webhook URL: `https://<public-host>/api/github/webhook` (Issues + Issue comments).  
Forward port **8080** (reverse proxy, Tailscale, etc.) so GitHub can reach Unraid.

## Ollama `400 Bad Request` on chat

Usually means the **Ollama version is old**, the **model does not support tools**, or tool messages were malformed. Update the Ollama container to a recent release, pull a tool-capable model (`qwen2.5-coder:7b` or `llama3.1`), and use agent **v0.3.0+** (sanitizes `tool_name` and retries without tools).

## Verify

**Port 8080 is often used by Open WebUI on Unraid.** If `curl .../api/config` returns `"name":"Open WebUI"`, you are not talking to this agent — use the **ollama-cursor-agent** container’s host port (Docker tab → port mapping, e.g. 8283).

```bash
curl -s "http://<UNRAID-IP>:<AGENT-PORT>/api/health"
curl -s "http://<UNRAID-IP>:<AGENT-PORT>/api/config"
```

Agent config includes **`"version":"0.3.9"`** (or newer), `"workspace_root"`, and optional `github_repo` — not Open WebUI’s `"name":"Open WebUI"`.

If the web UI still shows **v0.3.8** after “updating”, the **running container** was not recreated — `docker pull` only updates the image on disk (see below).

## Updates (when Unraid does not show one)

### Why `:latest` never “auto-updates”

`:latest` is only a **label on a container image**. Unraid (and Docker) keep running the **image digest you already pulled** until you pull again. Publishing a new build to GHCR moves the `:latest` tag on the registry, but your server still has the **old digest** — there is no background sync. “Check for updates” often misses GHCR `:latest` digest changes, so you should **pull explicitly** after each release.

Unraid often **does not flag** a new `ghcr.io/...:latest` image. The GitHub Actions build may already be done while Docker still shows no update.

**1. Pull the image** (Unraid terminal or SSH):

```bash
docker pull ghcr.io/killamfkr/ollama-cursor-agent:0.3.9
# or: docker pull ghcr.io/killamfkr/ollama-cursor-agent:latest
```

**2. Recreate the container** (required — **Stop → Start is not enough** on many Unraid setups):

- Docker → **ollama-cursor-agent** → **Stop**
- **Edit** → set Repository to `ghcr.io/killamfkr/ollama-cursor-agent:0.3.9` → **Apply** → **Start**

Or **Advanced view** → **Force update**, then **Apply**.

**3. Verify** (use your agent host port, e.g. 8283):

```bash
curl -s http://<UNRAID-IP>:8283/api/config
```

Expect **`"version":"0.3.9"`**. The sidebar line **Agent v0.3.9** comes from this API, not from the Unraid “updated” banner.

**3. Refresh the Private Apps template** (new paths like Agent data, repo UI):

```bash
curl -fsSL https://raw.githubusercontent.com/killamfkr/ollama-cursor-agent/main/unraid/install-templates.sh | bash
```

Then **Apps → Private Apps** → find **ollama-cursor-agent** → **Actions → Update** (or edit and Apply).

**4. GHCR login** (if pull fails with “unauthorized”): Docker → **Add Registry** → `https://ghcr.io`, username = your GitHub user, password = GitHub PAT with `read:packages`. The package must be **public**, or your account needs package access.

## Files

| File | Role |
|------|------|
| [`ollama.xml`](ollama.xml) | Private Apps — Ollama |
| [`ollama-cursor-agent.xml`](ollama-cursor-agent.xml) | Private Apps — agent |
| [`install-templates.sh`](install-templates.sh) | Install Private App XML |
| [`setup.sh`](setup.sh) | Compose stack + `manage.sh` |
| [`compose.yaml`](compose.yaml) | Compose Manager / Unraid 8 |
