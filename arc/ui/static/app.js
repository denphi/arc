const state = {
  sessions: [],
  commands: [],
  activeSessionId: null,
  activeSession: null,
  selectedArtifactId: null,
  selectedArtifactVersion: "0.1.0",
  selectedResultId: null,
  authToken: window.sessionStorage.getItem("arc.ui.apiToken") || "",
  busy: false,
};

const el = {
  appFrame: document.querySelector(".app-frame"),
  appShell: document.querySelector(".app-shell"),
  health: document.getElementById("healthLabel"),
  toggleSidebar: document.getElementById("toggleSidebar"),
  toggleInspector: document.getElementById("toggleInspector"),
  sidebarResizer: document.getElementById("sidebarResizer"),
  inspectorResizer: document.getElementById("inspectorResizer"),
  openConfig: document.getElementById("openConfig"),
  openConfigFooter: document.getElementById("openConfigFooter"),
  configModal: document.getElementById("configModal"),
  configForm: document.getElementById("configForm"),
  closeConfig: document.getElementById("closeConfig"),
  cancelConfig: document.getElementById("cancelConfig"),
  envPath: document.getElementById("envPath"),
  toolsToggle: document.getElementById("toolsToggle"),
  toolsPopover: document.getElementById("toolsPopover"),
  refreshSessions: document.getElementById("refreshSessions"),
  newSession: document.getElementById("newSession"),
  sessionList: document.getElementById("sessionList"),
  commandsList: document.getElementById("commandsList"),
  sessionTitle: document.getElementById("sessionTitle"),
  sessionMeta: document.getElementById("sessionMeta"),
  reloadSession: document.getElementById("reloadSession"),
  threadList: document.getElementById("threadList"),
  messageForm: document.getElementById("messageForm"),
  messageInput: document.getElementById("messageInput"),
  domainInput: document.getElementById("domainInput"),
  iterationsInput: document.getElementById("iterationsInput"),
  providerInput: document.getElementById("providerInput"),
  modelInput: document.getElementById("modelInput"),
  baseUrlInput: document.getElementById("baseUrlInput"),
  apiTokenInput: document.getElementById("apiTokenInput"),
  targetInput: document.getElementById("targetInput"),
  busyState: document.getElementById("busyState"),
  artifactCount: document.getElementById("artifactCount"),
  artifactList: document.getElementById("artifactList"),
  resultCount: document.getElementById("resultCount"),
  resultList: document.getElementById("resultList"),
  selectedArtifact: document.getElementById("selectedArtifact"),
  executionInputs: document.getElementById("executionInputs"),
  runExecution: document.getElementById("runExecution"),
  detailView: document.getElementById("detailView"),
  toast: document.getElementById("toast"),
};

el.apiTokenInput.value = state.authToken;

const layoutBounds = {
  drawer: { min: 210, max: 520, variable: "--sidebar-width", storage: "arc.ui.sidebarWidth" },
  sidecar: { min: 280, max: 560, variable: "--inspector-width", storage: "arc.ui.inspectorWidth" },
};

const trashIcon = `
  <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
    <path d="M3 6h18"></path>
    <path d="M8 6V4h8v2"></path>
    <path d="M19 6l-1 14H6L5 6"></path>
    <path d="M10 11v5"></path>
    <path d="M14 11v5"></path>
  </svg>
`;

async function api(path, options = {}) {
  const token = (state.authToken || el.apiTokenInput.value).trim();
  if (token) {
    window.sessionStorage.setItem("arc.ui.apiToken", token);
  } else {
    window.sessionStorage.removeItem("arc.ui.apiToken");
  }

  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
    ...options,
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (error) {
    data = { detail: text };
  }
  if (!response.ok) {
    const detail = data && data.detail ? data.detail : response.statusText;
    throw new Error(detail);
  }
  return data;
}

function showToast(message, kind = "info") {
  el.toast.textContent = message;
  el.toast.classList.toggle("error", kind === "error");
  el.toast.classList.add("show");
  window.setTimeout(() => el.toast.classList.remove("show"), 3600);
}

function setBusy(isBusy, label = "idle") {
  state.busy = isBusy;
  el.busyState.textContent = label;
  el.busyState.classList.toggle("busy", isBusy);
  for (const control of document.querySelectorAll("button, input, select, textarea")) {
    control.disabled = isBusy;
  }
}

function configFields() {
  return Array.from(document.querySelectorAll("[data-env-key]"));
}

function applyLayoutPrefs() {
  for (const [kind, bounds] of Object.entries(layoutBounds)) {
    const raw = window.localStorage.getItem(bounds.storage);
    const saved = raw === null ? Number.NaN : Number(raw);
    if (Number.isFinite(saved)) {
      setPanelWidth(kind, saved);
    }
  }
  setPanelCollapsed("drawer", window.localStorage.getItem("arc.ui.drawerCollapsed") === "true");
  setPanelCollapsed("sidecar", window.localStorage.getItem("arc.ui.sidecarCollapsed") === "true");
}

function setPanelWidth(kind, width) {
  const bounds = layoutBounds[kind];
  const max = Math.min(bounds.max, Math.max(bounds.min, window.innerWidth - 420));
  const next = Math.round(Math.min(Math.max(width, bounds.min), max));
  el.appFrame.style.setProperty(bounds.variable, `${next}px`);
  window.localStorage.setItem(bounds.storage, String(next));
}

function setPanelCollapsed(kind, collapsed) {
  const className = kind === "drawer" ? "drawer-collapsed" : "sidecar-collapsed";
  const storage = kind === "drawer" ? "arc.ui.drawerCollapsed" : "arc.ui.sidecarCollapsed";
  el.appFrame.classList.toggle(className, collapsed);
  window.localStorage.setItem(storage, String(collapsed));
  updateCollapseButtons();
}

function updateCollapseButtons() {
  const drawerCollapsed = el.appFrame.classList.contains("drawer-collapsed");
  const sidecarCollapsed = el.appFrame.classList.contains("sidecar-collapsed");
  el.toggleSidebar.setAttribute("aria-pressed", String(drawerCollapsed));
  el.toggleSidebar.setAttribute(
    "aria-label",
    drawerCollapsed ? "Expand sessions drawer" : "Collapse sessions drawer",
  );
  el.toggleInspector.setAttribute("aria-pressed", String(sidecarCollapsed));
  el.toggleInspector.setAttribute(
    "aria-label",
    sidecarCollapsed ? "Expand sidecar" : "Collapse sidecar",
  );
}

function startResize(kind, event) {
  if (event.button !== 0) {
    return;
  }
  event.preventDefault();
  closeTools();
  const bounds = layoutBounds[kind];
  const startX = event.clientX;
  const current = Number.parseFloat(getComputedStyle(el.appFrame).getPropertyValue(bounds.variable));
  const startWidth = Number.isFinite(current) ? current : bounds.min;
  el.appFrame.classList.add(kind === "drawer" ? "resizing-drawer" : "resizing-sidecar");

  const onMove = (moveEvent) => {
    const delta = moveEvent.clientX - startX;
    setPanelWidth(kind, kind === "drawer" ? startWidth + delta : startWidth - delta);
  };
  const onUp = () => {
    el.appFrame.classList.remove("resizing-drawer", "resizing-sidecar");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  };

  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp, { once: true });
}

function openTools() {
  el.toolsPopover.hidden = false;
  el.toolsToggle.setAttribute("aria-expanded", "true");
}

function closeTools() {
  el.toolsPopover.hidden = true;
  el.toolsToggle.setAttribute("aria-expanded", "false");
}

function toggleTools() {
  if (el.toolsPopover.hidden) {
    openTools();
  } else {
    closeTools();
  }
}

function openConfigModal() {
  closeTools();
  el.configModal.hidden = false;
  el.configModal.classList.add("show");
  loadConfig().catch((error) => showToast(error.message, "error"));
  window.setTimeout(() => el.providerInput.focus(), 0);
}

function closeConfigModal() {
  el.configModal.classList.remove("show");
  el.configModal.hidden = true;
}

async function loadConfig({ silent = false } = {}) {
  try {
    const config = await api("/api/config");
    syncConfig(config);
  } catch (error) {
    if (!silent) {
      throw error;
    }
  }
}

function syncConfig(config) {
  el.envPath.textContent = config.path || ".env";
  const values = config.values || {};
  for (const field of configFields()) {
    const key = field.dataset.envKey;
    if (Object.prototype.hasOwnProperty.call(values, key)) {
      field.value = values[key] || "";
    }
  }
  const storedToken = window.sessionStorage.getItem("arc.ui.apiToken") || "";
  if (!el.apiTokenInput.value && storedToken) {
    el.apiTokenInput.value = storedToken;
  }
}

async function saveConfig(event) {
  event.preventDefault();
  const values = {};
  for (const field of configFields()) {
    values[field.dataset.envKey] = field.value.trim();
  }
  setBusy(true, "saving");
  try {
    const config = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ values }),
    });
    state.authToken = values.ARC_API_TOKEN || "";
    if (state.authToken) {
      window.sessionStorage.setItem("arc.ui.apiToken", state.authToken);
    } else {
      window.sessionStorage.removeItem("arc.ui.apiToken");
    }
    syncConfig(config);
    closeConfigModal();
    showToast("Configuration saved");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function asJson(text, fallback) {
  const value = text.trim();
  if (!value) {
    return fallback;
  }
  return JSON.parse(value);
}

function renderJson(value) {
  el.detailView.textContent = JSON.stringify(value || {}, null, 2);
}

function sessionLabel(session) {
  return session.goal || session.session_id;
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    el.health.textContent = `v${health.version}`;
  } catch (error) {
    el.health.textContent = "offline";
  }
}

async function loadCommands() {
  const payload = await api("/api/commands");
  state.commands = payload.commands || [];
  renderCommands();
}

async function loadSessions() {
  const payload = await api("/api/sessions");
  state.sessions = payload.sessions || [];
  renderSessions();
  if (!state.activeSessionId && state.sessions.length) {
    await selectSession(state.sessions[0].session_id);
  }
}

function renderSessions() {
  el.sessionList.textContent = "";
  if (!state.sessions.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No sessions";
    el.sessionList.appendChild(empty);
    return;
  }
  for (const session of state.sessions) {
    const row = document.createElement("div");
    row.className = "session-row";
    row.classList.toggle("active", session.session_id === state.activeSessionId);

    const main = document.createElement("button");
    main.className = "session-main";
    main.type = "button";
    main.innerHTML = `
      <strong>${escapeHtml(sessionLabel(session))}</strong>
      <span>${escapeHtml(session.session_id)} · iteration ${session.iteration || 0}</span>
    `;
    main.addEventListener("click", () => selectSession(session.session_id));

    const remove = document.createElement("button");
    remove.className = "session-delete";
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete session ${session.session_id}`);
    remove.innerHTML = trashIcon;
    remove.addEventListener("click", () => deleteSession(session.session_id));

    row.appendChild(main);
    row.appendChild(remove);
    el.sessionList.appendChild(row);
  }
}

function renderCommands() {
  el.commandsList.textContent = "";
  const groups = new Map();
  for (const command of state.commands) {
    const group = command.group || "Commands";
    if (!groups.has(group)) {
      groups.set(group, []);
    }
    groups.get(group).push(command);
  }
  for (const [group, commands] of groups.entries()) {
    const heading = document.createElement("p");
    heading.className = "command-group";
    heading.textContent = group;
    el.commandsList.appendChild(heading);
    for (const command of commands) {
      const button = document.createElement("button");
      button.className = "command-button";
      button.type = "button";
      button.innerHTML = `
        <span class="command-row">
          <span class="flat-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <path d="M4 7h16"></path>
              <path d="M4 12h10"></path>
              <path d="M4 17h16"></path>
            </svg>
          </span>
          <span class="command-copy">
            <strong>${escapeHtml(command.usage)}</strong>
            <span>${escapeHtml(command.summary)}</span>
          </span>
        </span>
      `;
      button.addEventListener("click", () => {
        el.messageInput.value = command.name === "help" ? "/help" : `/${command.name} `;
        closeTools();
        el.messageInput.focus();
      });
      el.commandsList.appendChild(button);
    }
  }
}

async function createSession() {
  setBusy(true, "creating");
  try {
    const detail = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ prefix: "ui", goal: "" }),
    });
    state.activeSessionId = detail.session_id;
    state.activeSession = detail;
    await loadSessions();
    await selectSession(detail.session_id);
    showToast("Session created");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function deleteSession(sessionId) {
  if (!window.confirm(`Delete session ${sessionId}?`)) {
    return;
  }
  setBusy(true, "deleting");
  try {
    await api(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    if (state.activeSessionId === sessionId) {
      state.activeSessionId = null;
      state.activeSession = null;
      renderSession();
    }
    await loadSessions();
    if (!state.activeSessionId && state.sessions.length) {
      await selectSession(state.sessions[0].session_id);
    }
    showToast("Session deleted");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function selectSession(sessionId) {
  state.activeSessionId = sessionId;
  const detail = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
  state.activeSession = detail;
  state.selectedArtifactId = null;
  state.selectedResultId = null;
  renderSession();
  renderSessions();
}

function renderSession() {
  const detail = state.activeSession;
  if (!detail) {
    el.sessionTitle.textContent = "No session selected";
    el.sessionMeta.textContent = "Create or select a session.";
    el.threadList.textContent = "";
    el.artifactList.textContent = "";
    el.resultList.textContent = "";
    el.artifactCount.textContent = "0";
    el.resultCount.textContent = "0";
    el.selectedArtifact.textContent = "No artifact selected";
    renderJson({});
    return;
  }

  const meta = detail.meta || {};
  el.sessionTitle.textContent = meta.goal || detail.session_id;
  el.sessionMeta.textContent = `${detail.session_id} · iteration ${meta.iteration || 0}`;
  el.targetInput.value = Object.keys(meta.target || {}).length
    ? JSON.stringify(meta.target, null, 2)
    : "";
  renderThread(detail.thread || []);
  renderArtifacts(detail.artifacts || []);
  renderResults(detail.results || []);
  renderJson({ session: detail.session_id, meta, state: detail.state || {} });
}

function renderThread(messages) {
  el.threadList.textContent = "";
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "thread-empty";
    empty.innerHTML = `
      <div class="welcome-mark">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 2 2 7l10 5 10-5-10-5z"></path>
          <path d="M2 17l10 5 10-5"></path>
          <path d="M2 12l10 5 10-5"></path>
        </svg>
      </div>
      <h2>How can I help?</h2>
      <p>Ask ARC to run research, inspect artifacts, or type a slash command.</p>
    `;
    el.threadList.appendChild(empty);
    return;
  }
  for (const message of messages) {
    const article = document.createElement("article");
    article.className = `message ${message.role || "system"}`;
    const hasPayload = message.payload && Object.keys(message.payload).length > 0;
    const role = message.role || "system";
    const avatar = role === "user" ? "U" : role === "assistant" ? "ARC" : role === "tool" ? "RUN" : "!";
    article.innerHTML = `
      <div class="message-meta">
        <span class="message-avatar">${escapeHtml(avatar)}</span>
        <strong>${escapeHtml(message.title || message.role || "Message")}</strong>
        <span>${formatTime(message.ts)}</span>
      </div>
      <div class="message-bubble"><p>${escapeHtml(message.content || "")}</p></div>
    `;
    if (hasPayload) {
      appendPayloadPills(article, message.payload);
    }
    el.threadList.appendChild(article);
  }
  el.threadList.scrollTop = el.threadList.scrollHeight;
}

function appendPayloadPills(article, payload) {
  const sections = payloadSections(payload);
  if (!sections.length) {
    const inspect = document.createElement("button");
    inspect.className = "inline-button";
    inspect.type = "button";
    inspect.textContent = "Raw JSON";
    inspect.addEventListener("click", () => renderJson(payload));
    article.appendChild(inspect);
    return;
  }

  const wrapper = document.createElement("div");
  wrapper.className = "message-data";
  const row = document.createElement("div");
  row.className = "message-data-pills";
  const panel = document.createElement("pre");
  panel.className = "message-data-panel hidden";
  panel.setAttribute("aria-live", "polite");

  for (const section of sections) {
    const pill = document.createElement("button");
    pill.className = `data-pill ${section.kind}`;
    pill.type = "button";
    pill.setAttribute("aria-expanded", "false");
    pill.innerHTML = `
      <span>${escapeHtml(section.label)}</span>
      <strong>${escapeHtml(section.badge)}</strong>
    `;
    pill.addEventListener("click", () => {
      const expanded = pill.getAttribute("aria-expanded") === "true";
      for (const other of row.querySelectorAll(".data-pill")) {
        other.setAttribute("aria-expanded", "false");
      }
      if (expanded) {
        panel.classList.add("hidden");
        panel.textContent = "";
        return;
      }
      pill.setAttribute("aria-expanded", "true");
      panel.textContent = JSON.stringify(section.value, null, 2);
      panel.classList.remove("hidden");
      renderJson(section.value);
    });
    row.appendChild(pill);
  }

  const full = document.createElement("button");
  full.className = "data-pill full";
  full.type = "button";
  full.innerHTML = "<span>Raw JSON</span><strong>{ }</strong>";
  full.addEventListener("click", () => renderJson(payload));
  row.appendChild(full);

  wrapper.appendChild(row);
  wrapper.appendChild(panel);
  article.appendChild(wrapper);
}

function payloadSections(payload) {
  const inputs = firstObject(
    payload.inputs,
    nested(payload, "execution", "inputs"),
    nested(payload, "result", "inputs"),
    nested(payload, "result", "execution", "inputs"),
  );
  const outputs = firstObject(
    payload.outputs,
    nested(payload, "execution", "outputs"),
    nested(payload, "result", "outputs"),
    nested(payload, "result", "execution", "outputs"),
  );
  const metrics = firstObject(
    payload.metrics,
    nested(payload, "execution", "metrics"),
    nested(payload, "result", "execution", "metrics"),
  );
  const review = firstObject(payload.review, nested(payload, "result", "review"));
  const sections = [];
  if (inputs) {
    sections.push({ kind: "inputs", label: "Inputs", badge: objectBadge(inputs), value: inputs });
  }
  if (outputs) {
    sections.push({ kind: "outputs", label: "Outputs", badge: objectBadge(outputs), value: outputs });
  }
  if (metrics) {
    sections.push({ kind: "metrics", label: "Metrics", badge: objectBadge(metrics), value: metrics });
  }
  if (review) {
    sections.push({ kind: "review", label: "Review", badge: objectBadge(review), value: review });
  }
  return sections;
}

function nested(source, ...keys) {
  let value = source;
  for (const key of keys) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return null;
    }
    value = value[key];
  }
  return value;
}

function firstObject(...values) {
  return values.find((value) => value && typeof value === "object" && !Array.isArray(value)) || null;
}

function objectBadge(value) {
  const count = Object.keys(value).length;
  return `${count} ${count === 1 ? "item" : "items"}`;
}

function renderArtifacts(artifacts) {
  el.artifactCount.textContent = String(artifacts.length);
  el.artifactList.textContent = "";
  if (!artifacts.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No artifacts";
    el.artifactList.appendChild(empty);
    return;
  }
  for (const artifact of artifacts) {
    const button = document.createElement("button");
    button.className = "record-button";
    button.type = "button";
    button.classList.toggle("active", artifact.artifact_id === state.selectedArtifactId);
    button.innerHTML = `
      <strong>${escapeHtml(artifact.name)}</strong>
      <span>${escapeHtml(artifact.artifact_id)} · ${escapeHtml(artifact.version)}</span>
    `;
    button.addEventListener("click", () => {
      state.selectedArtifactId = artifact.artifact_id;
      state.selectedArtifactVersion = artifact.version;
      el.selectedArtifact.textContent = `${artifact.name} · ${artifact.version}`;
      el.executionInputs.value = JSON.stringify(defaultInputs(artifact), null, 2);
      renderArtifacts(artifacts);
      renderJson(artifact);
    });
    el.artifactList.appendChild(button);
  }
}

function renderResults(results) {
  el.resultCount.textContent = String(results.length);
  el.resultList.textContent = "";
  if (!results.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No results";
    el.resultList.appendChild(empty);
    return;
  }
  for (const result of results) {
    const button = document.createElement("button");
    button.className = "record-button";
    button.type = "button";
    button.classList.toggle("active", result.run_id === state.selectedResultId);
    button.innerHTML = `
      <strong>${escapeHtml(result.status)}</strong>
      <span>${escapeHtml(result.run_id)}</span>
    `;
    button.addEventListener("click", () => {
      state.selectedResultId = result.run_id;
      renderResults(results);
      renderJson(result);
    });
    el.resultList.appendChild(button);
  }
}

function defaultInputs(artifact) {
  const schema = (artifact.metadata && artifact.metadata.sim2l_inputs) || {};
  const values = {};
  for (const [key, field] of Object.entries(schema)) {
    values[key] = field && Object.prototype.hasOwnProperty.call(field, "default")
      ? field.default
      : 1.0;
  }
  return values;
}

function runPayload() {
  return {
    session_id: state.activeSessionId,
    domain: el.domainInput.value.trim() || null,
    iterations: Number(el.iterationsInput.value || 1),
    provider: el.providerInput.value || null,
    model: el.modelInput.value.trim() || null,
    base_url: el.baseUrlInput.value.trim() || null,
    target: asJson(el.targetInput.value, {}),
  };
}

async function sendMessage(event) {
  event.preventDefault();
  const content = el.messageInput.value.trim();
  if (!content) {
    return;
  }
  if (content.startsWith("/") || content.startsWith("\\")) {
    await runCommandText(content, "running");
    return;
  }
  setBusy(true, "running");
  try {
    const result = await api("/api/messages", {
      method: "POST",
      body: JSON.stringify({ ...runPayload(), content }),
    });
    state.activeSessionId = result.session_id;
    state.activeSession = result.session;
    el.messageInput.value = "";
    await loadSessions();
    await selectSession(result.session_id);
    renderJson(result.message);
    showToast("Run completed");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function runResearch(event) {
  return sendMessage(event);
}

async function runCommandText(command, label = "command") {
  if (isDestructiveCommand(command) && !window.confirm(`Run ${command}?`)) {
    return;
  }
  setBusy(true, label);
  try {
    const result = await api("/api/commands/run", {
      method: "POST",
      body: JSON.stringify({ ...runPayload(), command }),
    });
    if (result.session_id) {
      state.activeSessionId = result.session_id;
      state.activeSession = result.session;
    }
    el.messageInput.value = "";
    await loadSessions();
    if (result.session_id) {
      await selectSession(result.session_id);
    }
    renderJson(result.payload);
    showToast(result.text || "Command completed");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function runExecution() {
  if (!state.activeSessionId || !state.selectedArtifactId) {
    showToast("Select an artifact first", "error");
    return;
  }
  setBusy(true, "executing");
  try {
    const payload = {
      session_id: state.activeSessionId,
      artifact_id: state.selectedArtifactId,
      version: state.selectedArtifactVersion,
      inputs: asJson(el.executionInputs.value, {}),
    };
    const result = await api("/api/executions/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.activeSession = result.session;
    await selectSession(result.session_id);
    renderJson(result.execution);
    showToast("Execution completed");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function isDestructiveCommand(command) {
  const value = command.trim().toLowerCase();
  return /^\/(recipe|recipes|skills|skill)\s+(delete|remove|rm)\b/.test(value);
}

function formatTime(ts) {
  if (!ts) {
    return "";
  }
  try {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (error) {
    return "";
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

el.refreshSessions.addEventListener("click", () => {
  loadSessions().catch((error) => showToast(error.message, "error"));
});
el.newSession.addEventListener("click", createSession);
el.reloadSession.addEventListener("click", () => {
  if (state.activeSessionId) {
    selectSession(state.activeSessionId).catch((error) => showToast(error.message, "error"));
  }
});
el.messageForm.addEventListener("submit", sendMessage);
el.runExecution.addEventListener("click", runExecution);
el.toolsToggle.addEventListener("click", toggleTools);
el.openConfig.addEventListener("click", openConfigModal);
el.openConfigFooter.addEventListener("click", openConfigModal);
el.closeConfig.addEventListener("click", closeConfigModal);
el.cancelConfig.addEventListener("click", closeConfigModal);
el.configForm.addEventListener("submit", saveConfig);
el.toggleSidebar.addEventListener("click", () => {
  const collapsed = !el.appFrame.classList.contains("drawer-collapsed");
  setPanelCollapsed("drawer", collapsed);
});
el.toggleInspector.addEventListener("click", () => {
  const collapsed = !el.appFrame.classList.contains("sidecar-collapsed");
  setPanelCollapsed("sidecar", collapsed);
});
el.sidebarResizer.addEventListener("pointerdown", (event) => startResize("drawer", event));
el.inspectorResizer.addEventListener("pointerdown", (event) => startResize("sidecar", event));
el.messageInput.addEventListener("input", () => {
  el.messageInput.style.height = "auto";
  el.messageInput.style.height = `${Math.min(el.messageInput.scrollHeight, 180)}px`;
});
el.configModal.addEventListener("click", (event) => {
  if (event.target === el.configModal) {
    closeConfigModal();
  }
});
document.addEventListener("click", (event) => {
  if (!el.toolsPopover.hidden && !event.target.closest(".tools-menu")) {
    closeTools();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeTools();
    closeConfigModal();
  }
});

for (const tab of document.querySelectorAll(".tab-button")) {
  tab.addEventListener("click", () => {
    const selected = tab.dataset.tab;
    for (const other of document.querySelectorAll(".tab-button")) {
      other.classList.toggle("active", other === tab);
    }
    el.artifactList.classList.toggle("hidden", selected !== "artifacts");
    el.resultList.classList.toggle("hidden", selected !== "results");
  });
}

applyLayoutPrefs();
loadHealth();
loadConfig({ silent: true });
loadCommands().catch((error) => showToast(error.message, "error"));
loadSessions().catch((error) => showToast(error.message, "error"));
