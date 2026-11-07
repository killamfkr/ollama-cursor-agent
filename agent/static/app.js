let sessionId = null;

const transcript = document.getElementById("transcript");
const promptEl = document.getElementById("prompt");
const modelSelect = document.getElementById("modelSelect");
const healthStatus = document.getElementById("healthStatus");
const sessionLabel = document.getElementById("sessionLabel");
const sendBtn = document.getElementById("sendBtn");

function addBubble(kind, text, html = false) {
  const div = document.createElement("div");
  div.className = `bubble ${kind}`;
  if (html) div.innerHTML = text;
  else div.textContent = text;
  transcript.appendChild(div);
  transcript.scrollTop = transcript.scrollHeight;
  return div;
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

  const thinking = addBubble("thinking", "Agent is working…");

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
        } else if (payload.type === "tool_call") {
          const args = JSON.stringify(payload.data.arguments || {}, null, 2);
          addBubble(
            "tool",
            `▸ ${payload.data.name}\n${args}`,
          );
        } else if (payload.type === "tool_result") {
          addBubble("tool", `✓ ${payload.data.name}\n${payload.data.result}`);
        } else if (payload.type === "assistant_message") {
          thinking.remove();
          assistantBubble = addBubble("assistant", payload.data.content);
        } else if (payload.type === "done") {
          thinking.remove();
        }
      }
    }
    if (!assistantBubble) thinking.remove();
  } catch (err) {
    thinking.remove();
    addBubble("assistant", `Error: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
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
    const pick = preselect || data.selected || selectedRepo;
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
  selectedRepo = data.repo;
  await loadGithub();
}

async function loadGithub() {
  const el = document.getElementById("githubStatus");
  const select = document.getElementById("repoSelect");
  try {
    const r = await fetch("/api/github/status");
    const data = await r.json();
    repoLocked = Boolean(data.repo_locked);
    selectedRepo = data.repo || data.repo_selected_ui || data.repo_detected_workspace || null;

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

loadHealth();
loadModels();
loadGithub();
loadJobs();
