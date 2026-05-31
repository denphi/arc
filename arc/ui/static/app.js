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
  configOpener: null,
  activeJobId: null,
  eventSource: null,
  servicesDismissed: false,
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
  streamToggle: document.getElementById("streamToggle"),
  activityList: document.getElementById("activityList"),
  cancelJob: document.getElementById("cancelJob"),
  authorName: document.getElementById("authorName"),
  authorWorkflow: document.getElementById("authorWorkflow"),
  authorSchema: document.getElementById("authorSchema"),
  validateWorkflow: document.getElementById("validateWorkflow"),
  createArtifact: document.getElementById("createArtifact"),
  authorStatus: document.getElementById("authorStatus"),
  servicesBanner: document.getElementById("servicesBanner"),
  startServices: document.getElementById("startServices"),
  dismissServices: document.getElementById("dismissServices"),
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
    const error = new Error(detail);
    error.status = response.status;
    throw error;
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

// Safety net: setBusy(true) disables every control, and a handler can throw
// *after* a successful fetch but *before* its finally{setBusy(false)} (e.g. a
// render error). Without this, the whole UI would stay frozen with no way out
// but a reload. Any uncaught error / rejection force-clears the busy lock.
function recoverFromUncaught(error) {
  if (state.busy) {
    setBusy(false);
    showToast(error && error.message ? error.message : "Unexpected error", "error");
  }
}
window.addEventListener("error", (event) => recoverFromUncaught(event.error || event));
window.addEventListener("unhandledrejection", (event) => recoverFromUncaught(event.reason));

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
  // Remember the control that opened the modal so focus returns there on
  // close (a11y: don't strand keyboard/SR focus on a hidden element).
  state.configOpener = document.activeElement;
  el.configModal.hidden = false;
  el.configModal.classList.add("show");
  loadConfig().catch((error) => showToast(error.message, "error"));
  window.setTimeout(() => el.providerInput.focus(), 0);
}

function closeConfigModal() {
  const wasOpen = !el.configModal.hidden;
  el.configModal.classList.remove("show");
  el.configModal.hidden = true;
  if (wasOpen && state.configOpener && typeof state.configOpener.focus === "function") {
    state.configOpener.focus();
  }
  state.configOpener = null;
}

// Focus trap: while the config modal is open, keep Tab focus inside it.
function trapConfigFocus(event) {
  if (el.configModal.hidden || event.key !== "Tab") {
    return;
  }
  const focusable = el.configModal.querySelectorAll(
    'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
  );
  const enabled = Array.from(focusable).filter((node) => !node.disabled && node.offsetParent !== null);
  if (!enabled.length) {
    return;
  }
  const first = enabled[0];
  const last = enabled[enabled.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
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
    // A token may have just been supplied to unlock a locked server — (re)load
    // the data the bootstrap probe skipped on a 401. Best-effort: a still-bad
    // token surfaces its own toast.
    loadSessions().catch((error) => showToast(error.message, "error"));
    loadCommands().catch(() => {});
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function asJson(text, fallback, fieldName = "Value") {
  const value = text.trim();
  if (!value) {
    return fallback;
  }
  try {
    return JSON.parse(value);
  } catch (error) {
    throw new Error(`${fieldName} must be valid JSON: ${error.message}`);
  }
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

// Example research goals offered on an empty session. Clicking one fills the
// composer (and focuses it) so the user can edit before sending — the
// goal-first entry point. Kept domain-light so they read as starting points.
const EXAMPLE_GOALS = [
  "Maximize the band gap of a thin-film material by varying thickness",
  "Find input parameters that drive the output toward a target value",
  "Explore how a simulation's output changes across its input range",
];

function renderEmptyState() {
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
    <h2>What do you want to research?</h2>
    <p>Describe a goal and ARC will plan, run, and iterate toward it. You can
       refine the goal or adjust the target as it goes.</p>
  `;
  const examples = document.createElement("div");
  examples.className = "example-goals";
  const label = document.createElement("span");
  label.className = "example-label";
  label.textContent = "Try one to start:";
  examples.appendChild(label);
  for (const goal of EXAMPLE_GOALS) {
    const chip = document.createElement("button");
    chip.className = "example-goal";
    chip.type = "button";
    chip.textContent = goal;
    chip.addEventListener("click", () => {
      el.messageInput.value = goal;
      el.messageInput.focus();
      // Trigger the composer's auto-grow so the full goal is visible.
      el.messageInput.dispatchEvent(new Event("input"));
    });
    examples.appendChild(chip);
  }
  empty.appendChild(examples);
  el.threadList.appendChild(empty);
}

function renderThread(messages) {
  el.threadList.textContent = "";
  if (!messages.length) {
    renderEmptyState();
    return;
  }
  // Only the most recent message's suggestions are actionable — clear stale
  // chips from earlier turns so the user isn't tempted to act on old advice.
  const lastIndex = messages.length - 1;
  for (let i = 0; i < messages.length; i += 1) {
    const message = messages[i];
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
    if (i === lastIndex && Array.isArray(message.suggestions) && message.suggestions.length) {
      appendSuggestions(article, message.suggestions);
    }
    el.threadList.appendChild(article);
  }
  el.threadList.scrollTop = el.threadList.scrollHeight;
}

// Render the loop's next-step suggestion chips under a message. Clicking a
// chip runs its slash command; a chip with prompt_steps first asks how many
// iterations (the "steps when the chat asks for it" flow).
function appendSuggestions(article, suggestions) {
  const wrap = document.createElement("div");
  wrap.className = "suggestion-row";
  const hint = document.createElement("span");
  hint.className = "suggestion-hint";
  hint.textContent = "Next:";
  wrap.appendChild(hint);
  for (const suggestion of suggestions) {
    const chip = document.createElement("button");
    chip.className = "suggestion-chip";
    chip.type = "button";
    const note = suggestion.note ? ` (${suggestion.note})` : "";
    chip.textContent = `${suggestion.label}${note}`;
    chip.addEventListener("click", () => runSuggestion(suggestion));
    wrap.appendChild(chip);
  }
  article.appendChild(wrap);
}

async function runSuggestion(suggestion) {
  let command = suggestion.command || "";
  if (suggestion.prompt_steps) {
    const raw = window.prompt("How many iterations?", "3");
    if (raw === null) {
      return;   // cancelled
    }
    const steps = Math.max(1, Math.min(20, Number(raw) || 1));
    command = `${command} ${steps}`.trim();
  }
  if (!command) {
    return;
  }
  runCommandText(command, "running").catch((error) => showToast(error.message, "error"));
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
      loadArtifactFiles(artifact);
    });
    el.artifactList.appendChild(button);
  }
}

// Fetch the artifact's file list and render a viewer in the detail pane:
// a row of file chips that, when clicked, fetch + show that file's contents.
async function loadArtifactFiles(artifact) {
  if (!state.activeSessionId) {
    return;
  }
  let files = [];
  try {
    const detail = await api(
      `/api/sessions/${encodeURIComponent(state.activeSessionId)}` +
      `/artifacts/${encodeURIComponent(artifact.artifact_id)}` +
      `?version=${encodeURIComponent(artifact.version)}`,
    );
    files = detail.files || [];
  } catch (error) {
    showToast(error.message, "error");
    return;
  }
  renderArtifactFiles(artifact, files);
}

function renderArtifactFiles(artifact, files) {
  el.detailView.textContent = "";
  const wrap = document.createElement("div");
  wrap.className = "file-viewer";
  if (!files.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No viewable files";
    wrap.appendChild(empty);
  } else {
    const chips = document.createElement("div");
    chips.className = "file-chips";
    const pane = document.createElement("pre");
    pane.className = "file-contents";
    pane.textContent = "Select a file";
    for (const file of files) {
      const chip = document.createElement("button");
      chip.className = "file-chip";
      chip.type = "button";
      chip.innerHTML = `<span>${escapeHtml(file.path)}</span><strong>${formatBytes(file.size)}</strong>`;
      chip.addEventListener("click", async () => {
        for (const other of chips.querySelectorAll(".file-chip")) {
          other.classList.remove("active");
        }
        chip.classList.add("active");
        pane.textContent = "Loading…";
        try {
          const result = await api(
            `/api/sessions/${encodeURIComponent(state.activeSessionId)}` +
            `/artifacts/${encodeURIComponent(artifact.artifact_id)}` +
            `/files/${file.path.split("/").map(encodeURIComponent).join("/")}` +
            `?version=${encodeURIComponent(artifact.version)}`,
          );
          pane.textContent = result.content || "(empty)";
        } catch (error) {
          pane.textContent = `Error: ${error.message}`;
        }
      });
      chips.appendChild(chip);
    }
    wrap.appendChild(chips);
    wrap.appendChild(pane);
  }
  el.detailView.appendChild(wrap);
}

function formatBytes(size) {
  if (!Number.isFinite(size)) {
    return "";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  return `${(size / 1024).toFixed(1)} KB`;
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
      loadResultDetail(result.run_id);
    });
    el.resultList.appendChild(button);
  }
}

// Fetch a result's full record + the review mapped to its run, and show both
// in the detail pane (review first, then the raw execution JSON).
async function loadResultDetail(runId) {
  if (!state.activeSessionId) {
    return;
  }
  try {
    const detail = await api(
      `/api/sessions/${encodeURIComponent(state.activeSessionId)}` +
      `/results/${encodeURIComponent(runId)}`,
    );
    const review = detail.review || {};
    if (Object.keys(review).length) {
      const merged = { review, result: detail.result };
      el.detailView.textContent = JSON.stringify(merged, null, 2);
    } else {
      renderJson(detail.result);
    }
  } catch (error) {
    showToast(error.message, "error");
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
    target: asJson(el.targetInput.value, {}, "Target"),
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
  // Research runs go through the background job + SSE timeline by DEFAULT: a
  // real run (with an LLM provider) takes minutes, and a synchronous request
  // would leave the browser on a dead spinner with no feedback or way to
  // cancel. Untick "Run as a background job" to force the old blocking path.
  if (el.streamToggle.checked) {
    await startResearchJob(content);
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

// ── Background research jobs + SSE timeline ──────────────────────────────

async function startResearchJob(content) {
  try {
    const started = await api("/api/jobs/research", {
      method: "POST",
      body: JSON.stringify({ ...runPayload(), content }),
    });
    state.activeSessionId = started.session_id;
    state.activeJobId = started.job_id;
    el.messageInput.value = "";
    clearActivity();
    appendActivity({ kind: "status", text: `Job ${started.job_id} started`, ts: Date.now() / 1000 });
    el.cancelJob.hidden = false;
    el.busyState.textContent = "job running";
    el.busyState.classList.add("busy");
    streamJobEvents(started.job_id);
  } catch (error) {
    showToast(error.message, "error");
  }
}

function streamJobEvents(jobId) {
  // EventSource can't set headers, so a locked server needs the token in the
  // query string (the SSE route accepts ?token=, see server.py).
  const token = (state.authToken || el.apiTokenInput.value).trim();
  const url = `/api/jobs/${encodeURIComponent(jobId)}/events` +
    (token ? `?token=${encodeURIComponent(token)}` : "");
  if (state.eventSource) {
    state.eventSource.close();
  }
  const source = new EventSource(url);
  state.eventSource = source;
  source.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    if (data.kind === "done") {
      finishJob(jobId);
      return;
    }
    appendActivity(data);
  };
  source.onerror = () => {
    // The stream closes on completion; treat an error after we've seen 'done'
    // as benign. Otherwise surface it once and stop.
    if (state.activeJobId === jobId) {
      finishJob(jobId);
    }
  };
}

async function finishJob(jobId) {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  state.activeJobId = null;
  el.cancelJob.hidden = true;
  el.busyState.textContent = "idle";
  el.busyState.classList.remove("busy");
  // Reload the session so the thread + artifacts/results reflect the run.
  try {
    await loadSessions();
    if (state.activeSessionId) {
      await selectSession(state.activeSessionId);
    }
  } catch (error) {
    showToast(error.message, "error");
  }
}

function appendActivity(event) {
  const row = document.createElement("div");
  row.className = `activity-row activity-${escapeHtml(event.kind || "status")}`;
  row.innerHTML = `
    <span class="activity-kind">${escapeHtml(event.kind || "status")}</span>
    <span class="activity-text">${escapeHtml(event.text || "")}</span>
    <span class="activity-time">${formatTime(event.ts)}</span>
  `;
  el.activityList.appendChild(row);
  el.activityList.scrollTop = el.activityList.scrollHeight;
}

function clearActivity() {
  el.activityList.textContent = "";
}

async function cancelActiveJob() {
  if (!state.activeJobId) {
    return;
  }
  const jobId = state.activeJobId;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    appendActivity({ kind: "status", text: "Cancellation requested", ts: Date.now() / 1000 });
  } catch (error) {
    showToast(error.message, "error");
  }
}

// ── Artifact authoring ───────────────────────────────────────────────────

function setAuthorStatus(message, kind = "info") {
  el.authorStatus.textContent = message;
  el.authorStatus.classList.toggle("error", kind === "error");
  el.authorStatus.classList.toggle("ok", kind === "ok");
}

async function validateWorkflowSource() {
  const source = el.authorWorkflow.value.trim();
  if (!source) {
    setAuthorStatus("Enter workflow.py source to validate.", "error");
    return false;
  }
  try {
    const result = await api("/api/workflow/validate", {
      method: "POST",
      body: JSON.stringify({ source }),
    });
    setAuthorStatus(
      result.valid ? "Workflow is valid." : `Invalid: ${result.message}`,
      result.valid ? "ok" : "error",
    );
    return result.valid;
  } catch (error) {
    setAuthorStatus(error.message, "error");
    return false;
  }
}

async function createArtifactFromDraft() {
  if (!state.activeSessionId) {
    setAuthorStatus("Select or create a session first.", "error");
    return;
  }
  const name = el.authorName.value.trim();
  if (!name) {
    setAuthorStatus("Name is required.", "error");
    return;
  }
  const files = {};
  if (el.authorWorkflow.value.trim()) {
    files["workflow.py"] = el.authorWorkflow.value;
  }
  if (el.authorSchema.value.trim()) {
    files["sim2l.yaml"] = el.authorSchema.value;
  }
  setBusy(true, "creating artifact");
  try {
    const result = await api(
      `/api/sessions/${encodeURIComponent(state.activeSessionId)}/artifacts`,
      { method: "POST", body: JSON.stringify({ name, files }) },
    );
    state.activeSession = result.session;
    await selectSession(state.activeSessionId);
    setAuthorStatus(`Created ${result.artifact.name}.`, "ok");
    showToast("Artifact created");
  } catch (error) {
    setAuthorStatus(error.message, "error");
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
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
      inputs: asJson(el.executionInputs.value, {}, "Inputs"),
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

// ── sim2l services: prompt-to-start banner (mirrors the CLI chat) ─────────

// Show the banner when sim2l is installed but no services are running — the
// same condition the CLI uses to ask "Start sim2l services now?". Dismissible
// per page load; reappears on reload until services are up.
async function checkServices() {
  if (state.servicesDismissed) {
    return;
  }
  let info;
  try {
    info = await api("/api/services");
  } catch (error) {
    return;   // services status is advisory — never block the UI on it
  }
  el.servicesBanner.hidden = !info.prompt_start;
}

async function startServices() {
  el.startServices.disabled = true;
  el.startServices.textContent = "Starting…";
  try {
    const result = await api("/api/services/start", { method: "POST" });
    const failed = (result.reports || []).filter((r) => !r.ok);
    if (failed.length) {
      showToast(`Some services failed: ${failed.map((r) => r.service).join(", ")}`, "error");
    } else {
      showToast("sim2l services started");
    }
    el.servicesBanner.hidden = true;
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    el.startServices.disabled = false;
    el.startServices.textContent = "Start services";
  }
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
el.cancelJob.addEventListener("click", cancelActiveJob);
el.startServices.addEventListener("click", startServices);
el.dismissServices.addEventListener("click", () => {
  state.servicesDismissed = true;
  el.servicesBanner.hidden = true;
});
el.validateWorkflow.addEventListener("click", () => {
  validateWorkflowSource().catch((error) => showToast(error.message, "error"));
});
el.createArtifact.addEventListener("click", () => {
  createArtifactFromDraft().catch((error) => showToast(error.message, "error"));
});
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
  trapConfigFocus(event);
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

// When ARC_API_TOKEN is set (locked mode) and no token has been entered, the
// data endpoints 401. Rather than spamming an error toast for every bootstrap
// call, detect the 401 once, prompt for a token via the config modal, and stop.
function promptForToken() {
  showToast("This ARC server requires an API token. Enter it to continue.", "error");
  openConfigModal();
  window.setTimeout(() => el.apiTokenInput.focus(), 0);
}

async function init() {
  applyLayoutPrefs();
  await loadHealth();          // open endpoint — establishes server reachability
  try {
    // /api/sessions is auth-gated: it's our probe for locked mode.
    await loadSessions();
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      promptForToken();
      return;
    }
    showToast(error.message, "error");
    return;
  }
  // Authenticated (or default-open): load the rest.
  loadConfig({ silent: true });
  loadCommands().catch((error) => showToast(error.message, "error"));
  // Mirror the CLI's startup prompt: offer to start sim2l services if
  // installed but not running.
  checkServices();
}

init();
