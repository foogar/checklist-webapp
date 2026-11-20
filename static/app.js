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
  activityOpen: false,
  editing: false,
  // Preserved across re-renders so toggling a task cannot eat a half-typed
  // item in the add field.
  draft: "",
  clientId: "",
  inflight: 0,
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

function setOffline(value) {
  state.offline = value;
  dom.offlineBanner.hidden = !value;
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
    toast(error.message);
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
    Object.assign(task, previous);
    toast(error.message);
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
    toast("Item deleted", { actionLabel: "Undo", onAction: () => restoreTask(taskId) });
  } catch (error) {
    state.tasks.splice(index, 0, task);
    syncCounts();
    render();
    toast(error.message);
  }
}

async function restoreTask(taskId) {
  try {
    const data = await apiJson(`/api/tasks/${taskId}/restore`, { method: "POST" });
    state.tasks.push(data.task);
    sortTasks();
    syncCounts();
    afterMutation(data);
    render();
    toast("Item restored");
  } catch (error) {
    toast(error.message);
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
    toast(error.message);
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

  window.addEventListener("online", async () => {
    setOffline(false);
    if (!state.user) return;
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
  }

  registerServiceWorker();
}

boot();
