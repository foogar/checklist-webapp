/* Collaborative Checklists — frontend.
 *
 * Vanilla ES2020, no framework and no build step. Everything user-supplied is
 * written with textContent; the page never assigns a string to innerHTML,
 * which is what lets the Content-Security-Policy stay strict.
 */

"use strict";

const THEME_KEY = "checklist-theme";

const state = {
  user: null,
  csrf: "",
  offline: !navigator.onLine,
};

const dom = {
  main: document.getElementById("main"),
  authSlot: document.getElementById("auth-slot"),
  themeToggle: document.getElementById("theme-toggle"),
  brand: document.getElementById("brand"),
  offlineBanner: document.getElementById("offline-banner"),
  toasts: document.getElementById("toasts"),
};

const templates = {
  signedOut: document.getElementById("tpl-signed-out"),
  signedIn: document.getElementById("tpl-signed-in"),
  welcome: document.getElementById("tpl-welcome"),
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

function setOffline(value) {
  state.offline = value;
  dom.offlineBanner.hidden = !value;
}

async function api(path, options = {}) {
  const { method = "GET", body } = options;
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;

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

function renderMain() {
  dom.main.replaceChildren();
  if (!state.user) {
    dom.main.append(clone(templates.welcome));
    return;
  }

  // Milestone 1 shows only the shell; the list view is the next step. Keeping
  // the placeholder honest beats pretending an empty screen is an empty
  // account.
  const card = document.createElement("section");
  card.className = "card placeholder";
  const heading = document.createElement("h1");
  heading.textContent = "Loading your checklists…";
  const note = document.createElement("p");
  note.textContent = `Signed in as ${state.user.email}.`;
  card.append(heading, note);
  dom.main.append(card);
}

function render() {
  renderAuth();
  renderMain();
}

// -------------------------------------------------------------------- toasts

function toast(message, options = {}) {
  const { actionLabel, onAction, duration = 5000 } = options;
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

// ---------------------------------------------------------------------- boot

async function loadMe() {
  const response = await api("/api/me");
  if (!response || !response.ok) return;
  const data = await response.json();
  state.user = data.user || null;
  state.csrf = data.csrf || "";
}

function wireEvents() {
  dom.themeToggle.addEventListener("click", toggleTheme);

  dom.brand.addEventListener("click", () => {
    dom.main.focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  window.addEventListener("online", () => {
    setOffline(false);
    loadMe().then(render);
  });
  window.addEventListener("offline", () => setOffline(true));
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

async function boot() {
  applyStoredTheme();
  // Enable the colour transition only once the stored theme is in place.
  requestAnimationFrame(() => document.body.classList.add("theme-ready"));
  wireEvents();
  setOffline(!navigator.onLine);
  render();
  await loadMe();
  render();
  registerServiceWorker();
}

boot();
