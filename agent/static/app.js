let sessionId = null;

const transcript = document.getElementById("transcript");
const promptEl = document.getElementById("prompt");
const modelSelect = document.getElementById("modelSelect");
const healthStatus = document.getElementById("healthStatus");
const sessionLabel = document.getElementById("sessionLabel");
const sendBtn = document.getElementById("sendBtn");

function repoFullName(repo) {
  if (!repo) return "";
  if (typeof repo === "string") return repo;
  if (typeof repo === "object" && repo.full_name) return repo.full_name;
  return String(repo);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function workspaceCloneMessage(payload) {
  const d = payload?.detail !== undefined ? payload.detail : payload;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d
      .map((e) => e?.msg || JSON.stringify(e))
      .filter(Boolean)
      .join("\n");
  }
  if (!d || typeof d !== "object") return String(d ?? "Unknown error");
  const parts = [];
  if (d.action) parts.push(`Step: ${d.action}`);
  if (d.error) parts.push(String(d.error));
  else if (d.reason) parts.push(String(d.reason));
  if (d.hint) parts.push(d.hint);
  if (d.cleared?.errors?.length) {
    parts.push(`Remove errors:\n${d.cleared.errors.join("\n")}`);
  }
  if (d.install_errors?.length) {
    parts.push(`Install errors:\n${d.install_errors.join("\n")}`);
  }
  if (d.sample?.length) parts.push(`On disk: ${d.sample.join(", ")}`);
  if (d.repo) parts.push(`Selected repo: ${d.repo}`);
  if (!parts.length) return JSON.stringify(d, null, 2);
  return parts.join("\n\n");
}

function showWorkspaceAlert(message, { title = "Workspace", ok = false } = {}) {
  const el = document.getElementById("workspaceAlert");
  if (!el) return;
  if (!message) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.toggle("ok", ok);
  el.classList.toggle("hidden", false);
  el.innerHTML = `
    <span class="workspace-alert-title">${escapeHtml(title)}</span>
    <p class="workspace-alert-body">${escapeHtml(message)}</p>
    <button type="button" class="btn secondary dismiss-alert">Dismiss</button>
  `;
  el.querySelector(".dismiss-alert")?.addEventListener("click", () => {
    el.classList.add("hidden");
    el.innerHTML = "";
  });
}

function addBubble(kind, text, html = false) {
  const div = document.createElement("div");
  div.className = `bubble ${kind}`;
  if (html) div.innerHTML = text;
  else div.textContent = text;
  transcript.appendChild(div);
  transcript.scrollTop = transcript.scrollHeight;
  return div;
}

function createActivityBubble() {
  const div = document.createElement("div");
  div.className = "bubble activity";
  div.innerHTML = `
    <div class="activity-row">
      <span class="spinner" aria-hidden="true"></span>
      <span class="activity-text">Agent is working…</span>
    </div>
    <div class="activity-detail"></div>
  `;
  transcript.appendChild(div);
  transcript.scrollTop = transcript.scrollHeight;
  return div;
}

function setActivity(bubble, main, detail = "") {
  const t = bubble.querySelector(".activity-text");
  const d = bubble.querySelector(".activity-detail");
  if (t) t.textContent = main;
  if (d) {
    d.textContent = detail || "";
    d.style.display = detail ? "block" : "none";
  }
  transcript.scrollTop = transcript.scrollHeight;
}

function setRunStatus(active, text) {
  const bar = document.getElementById("runStatus");
  const label = document.getElementById("runStatusText");
  const btn = document.getElementById("sendBtn");
  if (!bar || !label) return;
  if (active) {
    bar.classList.remove("hidden");
    label.textContent = text || "Working…";
    btn.classList.add("is-running");
    btn.textContent = "Running…";
  } else {
    bar.classList.add("hidden");
    btn.classList.remove("is-running");
    btn.textContent = "Run agent";
  }
}

async function loadHealth() {
  try {
    const r = await fetch("/api/health");
    const data = await r.json();
    if (data.ok) {
      healthStatus.textContent = `Ollama connected · ${data.models.length} model(s)`;
      healthStatus.className = "status ok";
    } else {
      healthStatus.textContent = `Ollama unreachable: ${data.error || "unknown"}`;
      healthStatus.className = "status bad";
    }
  } catch (e) {
    healthStatus.textContent = `API error: ${e.message}`;
    healthStatus.className = "status bad";
  }
}

async function loadModels() {
  const r = await fetch("/api/models");
  const data = await r.json();
  const models = data.models || [];
  const current = modelSelect.value;
  modelSelect.innerHTML = "";
  if (!models.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(no models — pull one)";
    modelSelect.appendChild(opt);
    return;
  }
  for (const m of models) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    modelSelect.appendChild(opt);
  }
  if (current && models.includes(current)) modelSelect.value = current;
}

document.getElementById("refreshModels").addEventListener("click", loadModels);

document.getElementById("pullBtn").addEventListener("click", async () => {
  const name = document.getElementById("pullModel").value.trim();
  if (!name) return;
  const log = document.getElementById("pullLog");
  log.textContent = `Pulling ${name}…\n`;
  const r = await fetch("/api/models/pull", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: name }),
  });
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value);
    for (const line of chunk.split("\n")) {
      if (!line.trim()) continue;
      try {
        const j = JSON.parse(line);
        if (j.status) log.textContent += j.status + "\n";
        if (j.error) log.textContent += `Error: ${j.error}\n`;
      } catch (_) {
        log.textContent += line + "\n";
      }
    }
  }
  await loadModels();
  modelSelect.value = name;
});

document.getElementById("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = promptEl.value.trim();
  if (!message) return;

  sendBtn.disabled = true;
  addBubble("user", message);
  promptEl.value = "";

  const activity = createActivityBubble();
  let activityTimer = null;
  const startedAt = Date.now();
  activityTimer = setInterval(() => {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    setActivity(activity, `Agent is working… (${s}s)`, activity?.querySelector(".activity-detail")?.textContent || "");
  }, 1000);
  setRunStatus(true, "Agent is working…");

  const body = {
    message,
    session_id: sessionId,
    model: modelSelect.value || undefined,
  };

  try {
    const r = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let assistantBubble = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        if (!part.startsWith("data: ")) continue;
        const payload = JSON.parse(part.slice(6));
        if (payload.type === "session") {
          sessionId = payload.session_id;
          sessionLabel.textContent = `Session ${sessionId.slice(0, 8)}…`;
        } else if (payload.type === "thinking") {
          const step = payload.data.step ?? "?";
          setActivity(activity, `Thinking · step ${step}`, "Waiting for the model…");
          setRunStatus(true, `Step ${step} · thinking…`);
        } else if (payload.type === "status") {
          const msg = payload.data.message || "Working…";
          const phase = payload.data.phase || "";
          setActivity(activity, msg, phase ? `Phase: ${phase}` : "");
          setRunStatus(true, msg);
        } else if (payload.type === "tool_call") {
          setActivity(
            activity,
            `Tool: ${payload.data.name}`,
            "Executing…"
          );
          setRunStatus(true, `Running ${payload.data.name}…`);
          const args = payload.data.arguments || {};
          const brief = Object.entries(args)
            .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
            .join(", ");
          addBubble(
            "tool",
            brief
              ? `▸ ${payload.data.name} (${brief})`
              : `▸ ${payload.data.name}`,
          );
        } else if (payload.type === "tool_result") {
          const preview = (payload.data.result || "").slice(0, 280);
          addBubble("tool", `✓ ${payload.data.name}\n${preview}`);
          setActivity(activity, `Finished ${payload.data.name}`, "Waiting for model…");
        } else if (payload.type === "assistant_message") {
          assistantBubble = addBubble("assistant", payload.data.content);
        } else if (payload.type === "done") {
          activity.remove();
        }
      }
    }
    if (!assistantBubble) activity.remove();
  } catch (err) {
    activity.remove();
    addBubble("assistant", `Error: ${err.message}`);
  } finally {
    if (activityTimer) clearInterval(activityTimer);
    setRunStatus(false);
    sendBtn.disabled = false;
    if (activity.parentNode) activity.remove();
  }
});

let selectedRepo = null;
let repoLocked = false;

async function loadGithubRepos(preselect) {
  const select = document.getElementById("repoSelect");
  if (repoLocked) return;
  select.disabled = true;
  const first = select.options[0];
  select.innerHTML = "";
  if (first) select.appendChild(first);
  else {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Loading…";
    select.appendChild(opt);
  }
  try {
    const r = await fetch("/api/github/repos");
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    select.innerHTML = "";
    const repos = data.repos || [];
    if (!repos.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(no repositories)";
      select.appendChild(opt);
      select.disabled = true;
      return;
    }
    const pick = repoFullName(preselect || data.selected || selectedRepo);
    for (const repo of repos) {
      const opt = document.createElement("option");
      opt.value = repo.full_name;
      opt.textContent = repo.private ? `${repo.full_name} (private)` : repo.full_name;
      select.appendChild(opt);
    }
    if (pick && repos.some((x) => x.full_name === pick)) {
      select.value = pick;
      selectedRepo = pick;
    }
    select.disabled = false;
  } catch (e) {
    select.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = `Error: ${e.message}`;
    select.appendChild(opt);
  }
}

async function setGithubRepo(fullName) {
  if (!fullName || repoLocked) return;
  const r = await fetch("/api/github/repo", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo: fullName }),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
  selectedRepo = repoFullName(data.repo);
  if (data.clone) {
    const ws = document.getElementById("workspaceStatus");
    if (data.clone.ok) {
      showWorkspaceAlert(
        `${data.clone.action} ${data.clone.repo || data.repo} into workspace.`,
        { title: "Workspace updated", ok: true }
      );
      ws.textContent = `Workspace: ${data.clone.action} ${data.repo}`;
      ws.className = "status ok";
    } else {
      const title =
        data.clone.action === "blocked"
          ? "Could not add repo to workspace"
          : "Clone failed";
      const extra =
        data.clone.action === "blocked"
          ? "\n\nUse **Replace workspace & clone** to wipe /workspace and clone the selected repo."
          : "";
      showWorkspaceAlert(workspaceCloneMessage(data.clone) + extra, { title });
      ws.textContent = data.clone.hint || title;
      ws.className = "status bad";
    }
  }
  await loadGithub();
  await loadWorkspaceStatus();
}

async function loadWorkspaceStatus() {
  const el = document.getElementById("workspaceStatus");
  if (!el) return;
  try {
    const r = await fetch("/api/workspace");
    const data = await r.json();
    if (data.hint) {
      el.textContent = data.hint;
      el.className = data.synced && !data.empty ? "status ok" : "status bad";
    } else if (data.synced) {
      el.textContent = `Workspace in sync with ${data.selected_repo}`;
      el.className = "status ok";
    } else {
      el.textContent = `Workspace: ${data.path}`;
      el.className = "status";
    }
  } catch (e) {
    el.textContent = "";
  }
}

async function runWorkspaceReplace() {
  const repo = repoFullName(
    selectedRepo || document.getElementById("repoSelect")?.value || ""
  );
  if (!repo) {
    showWorkspaceAlert("Pick a repository first.", { title: "No repository" });
    return;
  }
  const ok = window.confirm(
    `DELETE all files under /workspace and clone ${repo}?\n\n` +
      "This cannot be undone. Unraid files on the host mount will be removed."
  );
  if (!ok) return;

  const el = document.getElementById("workspaceStatus");
  showWorkspaceAlert(null);
  el.textContent = "Clearing workspace and cloning…";
  el.className = "status";
  try {
    const r = await fetch("/api/workspace/replace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true, repo }),
    });
    const data = await r.json();
    if (!r.ok) {
      const msg = workspaceCloneMessage(data);
      showWorkspaceAlert(msg, { title: "Replace workspace failed" });
      el.textContent = "Replace failed";
      el.className = "status bad";
      await loadWorkspaceStatus();
      return;
    }
    const n = data.cleared?.removed?.length ?? 0;
    showWorkspaceAlert(
      data.message || `Cloned ${data.repo} (removed ${n} items).`,
      { title: "Workspace replaced", ok: true }
    );
    el.textContent = `Workspace: ${data.repo} (${data.action})`;
    el.className = "status ok";
  } catch (e) {
    showWorkspaceAlert(String(e.message), { title: "Replace workspace failed" });
    el.textContent = "Replace failed";
    el.className = "status bad";
  }
  await loadWorkspaceStatus();
}

document.getElementById("replaceWorkspace").addEventListener("click", () => {
  runWorkspaceReplace();
});

document.getElementById("cloneWorkspace").addEventListener("click", async () => {
  const el = document.getElementById("workspaceStatus");
  showWorkspaceAlert(null);
  el.textContent = "Cloning / pulling…";
  el.className = "status";
  try {
    const r = await fetch("/api/workspace/clone", { method: "POST" });
    const data = await r.json();
    if (!r.ok) {
      const msg = workspaceCloneMessage(data);
      const title =
        data.detail?.action === "blocked" || data.action === "blocked"
          ? "Could not clone — workspace not empty"
          : "Clone failed";
      showWorkspaceAlert(msg, { title });
      el.textContent = title;
      el.className = "status bad";
      await loadWorkspaceStatus();
      return;
    }
    showWorkspaceAlert(`${data.action}: ${data.repo}`, {
      title: "Workspace updated",
      ok: true,
    });
    el.textContent = `${data.action}: ${data.repo}`;
    el.className = "status ok";
  } catch (e) {
    showWorkspaceAlert(String(e.message), { title: "Clone failed" });
    el.textContent = "Clone failed";
    el.className = "status bad";
  }
  await loadWorkspaceStatus();
});

async function loadGithub() {
  const el = document.getElementById("githubStatus");
  const select = document.getElementById("repoSelect");
  try {
    const r = await fetch("/api/github/status");
    const data = await r.json();
    repoLocked = Boolean(data.repo_locked);
    selectedRepo = repoFullName(
      data.repo_selected_ui ||
        data.repo ||
        data.repo_detected_workspace ||
        data.repo_details?.full_name ||
        null
    ) || null;

    if (!data.token_configured) {
      el.textContent =
        "Set AGENT_GITHUB_TOKEN on the container, then pick a repo below (like Cursor Cloud)";
      el.className = "status bad";
      select.disabled = true;
      return;
    }
    if (data.ok === false) {
      el.textContent = `GitHub error: ${data.error || "auth failed"}`;
      el.className = "status bad";
    } else if (data.configured && data.login) {
      const repo =
        data.repo?.full_name || data.repo || selectedRepo || "no repo selected";
      el.textContent = `GitHub: ${data.login} @ ${repo}`;
      el.className = "status ok";
    } else {
      el.textContent = `Signed in as ${data.login || "…"} — choose a repository`;
      el.className = "status";
    }

    if (repoLocked) {
      select.disabled = true;
      if (selectedRepo) {
        select.innerHTML = "";
        const opt = document.createElement("option");
        opt.value = selectedRepo;
        opt.textContent = `${selectedRepo} (from AGENT_GITHUB_REPO)`;
        select.appendChild(opt);
        select.value = selectedRepo;
      }
      return;
    }
    await loadGithubRepos(selectedRepo);
    await loadWorkspaceStatus();
  } catch (e) {
    el.textContent = `GitHub check failed: ${e.message}`;
    el.className = "status bad";
  }
}

document.getElementById("refreshRepos").addEventListener("click", () =>
  loadGithubRepos(selectedRepo)
);
document.getElementById("repoSelect").addEventListener("change", async (e) => {
  const value = e.target.value;
  if (!value) return;
  try {
    await setGithubRepo(value);
  } catch (err) {
    const el = document.getElementById("githubStatus");
    el.textContent = `Could not set repo: ${err.message}`;
    el.className = "status bad";
    showWorkspaceAlert(err.message, { title: "Could not set repository" });
  }
});

async function loadJobs() {
  const panel = document.getElementById("jobsPanel");
  const r = await fetch("/api/github/jobs");
  const data = await r.json();
  const jobs = data.jobs || [];
  if (!jobs.length) {
    panel.textContent = "";
    return;
  }
  panel.innerHTML = jobs
    .slice(0, 5)
    .map(
      (j) =>
        `#${j.issue_number} [${j.status}] ${j.branch || ""} ${
          j.pr_url ? `<a href="${j.pr_url}">PR</a>` : ""
        }`
    )
    .join("<br/>");
}

async function loadIssues() {
  const list = document.getElementById("issueList");
  list.innerHTML = "<li>Loading…</li>";
  try {
    const r = await fetch("/api/github/issues");
    const data = await r.json();
    const issues = data.issues || [];
    if (!issues.length) {
      list.innerHTML = "<li>No open issues</li>";
      return;
    }
    list.innerHTML = "";
    for (const issue of issues) {
      const li = document.createElement("li");
      const title = document.createElement("span");
      title.textContent = `#${issue.number} ${issue.title}`;
      const btn = document.createElement("button");
      btn.className = "btn secondary";
      btn.textContent = "Run";
      btn.onclick = () => runIssue(issue.number, issue.title);
      li.appendChild(title);
      li.appendChild(btn);
      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = `<li>${e.message}</li>`;
  }
}

async function runIssue(number, title) {
  if (!number) return;
  addBubble(
    "user",
    `Run GitHub agent on issue #${number}${title ? ": " + title : ""}`
  );
  const r = await fetch("/api/github/run-issue", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ issue_number: Number(number) }),
  });
  const data = await r.json();
  if (!r.ok) {
    addBubble("assistant", `Failed: ${JSON.stringify(data)}`);
    return;
  }
  addBubble(
    "assistant",
    `Queued job ${data.job_id} on branch ${data.branch}. Watch sidebar jobs or issue comments.`
  );
  await loadJobs();
  setInterval(loadJobs, 5000);
}

document.getElementById("refreshIssues").addEventListener("click", loadIssues);
document.getElementById("runIssueBtn").addEventListener("click", () => {
  const n = document.getElementById("issueNumber").value;
  runIssue(n);
});

function parseVersionParts(v) {
  return String(v || "")
    .split(".")
    .map((n) => parseInt(n, 10) || 0);
}

function versionOlderThan(current, minimum) {
  const a = parseVersionParts(current);
  const b = parseVersionParts(minimum);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (x < y) return true;
    if (x > y) return false;
  }
  return false;
}

const RECOMMENDED_AGENT_VERSION = "0.3.12";

async function loadAppMeta() {
  const el = document.getElementById("appVersion");
  if (!el) return;
  try {
    const r = await fetch("/api/config", { cache: "no-store" });
    const data = await r.json();
    const v = data.version || "";
    el.style.color = "";
    el.textContent = v ? `Agent v${v}` : "";
    if (v && versionOlderThan(v, RECOMMENDED_AGENT_VERSION)) {
      el.textContent += ` — pull :${RECOMMENDED_AGENT_VERSION}, Stop → Edit → Apply`;
      el.style.color = "var(--danger)";
    }
  } catch (_) {
    el.textContent = "";
  }
}

loadHealth();
loadModels();
loadGithub();
loadJobs();
loadAppMeta();
loadWorkspaceStatus();

function setSidebarOpen(open) {
  document.body.classList.toggle("sidebar-open", open);
  const backdrop = document.getElementById("sidebarBackdrop");
  if (backdrop) backdrop.style.display = open ? "block" : "none";
}

const sidebarToggle = document.getElementById("sidebarToggle");
const sidebarClose = document.getElementById("sidebarClose");
const sidebarBackdrop = document.getElementById("sidebarBackdrop");
if (sidebarToggle) {
  sidebarToggle.addEventListener("click", () => setSidebarOpen(true));
}
if (sidebarClose) {
  sidebarClose.addEventListener("click", () => setSidebarOpen(false));
}
if (sidebarBackdrop) {
  sidebarBackdrop.addEventListener("click", () => setSidebarOpen(false));
}
