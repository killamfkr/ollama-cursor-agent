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
3. **Workspace**: git clone the agent may edit, e.g. `/mnt/user/appdata/ollama-cursor-agent/workspace`.
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

## Verify

```bash
curl -s "http://<UNRAID-IP>:8080/api/health"
```

## Files

| File | Role |
|------|------|
| [`ollama.xml`](ollama.xml) | Private Apps — Ollama |
| [`ollama-cursor-agent.xml`](ollama-cursor-agent.xml) | Private Apps — agent |
| [`install-templates.sh`](install-templates.sh) | Install Private App XML |
| [`setup.sh`](setup.sh) | Compose stack + `manage.sh` |
| [`compose.yaml`](compose.yaml) | Compose Manager / Unraid 8 |
