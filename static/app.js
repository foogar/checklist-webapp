/* Collaborative Checklists — frontend.
 *
 * Vanilla ES2020, no framework and no build step. Everything user-supplied is
 * written with textContent; the page never assigns a string to innerHTML,
 * which is what lets the Content-Security-Policy stay strict.
 *
 * State lives in one object and render() redraws from it. That is more than
 * fast enough for a household list, and it means there is exactly one place
 * that decides what the screen looks like.
 */

"use strict";

const THEME_KEY = "checklist-theme";

const state = {
  user: null,
  csrf: "",
  offline: !navigator.onLine,
  lists: [],
  activeId: null,
  tasks: [],
  version: 0,
  activity: [],
  presence: [],
  members: [],
  inviteLinks: [],
  blocks: [],
  newInviteUrl: "",
  activityOpen: false,
  editing: false,
  // Preserved across re-renders so toggling a task cannot eat a half-typed
  // item in the add field.
  draft: "",
  clientId: "",
  inflight: 0,
  queue: [],
  flushing: false,
  pollTimer: null,
  pollCount: 0,
  lastInteraction: Date.now(),
};

const dom = {
  main: document.getElementById("main"),
  authSlot: document.getElementById("auth-slot"),
  themeToggle: document.getElementById("theme-toggle"),
  brand: document.getElementById("brand"),
  offlineBanner: document.getElementById("offline-banner"),
  toasts: document.getElementById("toasts"),
  newListDialog: document.getElementById("dlg-new-list"),
  newListForm: document.getElementById("form-new-list"),
  confirmDialog: document.getElementById("dlg-confirm"),
  shareDialog: document.getElementById("dlg-share"),
  settingsDialog: document.getElementById("dlg-settings"),
  inviteForm: document.getElementById("form-invite"),
  settingsForm: document.getElementById("form-settings"),
  importFile: document.getElementById("import-file"),
};

const templates = {
  signedOut: document.getElementById("tpl-signed-out"),
  signedIn: document.getElementById("tpl-signed-in"),
  welcome: document.getElementById("tpl-welcome"),
  overview: document.getElementById("tpl-overview"),
  listCard: document.getElementById("tpl-list-card"),
  workspace: document.getElementById("tpl-workspace"),
  task: document.getElementById("tpl-task"),
};

// --------------------------------------------------------------------- theme

function prefersDark() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function currentTheme() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === "light" || explicit === "dark") return explicit;
  return prefersDark() ? "dark" : "light";
}

function applyStoredTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem(THEME_KEY);
  } catch (error) {
    // Private mode or a blocked storage partition: the system preference is
    // still a perfectly good answer.
  }
  if (stored === "light" || stored === "dark") {
    document.documentElement.dataset.theme = stored;
  }
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (error) {
    /* not fatal */
  }
}

// ----------------------------------------------------------------------- api

function makeClientId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function loadClientId() {
  try {
    let id = sessionStorage.getItem("checklist-client-id");
    if (!id) {
      id = makeClientId();
      sessionStorage.setItem("checklist-client-id", id);
    }
    return id;
  } catch (error) {
    // Storage blocked: presence still works, it just cannot be stable across
    // reloads within this tab.
    return makeClientId();
  }
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// A dropped connection surfaces as a thrown fetch; a dead origin behind a
// proxy surfaces as a well-formed 5xx. Both mean "keep this change and retry".
function isOffline(error) {
  return !error || error.status === 0 || error.status >= 500;
}

function setOffline(value) {
  state.offline = value;
  updateOfflineBanner();
}

function updateOfflineBanner() {
  const pending = state.queue.length;
  if (state.offline) {
    dom.offlineBanner.hidden = false;
    dom.offlineBanner.textContent = pending
      ? `Offline — ${pending} change${pending === 1 ? "" : "s"} will be sent when you reconnect.`
      : "Offline — checklists cannot be loaded or changed until the connection is back.";
  } else if (pending) {
    dom.offlineBanner.hidden = false;
    dom.offlineBanner.textContent = `Sending ${pending} change${pending === 1 ? "" : "s"}…`;
  } else {
    dom.offlineBanner.hidden = true;
  }
}

async function apiFetch(path, options = {}) {
  const { method = "GET", body, headers: extraHeaders } = options;
  const headers = { Accept: "application/json", ...(extraHeaders || {}) };
  if (state.clientId) headers["X-Client-Id"] = state.clientId;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;

  const mutating = method !== "GET";
  if (mutating) state.inflight += 1;
  try {
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
    // A 5xx behind a proxy usually means the origin is down, which is more
    // useful to the user as "offline" than as a raw error.
    setOffline(response.status >= 500);
    return response;
  } catch (error) {
    setOffline(true);
    return null;
  } finally {
    if (mutating) state.inflight -= 1;
  }
}

async function apiJson(path, options = {}) {
  const response = await apiFetch(path, options);
  if (!response) {
    throw new ApiError("You are offline. Reconnect and try again.", 0);
  }
  if (response.status === 304) return null;

  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (error) {
      /* a non-JSON error page */
    }
  }

  if (!response.ok) {
    if (response.status === 401) {
      stopPolling();
      state.user = null;
      state.lists = [];
      render();
    }
    throw new ApiError(
      (data && data.detail) || `Request failed (${response.status})`,
      response.status
    );
  }
  return data;
}

// --------------------------------------------------------------- offline queue

const QUEUE_KEY = "checklist-queue";

function loadQueue() {
  try {
    const parsed = JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    return [];
  }
}

function saveQueue() {
  try {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(state.queue));
  } catch (error) {
    // Storage blocked: the queue still works for this page's lifetime.
  }
}

function enqueue(method, path, body, options = {}) {
  state.queue.push({
    id: makeClientId(),
    userId: state.user ? state.user.id : null,
    listId: state.activeId,
    method,
    path,
    body: body === undefined ? null : body,
    tempId: options.tempId || null,
  });
  saveQueue();
  updateOfflineBanner();
  window.setTimeout(flushQueue, 1500);
}

function remapTempId(tempId, realId) {
  for (const op of state.queue) {
    op.path = op.path.replace(`/api/tasks/${tempId}`, `/api/tasks/${realId}`);
    if (op.body && Array.isArray(op.body.task_ids)) {
      op.body.task_ids = op.body.task_ids.map((id) =>
        id === tempId ? realId : id
      );
    }
  }
  const task = state.tasks.find((entry) => entry.id === tempId);
  if (task) task.id = realId;
  saveQueue();
}

async function flushQueue() {
  if (state.flushing || state.queue.length === 0 || !state.user) return;

  state.flushing = true;
  updateOfflineBanner();

  let touchedList = null;
  let sent = 0;

  while (state.queue.length > 0) {
    const op = state.queue[0];
    // A different account's queue is left alone rather than sent under this
    // session.
    if (op.userId !== state.user.id) break;

    const response = await apiFetch(op.path, {
      method: op.method,
      body: op.body === null ? undefined : op.body,
      headers: { "Idempotency-Key": op.id },
    });

    if (!response) break; // still offline
    if (response.status === 401) break;
    if (response.status >= 500) break;

    if (response.status === 409) {
      // The list moved on. Applying a stale order would undo someone's work.
      state.queue.shift();
      saveQueue();
      touchedList = op.listId || touchedList;
      continue;
    }

    if (response.status >= 400 && response.status !== 404) {
      // Validation or permission failure: retrying cannot succeed.
      state.queue.shift();
      saveQueue();
      toast("A saved change could not be applied.");
      continue;
    }

    if (op.tempId && response.status < 300) {
      try {
        const data = await response.json();
        if (data && data.task) remapTempId(op.tempId, data.task.id);
      } catch (error) {
        // The full state fetch below reconciles anyway.
      }
    }

    state.queue.shift();
    saveQueue();
    sent += 1;
    touchedList = op.listId || touchedList;
  }

  state.flushing = false;
  updateOfflineBanner();

  if (sent > 0 && state.queue.length === 0 && touchedList !== null) {
    try {
      await refreshLists();
      if (state.activeId !== null) await loadState();
      render();
      toast("All saved changes are in.");
    } catch (error) {
      // The banner still reflects anything left in the queue.
    }
  }
}

// -------------------------------------------------------------------- render

function clone(template) {
  return template.content.cloneNode(true);
}

function fill(root, field, value) {
  const node = root.querySelector(`[data-field="${field}"]`);
  if (!node) return;
  // textContent does not populate an <input>; its value is what gets submitted.
  if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
    node.value = value;
  } else {
    node.textContent = value;
  }
}

function currentList() {
  return state.lists.find((list) => list.id === state.activeId) || null;
}

function render() {
  renderAuth();
  if (!state.user) {
    renderWelcome();
    return;
  }
  if (state.activeId !== null) {
    renderWorkspace();
    return;
  }
  renderOverview();
}

function renderAuth() {
  dom.authSlot.replaceChildren();
  if (state.user) {
    const fragment = clone(templates.signedIn);
    fill(fragment, "name", state.user.name || state.user.email);
    fill(fragment, "csrf", state.csrf);
    dom.authSlot.append(fragment);
  } else {
    dom.authSlot.append(clone(templates.signedOut));
  }
}

function renderWelcome() {
  dom.main.replaceChildren(clone(templates.welcome));
}

function renderOverview() {
  const section = clone(templates.overview);
  const grid = section.querySelector('[data-slot="lists"]');

  for (const list of state.lists) {
    const card = clone(templates.listCard);
    fill(card, "title", list.title);
    fill(
      card,
      "meta",
      list.role === "owner" ? "Yours" : `Shared by ${list.owner_name}`
    );

    const done = list.task_count - list.pending_count;
    fill(
      card,
      "count",
      list.task_count === 0 ? "No items yet" : `${done} of ${list.task_count} done`
    );

    const badge = card.querySelector('[data-field="badge"]');
    badge.hidden = !list.is_resettable;

    card.querySelector('[data-action="open-list"]').dataset.listId = String(list.id);
    grid.append(card);
  }

  section.querySelector('[data-slot="empty"]').hidden = state.lists.length > 0;
  dom.main.replaceChildren(section);
}

function subtitleFor(list) {
  const parts = [
    list.role === "owner" ? "Your list" : `Shared by ${list.owner_name}`,
  ];
  if (list.is_resettable) parts.push("daily reset");
  return parts.join(" · ");
}

function renderWorkspace() {
  const list = currentList();
  if (!list) {
    state.activeId = null;
    renderOverview();
    return;
  }

  const fragment = clone(templates.workspace);
  const section = fragment.querySelector(".workspace");
  fill(section, "title", list.title);
  fill(section, "sub", subtitleFor(list));
  section.classList.toggle("editing", state.editing);

  const editButton = section.querySelector('[data-action="toggle-edit"]');
  editButton.textContent = state.editing ? "Done" : "Edit";
  editButton.setAttribute("aria-pressed", String(state.editing));

  section.querySelector('[data-slot="edit-hint"]').hidden = !state.editing;

  const resetButton = section.querySelector('[data-action="reset"]');
  resetButton.hidden = !(list.role === "owner" && list.is_resettable);

  const deleteButton = section.querySelector('[data-action="delete-list"]');
  deleteButton.hidden = list.role !== "owner";

  section.querySelector('[data-action="share"]').hidden = list.role !== "owner";
  section.querySelector('[data-action="settings"]').hidden = list.role !== "owner";

  const form = section.querySelector('[data-action="add-task"]');
  if (list.role === "viewer") {
    form.hidden = true;
    editButton.hidden = true;
  } else {
    form.elements.title.value = state.draft;
  }

  renderTasks(section);
  renderPresence(section);
  renderActivity(section, list);
  dom.main.replaceChildren(fragment);
}

function renderTasks(section) {
  const pending = state.tasks.filter((task) => task.status === "pending");
  const completed = state.tasks.filter((task) => task.status === "completed");

  const pendingList = section.querySelector('[data-slot="pending"]');
  for (const task of pending) pendingList.append(buildTask(task, true));
  section.querySelector('[data-slot="no-pending"]').hidden = pending.length > 0;

  const completedSection = section.querySelector('[data-slot="completed"]');
  completedSection.hidden = completed.length === 0;
  const completedList = section.querySelector('[data-slot="completed-list"]');
  for (const task of completed) completedList.append(buildTask(task, false));
}

function buildTask(task, isPending) {
  const fragment = clone(templates.task);
  const row = fragment.querySelector(".task");
  row.dataset.taskId = String(task.id);

  const check = row.querySelector(".task-check");
  check.checked = !isPending;

  fill(fragment, "title", task.title);

  if (!isPending) {
    const who = task.completed_by_name || "Someone";
    const when = relativeTime(task.completed_at);
    fill(fragment, "meta", when ? `${who} · ${when}` : who);
  }

  if (!isPending) {
    row.querySelector('[data-action="move-up"]').hidden = true;
    row.querySelector('[data-action="move-down"]').hidden = true;
  }

  return fragment;
}

function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.round(seconds / 86400)}d ago`;
  return new Date(then).toLocaleDateString();
}

function renderPresence(section) {
  const node = section.querySelector('[data-slot="presence"]');
  if (!state.user) {
    node.hidden = true;
    return;
  }
  const names = [
    ...new Set(
      (state.presence || [])
        .filter((entry) => entry.user_id !== state.user.id)
        .map((entry) => entry.name)
        .filter(Boolean)
    ),
  ];
  node.hidden = names.length === 0;
  node.textContent = names.length ? `Also here: ${names.join(", ")}` : "";
}

function activityText(entry) {
  const who = entry.user_name || "Someone";
  const subject = entry.detail ? `“${entry.detail}”` : "an item";
  switch (entry.action) {
    case "list.created":
      return `${who} created the list`;
    case "list.renamed":
      return `${who} renamed the list to “${entry.detail}”`;
    case "list.settings":
      return `${who} changed the reset setting`;
    case "list.reset":
      return `${who} reset the list`;
    case "list.imported":
      return `${who} imported “${entry.detail}”`;
    case "list.auto_reset":
      return "The list reset automatically";
    case "invite.created":
      return `${who} created an invite link`;
    case "invite.revoked":
      return `${who} revoked an invite link`;
    case "member.joined":
      return `${who} joined the list`;
    case "member.removed":
      return `${who} removed ${entry.detail || "someone"}`;
    case "member.role":
      return `${who} changed a role to ${entry.detail}`;
    case "member.unblocked":
      return `${who} allowed ${entry.detail || "someone"} back`;
    case "task.added":
      return `${who} added ${subject}`;
    case "task.completed":
      return `${who} completed ${subject}`;
    case "task.reopened":
      return `${who} reopened ${subject}`;
    case "task.deleted":
      return `${who} deleted ${subject}`;
    case "task.restored":
      return `${who} restored ${subject}`;
    case "task.renamed":
      return `${who} renamed ${subject}`;
    default:
      return `${who} made a change`;
  }
}

function renderActivity(section, list) {
  const details = section.querySelector('[data-slot="activity"]');
  const entries = state.activity || [];
  // Solo lists have no one else to report on, and no polling to keep the feed
  // current, so the panel is shown only where it means something.
  if (list.member_count <= 1 || entries.length === 0) {
    details.hidden = true;
    return;
  }

  details.hidden = false;
  details.open = state.activityOpen;
  details.addEventListener("toggle", () => {
    state.activityOpen = details.open;
  });

  const target = details.querySelector('[data-slot="activity-list"]');
  for (const entry of entries) {
    const item = document.createElement("li");
    item.className = "activity-item";

    const text = document.createElement("span");
    text.textContent = activityText(entry);

    const time = document.createElement("time");
    time.dateTime = entry.created_at || "";
    time.textContent = relativeTime(entry.created_at);

    item.append(text, time);
    target.append(item);
  }
}

// -------------------------------------------------------------------- toasts

function toast(message, options = {}) {
  const { actionLabel, onAction, duration = 6000 } = options;
  const element = document.createElement("div");
  element.className = "toast";

  const text = document.createElement("p");
  text.textContent = message;
  element.append(text);

  if (actionLabel && onAction) {
    const action = document.createElement("button");
    action.type = "button";
    action.textContent = actionLabel;
    action.addEventListener("click", () => {
      element.remove();
      onAction();
    });
    element.append(action);
  }

  dom.toasts.append(element);
  if (duration > 0) {
    window.setTimeout(() => element.remove(), duration);
  }
}

// ------------------------------------------------------------------- actions

async function refreshLists() {
  const data = await apiJson("/api/lists");
  state.lists = data.lists;
}

const POLL_ACTIVE_MS = 4000;
const POLL_IDLE_MS = 20000;
const POLL_FULL_EVERY = 5;
const IDLE_AFTER_MS = 120000;

function applyState(data, { render: shouldRender = true } = {}) {
  state.version = data.version;
  state.tasks = data.tasks;
  state.activity = data.activity || [];
  state.presence = data.presence || [];
  state.members = data.members || [];
  state.inviteLinks = data.invite_links || [];
  state.blocks = data.blocks || [];

  const index = state.lists.findIndex((entry) => entry.id === data.list.id);
  if (index === -1) state.lists.push(data.list);
  else state.lists[index] = data.list;

  if (!shouldRender) return;

  // Re-rendering must not steal focus from a checkbox or the add field.
  const active = document.activeElement;
  const taskId =
    active && active.closest && active.closest(".task")
      ? Number(active.closest(".task").dataset.taskId)
      : null;
  const addFocused = Boolean(active && active.id === "new-task");

  render();
  if (taskId !== null) focusTask(taskId);
  if (addFocused) focusAdd();
}

async function loadState() {
  const data = await apiJson(`/api/lists/${state.activeId}/state`);
  if (data) applyState(data, { render: false });
  return data;
}

function stopPolling() {
  if (state.pollTimer !== null) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

function shouldPoll() {
  const list = currentList();
  return Boolean(
    state.user &&
      state.activeId !== null &&
      list &&
      list.member_count > 1 &&
      document.visibilityState === "visible"
  );
}

function schedulePoll() {
  stopPolling();
  if (!shouldPoll()) return;
  const idle = Date.now() - state.lastInteraction > IDLE_AFTER_MS;
  state.pollTimer = window.setTimeout(poll, idle ? POLL_IDLE_MS : POLL_ACTIVE_MS);
}

function nudgePoll() {
  if (!shouldPoll()) return;
  stopPolling();
  state.pollTimer = window.setTimeout(poll, 800);
}

async function poll() {
  state.pollTimer = null;
  if (!shouldPoll()) return;
  if (state.inflight > 0) {
    schedulePoll();
    return;
  }

  // A conditional request costs one integer comparison and no body. Every
  // fifth poll drops the condition so presence and activity refresh even when
  // the list itself has not changed.
  const full = state.pollCount % POLL_FULL_EVERY === 0 || !state.version;
  state.pollCount += 1;
  const suffix = full ? "" : `?since=${state.version}`;

  try {
    const data = await apiJson(`/api/lists/${state.activeId}/state${suffix}`);
    if (data) applyState(data);
  } catch (error) {
    if (error.status === 404) {
      toast("That list is no longer available.");
      state.activeId = null;
      await showOverview({ push: false });
      return;
    }
    // Anything else is transient; the offline banner already explains it.
  }
  schedulePoll();
}

function afterMutation(data) {
  if (data && Number.isInteger(data.version)) state.version = data.version;
  // Force the next poll to fetch everything, so activity and counts catch up
  // even though this client already knows the new version.
  state.pollCount = 0;
  nudgePoll();
  if (state.queue.length > 0) window.setTimeout(flushQueue, 500);
}

function sortTasks() {
  state.tasks.sort((a, b) => a.position - b.position || a.id - b.id);
}

function syncCounts() {
  const list = currentList();
  if (!list) return;
  list.task_count = state.tasks.length;
  list.pending_count = state.tasks.filter((task) => task.status === "pending").length;
}

function routeFromHash() {
  const match = location.hash.match(/^#\/list\/(\d+)$/);
  return match ? Number(match[1]) : null;
}

async function openList(id, { push = true } = {}) {
  if (push) history.pushState(null, "", `#/list/${id}`);
  state.activeId = id;
  state.editing = false;
  state.draft = "";
  state.activity = [];
  state.presence = [];
  state.activityOpen = false;
  state.pollCount = 0;
  state.lastInteraction = Date.now();

  try {
    await loadState();
  } catch (error) {
    state.activeId = null;
    toast(error.message);
    if (error.status === 404) {
      await showOverview({ push: false });
      return;
    }
  }
  render();
  schedulePoll();
}

async function showOverview({ push = true } = {}) {
  stopPolling();
  if (push) history.pushState(null, "", location.pathname);
  state.activeId = null;
  state.editing = false;
  state.tasks = [];
  state.activity = [];
  state.presence = [];
  state.draft = "";

  if (state.user) {
    try {
      await refreshLists();
    } catch (error) {
      toast(error.message);
    }
  }
  render();
}

async function addTask(form) {
  const input = form.elements.title;
  const title = input.value.trim();
  if (!title || state.activeId === null) return;

  try {
    const data = await apiJson(`/api/lists/${state.activeId}/tasks`, {
      method: "POST",
      body: { title },
    });
    state.tasks.push(data.task);
    sortTasks();
    state.draft = "";
    syncCounts();
    afterMutation(data);
    render();
    focusAdd();
  } catch (error) {
    if (isOffline(error) && state.activeId !== null) {
      // Offline: keep the optimistic item with a negative id, and let the
      // queue replace it with the server's row when the connection returns.
      const tempId = -Date.now();
      const position =
        state.tasks.reduce((max, task) => Math.max(max, task.position || 0), 0) +
        1024;
      state.tasks.push({
        id: tempId,
        title,
        status: "pending",
        position,
        created_by: state.user ? state.user.id : null,
        created_by_name: state.user ? state.user.name : null,
        completed_at: null,
        completed_by: null,
        completed_by_name: null,
        temp: true,
      });
      state.draft = "";
      syncCounts();
      enqueue("POST", `/api/lists/${state.activeId}/tasks`, { title }, { tempId });
      render();
      focusAdd();
      toast("Saved on this device. It will be sent when you reconnect.");
    } else {
      toast(error.message);
    }
  }
}

async function toggleTask(taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return;

  const previous = { ...task };
  const next = task.status === "pending" ? "completed" : "pending";

  task.status = next;
  if (next === "completed") {
    task.completed_at = new Date().toISOString();
    task.completed_by_name = state.user ? state.user.name : null;
  } else {
    task.completed_at = null;
    task.completed_by_name = null;
  }
  syncCounts();
  render();
  focusTask(taskId);

  try {
    const data = await apiJson(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: { status: next },
    });
    Object.assign(task, data.task);
    afterMutation(data);
  } catch (error) {
    if (isOffline(error)) {
      enqueue("PATCH", `/api/tasks/${taskId}`, { status: next });
    } else {
      Object.assign(task, previous);
      toast(error.message);
    }
  }
  syncCounts();
  render();
  focusTask(taskId);
}

async function deleteTask(taskId) {
  const index = state.tasks.findIndex((item) => item.id === taskId);
  if (index === -1) return;

  const [task] = state.tasks.splice(index, 1);
  syncCounts();
  render();

  try {
    const data = await apiJson(`/api/tasks/${taskId}`, { method: "DELETE" });
    afterMutation(data);
    toast("Item deleted", { actionLabel: "Undo", onAction: () => restoreTask(task) });
  } catch (error) {
    if (isOffline(error)) {
      enqueue("DELETE", `/api/tasks/${taskId}`);
      toast("Item deleted", { actionLabel: "Undo", onAction: () => restoreTask(task) });
    } else {
      state.tasks.splice(index, 0, task);
      syncCounts();
      render();
      toast(error.message);
    }
  }
}

async function restoreTask(task) {
  const taskId = typeof task === "number" ? task : task.id;
  try {
    const data = await apiJson(`/api/tasks/${taskId}/restore`, { method: "POST" });
    state.tasks.push(data.task);
    sortTasks();
    syncCounts();
    afterMutation(data);
    render();
    toast("Item restored");
  } catch (error) {
    if (isOffline(error)) {
      if (typeof task !== "number") {
        state.tasks.push(task);
        sortTasks();
        syncCounts();
        render();
      }
      enqueue("POST", `/api/tasks/${taskId}/restore`);
      toast("Restored on this device. It will be sent when you reconnect.");
    } else {
      toast(error.message);
    }
  }
}

async function moveTask(taskId, direction) {
  const pending = state.tasks.filter((task) => task.status === "pending");
  const index = pending.findIndex((task) => task.id === taskId);
  const target = direction === "up" ? index - 1 : index + 1;
  if (index < 0 || target < 0 || target >= pending.length) return;

  [pending[index], pending[target]] = [pending[target], pending[index]];
  const completed = state.tasks.filter((task) => task.status === "completed");
  state.tasks = [...pending, ...completed];
  state.tasks.forEach((task, position) => {
    task.position = (position + 1) * 1024;
  });
  render();

  try {
    const data = await apiJson(`/api/lists/${state.activeId}/reorder`, {
      method: "POST",
      body: { task_ids: state.tasks.map((task) => task.id) },
      headers: { "If-Match": String(state.version) },
    });
    afterMutation(data);
  } catch (error) {
    if (isOffline(error)) {
      if (state.tasks.some((task) => task.id < 0)) {
        toast("The new order will be saved once the item syncs.");
      } else {
        enqueue("POST", `/api/lists/${state.activeId}/reorder`, {
          task_ids: state.tasks.map((task) => task.id),
        });
      }
    } else {
      if (error.status === 409) {
        toast("Someone changed this list — refreshed.");
      } else {
        toast(error.message);
      }
      try {
        await loadState();
      } catch (reloadError) {
        /* the toast already explains the problem */
      }
      render();
    }
  }
}

async function resetList() {
  const list = currentList();
  if (!list) return;

  const confirmed = await confirmAction({
    title: "Reset this list?",
    message: "Every item will be marked as not done. Nothing is deleted.",
    confirmLabel: "Reset",
  });
  if (!confirmed) return;

  try {
    const data = await apiJson(`/api/lists/${list.id}/reset`, { method: "POST" });
    state.tasks = data.tasks;
    syncCounts();
    afterMutation(data);
    render();
    toast("List reset");
  } catch (error) {
    if (isOffline(error)) {
      state.tasks = state.tasks.map((task) => ({
        ...task,
        status: "pending",
        completed_at: null,
        completed_by: null,
        completed_by_name: null,
      }));
      syncCounts();
      render();
      enqueue("POST", `/api/lists/${list.id}/reset`);
      toast("Reset saved on this device. It will be sent when you reconnect.");
    } else {
      toast(error.message);
    }
  }
}

async function createList(form) {
  const title = form.elements.title.value.trim();
  if (!title) return;

  try {
    const list = await apiJson("/api/lists", {
      method: "POST",
      body: { title, is_resettable: form.elements.resettable.checked },
    });
    state.lists.unshift(list);
    dom.newListDialog.close();
    await openList(list.id);
  } catch (error) {
    toast(error.message);
  }
}

async function deleteList() {
  const list = currentList();
  if (!list) return;

  const confirmed = await confirmAction({
    title: `Delete “${list.title}”?`,
    message: "The list and every item on it will be removed. This cannot be undone.",
    confirmLabel: "Delete",
  });
  if (!confirmed) return;

  try {
    await apiJson(`/api/lists/${list.id}`, { method: "DELETE" });
    state.lists = state.lists.filter((entry) => entry.id !== list.id);
    await showOverview();
    toast("List deleted");
  } catch (error) {
    toast(error.message);
  }
}

function toggleEdit() {
  state.editing = !state.editing;
  render();
  const button = document.querySelector('[data-action="toggle-edit"]');
  if (button) button.focus();
}

// ------------------------------------------------------------------ sharing

function formatDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (!Number.isFinite(date.getTime())) return "";
  return date.toLocaleDateString();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    return false;
  }
}

function buildMember(member) {
  const row = document.createElement("li");
  row.className = "member";

  const name = document.createElement("span");
  name.className = "member-name";
  name.textContent = member.name || member.email || "Someone";
  if (member.email) {
    const email = document.createElement("span");
    email.className = "muted";
    email.textContent = member.email;
    name.append(email);
  }
  row.append(name);

  if (member.role === "owner") {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = "Owner";
    row.append(badge);
    return row;
  }

  const select = document.createElement("select");
  select.dataset.action = "member-role";
  select.dataset.userId = String(member.user_id);
  select.setAttribute("aria-label", `Role for ${member.name || member.email}`);
  for (const [value, label] of [
    ["editor", "Can edit"],
    ["viewer", "Can view"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = member.role === value;
    select.append(option);
  }
  row.append(select);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "link-button danger";
  remove.dataset.action = "remove-member";
  remove.dataset.userId = String(member.user_id);
  remove.textContent = "Remove";
  row.append(remove);

  return row;
}

function buildBlocked(person) {
  const row = document.createElement("li");
  row.className = "member";

  const name = document.createElement("span");
  name.className = "member-name";
  name.textContent = person.name || person.email || "Someone";
  if (person.email) {
    const email = document.createElement("span");
    email.className = "muted";
    email.textContent = person.email;
    name.append(email);
  }
  row.append(name);

  const allow = document.createElement("button");
  allow.type = "button";
  allow.className = "link-button";
  allow.dataset.action = "unblock-member";
  allow.dataset.userId = String(person.user_id);
  allow.textContent = "Allow back";
  row.append(allow);

  return row;
}

function buildLink(link) {
  const row = document.createElement("li");
  row.className = "link-row";

  const label = document.createElement("span");
  const role = link.role === "viewer" ? "Can view" : "Can edit";
  let stateText;
  if (link.active) {
    stateText = `${link.uses}/${link.max_uses} used · expires ${formatDate(link.expires_at)}`;
  } else if (link.revoked_at) {
    stateText = "Revoked";
  } else if (link.uses >= link.max_uses) {
    stateText = "Used up";
  } else {
    stateText = "Expired";
  }
  label.textContent = `${role} · ${stateText}`;
  row.append(label);

  if (link.active) {
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "link-button danger";
    revoke.dataset.action = "revoke-link";
    revoke.dataset.linkId = String(link.id);
    revoke.textContent = "Revoke";
    row.append(revoke);
  }

  return row;
}

function renderShare() {
  const list = currentList();
  if (!list) return;

  dom.shareDialog.querySelector('[data-field="title"]').textContent = list.title;

  const members = dom.shareDialog.querySelector('[data-slot="members"]');
  members.replaceChildren();
  for (const member of state.members) members.append(buildMember(member));

  const blockedSection = dom.shareDialog.querySelector('[data-slot="blocked-section"]');
  const blockedList = dom.shareDialog.querySelector('[data-slot="blocked"]');
  blockedList.replaceChildren();
  blockedSection.hidden = state.blocks.length === 0;
  for (const person of state.blocks) blockedList.append(buildBlocked(person));

  const newLink = dom.shareDialog.querySelector('[data-slot="new-link"]');
  newLink.hidden = !state.newInviteUrl;
  if (state.newInviteUrl) {
    newLink.querySelector('[data-field="url"]').value = state.newInviteUrl;
  }

  const links = dom.shareDialog.querySelector('[data-slot="links"]');
  links.replaceChildren();
  for (const link of state.inviteLinks) links.append(buildLink(link));
}

async function refreshShareState() {
  if (state.activeId === null) return;
  try {
    const data = await apiJson(`/api/lists/${state.activeId}/state`);
    if (!data) return;
    applyState(data, { render: false });
    render();
    renderShare();
  } catch (error) {
    toast(error.message);
  }
}

async function openShareDialog() {
  const list = currentList();
  if (!list || list.role !== "owner") return;
  state.newInviteUrl = "";
  renderShare();
  dom.shareDialog.showModal();
  await refreshShareState();
}

async function createInviteLink(form) {
  const list = currentList();
  if (!list) return;
  try {
    const link = await apiJson(`/api/lists/${list.id}/invite-links`, {
      method: "POST",
      body: {
        role: form.elements.role.value,
        expires_in_days: Number(form.elements.expires_in_days.value),
        max_uses: Number(form.elements.max_uses.value),
      },
    });
    state.newInviteUrl = link.url;
    state.inviteLinks.unshift(link);
    renderShare();
    const copied = await copyText(link.url);
    toast(copied ? "Invite link created and copied" : "Invite link created");
  } catch (error) {
    toast(error.message);
  }
}

async function copyInviteLink() {
  const input = dom.shareDialog.querySelector('[data-field="url"]');
  if (!input) return;
  const copied = await copyText(input.value);
  if (copied) {
    toast("Link copied");
  } else {
    input.select();
    toast("Press Ctrl+C to copy the link");
  }
}

// ----------------------------------------------------------------- settings

function updateResetTimeVisibility() {
  const enabled = dom.settingsForm.elements.auto_reset.checked;
  dom.settingsForm.querySelector('[data-slot="reset-time"]').hidden = !enabled;
}

function openSettings() {
  const list = currentList();
  if (!list || list.role !== "owner") return;

  const form = dom.settingsForm;
  form.reset();
  form.elements.title.value = list.title;
  form.elements.is_resettable.checked = list.is_resettable;

  const scheduled = list.reset_hour !== null && list.reset_hour !== undefined;
  form.elements.auto_reset.checked = scheduled;
  form.elements.reset_time.value = scheduled
    ? `${String(list.reset_hour).padStart(2, "0")}:00`
    : "04:00";
  updateResetTimeVisibility();

  form.querySelector('[data-slot="export"]').href = `/api/lists/${list.id}/export`;
  dom.settingsDialog.showModal();
}

async function saveSettings(form) {
  const list = currentList();
  if (!list) return;

  const auto = form.elements.auto_reset.checked;
  const time = form.elements.reset_time.value || "04:00";
  const hour = Number(time.split(":")[0]);

  try {
    const updated = await apiJson(`/api/lists/${list.id}`, {
      method: "PATCH",
      body: {
        title: form.elements.title.value.trim(),
        is_resettable: form.elements.is_resettable.checked,
        reset_hour: auto ? hour : null,
        reset_tz: auto
          ? Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
          : "UTC",
      },
    });
    const index = state.lists.findIndex((entry) => entry.id === updated.id);
    if (index === -1) state.lists.push(updated);
    else state.lists[index] = updated;
    dom.settingsDialog.close();
    render();
    toast("Settings saved");
  } catch (error) {
    toast(error.message);
  }
}

// ------------------------------------------------------------------- import

function openImportPicker() {
  dom.importFile.value = "";
  dom.importFile.click();
}

async function importList(file) {
  if (!file) return;

  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (error) {
    toast("That file is not valid JSON.");
    return;
  }

  try {
    const result = await apiJson("/api/import", { method: "POST", body: payload });
    await refreshLists();
    await openList(result.list.id);
    toast(`Imported ${result.imported} items`);
  } catch (error) {
    toast(error.message);
  }
}

// ------------------------------------------------------------------- dialogs

let confirmResolve = null;

function confirmAction({ title, message, confirmLabel = "Delete" }) {
  if (dom.confirmDialog.open) return Promise.resolve(false);
  return new Promise((resolve) => {
    const dialog = dom.confirmDialog;
    dialog.querySelector('[data-field="title"]').textContent = title;
    dialog.querySelector('[data-field="message"]').textContent = message;
    dialog.querySelector('[data-action="confirm"]').textContent = confirmLabel;
    dialog.returnValue = "";
    confirmResolve = resolve;
    dialog.showModal();
  });
}

function openNewListDialog() {
  if (dom.newListDialog.open) return;
  dom.newListForm.reset();
  dom.newListDialog.showModal();
  dom.newListForm.elements.title.focus();
}

// -------------------------------------------------------------------- events

function taskIdOf(element) {
  const row = element.closest(".task");
  return row ? Number(row.dataset.taskId) : null;
}

function focusTask(taskId) {
  const row = document.querySelector(`.task[data-task-id="${taskId}"] .task-check`);
  if (row) row.focus({ preventScroll: true });
}

function focusAdd() {
  const input = document.getElementById("new-task");
  if (input) input.focus();
}

function wireEvents() {
  dom.themeToggle.addEventListener("click", toggleTheme);
  dom.brand.addEventListener("click", () => showOverview());

  dom.newListForm.addEventListener("submit", (event) => {
    event.preventDefault();
    createList(dom.newListForm);
  });

  dom.confirmDialog
    .querySelector('[data-action="confirm"]')
    .addEventListener("click", () => dom.confirmDialog.close("confirm"));

  dom.confirmDialog.addEventListener("close", () => {
    if (!confirmResolve) return;
    const resolve = confirmResolve;
    confirmResolve = null;
    resolve(dom.confirmDialog.returnValue === "confirm");
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest('[data-action="close-dialog"]');
    if (!button) return;
    const dialog = button.closest("dialog");
    if (dialog) dialog.close("cancel");
  });

  dom.main.addEventListener("click", async (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;

    switch (target.dataset.action) {
      case "new-list":
        openNewListDialog();
        break;
      case "open-list":
        await openList(Number(target.dataset.listId));
        break;
      case "toggle-edit":
        toggleEdit();
        break;
      case "share":
        await openShareDialog();
        break;
      case "settings":
        openSettings();
        break;
      case "import":
        openImportPicker();
        break;
      case "reset":
        await resetList();
        break;
      case "delete-list":
        await deleteList();
        break;
      case "move-up":
        await moveTask(taskIdOf(target), "up");
        break;
      case "move-down":
        await moveTask(taskIdOf(target), "down");
        break;
      case "delete-task":
        await deleteTask(taskIdOf(target));
        break;
      default:
        break;
    }
  });

  dom.main.addEventListener("change", (event) => {
    if (!event.target.classList.contains("task-check")) return;
    const taskId = taskIdOf(event.target);
    if (taskId !== null) toggleTask(taskId);
  });

  dom.main.addEventListener("submit", (event) => {
    if (!event.target.matches('[data-action="add-task"]')) return;
    event.preventDefault();
    addTask(event.target);
  });

  dom.main.addEventListener("input", (event) => {
    if (event.target.id === "new-task") state.draft = event.target.value;
  });

  // -- sharing dialog --------------------------------------------------

  dom.shareDialog.addEventListener("click", async (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;

    switch (target.dataset.action) {
      case "copy-link":
        await copyInviteLink();
        break;
      case "remove-member": {
        const userId = Number(target.dataset.userId);
        const member = state.members.find((entry) => entry.user_id === userId);
        const confirmed = await confirmAction({
          title: `Remove ${member ? member.name || member.email : "this person"}?`,
          message:
            "They lose access immediately and cannot rejoin with a link until you allow them back.",
          confirmLabel: "Remove",
        });
        if (!confirmed) return;
        try {
          await apiJson(`/api/lists/${state.activeId}/members/${userId}`, {
            method: "DELETE",
          });
          await refreshShareState();
          toast("Access removed");
        } catch (error) {
          toast(error.message);
        }
        break;
      }
      case "unblock-member": {
        const userId = Number(target.dataset.userId);
        try {
          await apiJson(`/api/lists/${state.activeId}/blocks/${userId}`, {
            method: "DELETE",
          });
          await refreshShareState();
          toast("They can rejoin with a valid link");
        } catch (error) {
          toast(error.message);
        }
        break;
      }
      case "revoke-link": {
        const linkId = Number(target.dataset.linkId);
        const confirmed = await confirmAction({
          title: "Revoke this link?",
          message: "Anyone who has not joined yet will not be able to use it.",
          confirmLabel: "Revoke",
        });
        if (!confirmed) return;
        try {
          await apiJson(
            `/api/lists/${state.activeId}/invite-links/${linkId}`,
            { method: "DELETE" }
          );
          state.newInviteUrl = "";
          await refreshShareState();
          toast("Link revoked");
        } catch (error) {
          toast(error.message);
        }
        break;
      }
      default:
        break;
    }
  });

  dom.shareDialog.addEventListener("change", async (event) => {
    const select = event.target.closest('[data-action="member-role"]');
    if (!select) return;
    try {
      await apiJson(
        `/api/lists/${state.activeId}/members/${select.dataset.userId}`,
        { method: "PATCH", body: { role: select.value } }
      );
      await refreshShareState();
      toast("Role updated");
    } catch (error) {
      toast(error.message);
      renderShare();
    }
  });

  dom.shareDialog.addEventListener("close", () => {
    state.newInviteUrl = "";
  });

  dom.inviteForm.addEventListener("submit", (event) => {
    event.preventDefault();
    createInviteLink(dom.inviteForm);
  });

  // -- settings dialog -------------------------------------------------

  dom.settingsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveSettings(dom.settingsForm);
  });

  dom.settingsForm.elements.auto_reset.addEventListener(
    "change",
    updateResetTimeVisibility
  );

  dom.importFile.addEventListener("change", () => {
    importList(dom.importFile.files[0]);
  });

  window.addEventListener("online", async () => {
    setOffline(false);
    if (!state.user) return;
    await flushQueue();
    try {
      if (state.activeId !== null) await loadState();
      else await refreshLists();
      render();
      schedulePoll();
    } catch (error) {
      toast(error.message);
    }
  });
  window.addEventListener("offline", () => setOffline(true));

  // A quiet retry: the browser can still think it is online while the origin
  // is down, and the `online` event never fires in that case.
  window.setInterval(() => {
    if (state.queue.length > 0 && !state.flushing) flushQueue();
  }, 15000);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      state.lastInteraction = Date.now();
      schedulePoll();
    } else {
      stopPolling();
    }
  });

  // Any interaction resets the idle backoff, so an actively used list polls
  // quickly while one left open in the background slows down.
  dom.main.addEventListener("pointerdown", () => {
    state.lastInteraction = Date.now();
  });
  dom.main.addEventListener("keydown", () => {
    state.lastInteraction = Date.now();
  });

  window.addEventListener("popstate", () => {
    const wanted = routeFromHash();
    if (wanted !== null) openList(wanted, { push: false });
    else showOverview({ push: false });
  });
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;

  const register = () => {
    // require-trusted-types-for treats serviceWorker.register as a script-URL
    // sink. The policy is deliberately a pass-through: the URL is a constant
    // in this file, and keeping the directive means any future dynamic script
    // URL has to make the same decision explicitly.
    let url = "/sw.js";
    if (window.trustedTypes) {
      const policy = window.trustedTypes.createPolicy("checklist", {
        createScriptURL: (value) => value,
      });
      url = policy.createScriptURL(url);
    }
    navigator.serviceWorker.register(url).catch((error) => {
      console.warn("Service worker registration failed:", error);
    });
  };

  // boot() awaits /api/me, which can easily outlast the load event. Waiting
  // for an event that has already fired would silently never register.
  if (document.readyState === "complete") register();
  else window.addEventListener("load", register, { once: true });
}

// ---------------------------------------------------------------------- boot

async function loadMe() {
  const data = await apiJson("/api/me");
  state.user = data.user || null;
  state.csrf = data.csrf || "";
}

async function boot() {
  state.clientId = loadClientId();
  state.queue = loadQueue();
  applyStoredTheme();
  // Enable the colour transition only once the stored theme is in place.
  requestAnimationFrame(() => document.body.classList.add("theme-ready"));
  wireEvents();
  setOffline(!navigator.onLine);
  render();

  try {
    await loadMe();
  } catch (error) {
    // Offline on first load: show the welcome screen and the banner.
  }
  render();

  if (state.user) {
    try {
      await refreshLists();
    } catch (error) {
      toast(error.message);
    }
    const wanted = routeFromHash();
    if (wanted !== null) await openList(wanted, { push: false });
    else render();

    if (state.queue.length > 0) flushQueue();
  }

  registerServiceWorker();
}

boot();
