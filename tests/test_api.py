"""End-to-end tests for the shell, sessions, headers, and migrations.

Stdlib only, no pytest:

    python tests/test_api.py
    python -m unittest discover -s tests -v

The suite runs against a temporary database and never touches a real one.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent

WORKDIR = tempfile.mkdtemp(prefix="checklist-test-")
atexit.register(shutil.rmtree, WORKDIR, ignore_errors=True)

# Set before importing the app: config reads the environment once, at import.
os.environ["DATABASE_PATH"] = str(Path(WORKDIR) / "test.db")
os.environ.setdefault("SESSION_SECRET", "z" * 48)
os.environ["DEV_AUTH_ENABLED"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "0"  # integration tests stay deterministic

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

import auth  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
import presence  # noqa: E402
import ratelimit  # noqa: E402
from config import settings  # noqa: E402
from asgi_client import TestClient  # noqa: E402
from util import utcnow  # noqa: E402

TABLES = (
    "activity",
    "idempotency",
    "list_blocks",
    "invite_tokens",
    "list_shares",
    "list_invites",
    "tasks",
    "lists",
    "users",
)


@contextmanager
def override(**values):
    """Temporarily change a frozen setting for one test.

    Every module holds the same Settings instance, so patching the object
    affects all of them.
    """
    previous = {key: getattr(settings, key) for key in values}
    for key, value in values.items():
        object.__setattr__(settings, key, value)
    try:
        yield
    finally:
        for key, value in previous.items():
            object.__setattr__(settings, key, value)


def reset_database() -> None:
    database.init_db()
    presence.clear()
    with database.connection() as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")


def create_user(email: str = "owner@test.local", name: str = "Owner") -> int:
    with database.connection() as conn:
        return auth.upsert_user(conn, email, name, None)


def session_cookie(user_id: int, csrf: str = "test-csrf", ttl: int = 3600) -> str:
    now = int(time.time())
    return auth.sign_json(
        {"uid": user_id, "csrf": csrf, "iat": now, "exp": now + ttl}
    )


class ShellTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        self.client = TestClient(main.app)

    def test_health_reports_database(self):
        response = self.client.get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["database"], "ok")

    def test_shell_is_static_and_contains_no_user_data(self):
        response = self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Collaborative Checklists", response.text)
        self.assertNotIn("@test.local", response.text)
        self.assertNotIn("<script>", response.text)

    def test_shell_versions_its_assets(self):
        body = self.client.get("/").text
        self.assertNotIn("__ASSET_", body)
        self.assertRegex(body, r"/static/app\.css\?v=[0-9a-f]+")
        self.assertRegex(body, r"/static/app\.js\?v=[0-9a-f]+")

    def test_management_actions_are_labelled(self):
        body = self.client.get("/").text
        self.assertIn(">Export List</a>", body)
        self.assertIn(">Import List</button>", body)
        # Export moved out of the settings dialog, so its old label is gone.
        self.assertNotIn("Export JSON", body)

    def test_manifest_is_installable(self):
        response = self.client.get("/manifest.json")
        self.assertEqual(response.status, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("application/manifest+json")
        )
        manifest = response.json()
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        self.assertEqual(sizes, {"192x192", "512x512"})
        for icon in manifest["icons"]:
            self.assertIn("any", icon["purpose"])

    def test_service_worker_is_served_from_root(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["service-worker-allowed"], "/")
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertIn("text/javascript", response.headers["content-type"])

    def test_offline_page_is_served(self):
        response = self.client.get("/offline.html")
        self.assertEqual(response.status, 200)
        self.assertIn("You are offline", response.text)

    def test_head_returns_headers_without_a_body(self):
        response = self.client.head("/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"")
        self.assertGreater(int(response.headers["content-length"]), 0)

    def test_static_path_traversal_is_refused(self):
        self.assertEqual(self.client.get("/static/../main.py").status, 404)
        self.assertEqual(self.client.get("/static/%2e%2e/main.py").status, 404)

    def test_unknown_page_is_html(self):
        response = self.client.get("/nope")
        self.assertEqual(response.status, 404)
        self.assertIn("text/html", response.headers["content-type"])

    def test_unknown_api_is_json(self):
        response = self.client.get("/api/nope")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["detail"], "Not found")

    def test_robots_disallows_indexing(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status, 200)
        self.assertIn("Disallow: /", response.text)

    def test_error_detail_is_escaped_in_html(self):
        request = main.Request(
            method="GET",
            path="/auth/callback",
            query={},
            headers={},
            cookies={},
            body=b"",
            client_host="127.0.0.1",
            scheme="http",
        )
        response = main.error_response(request, main.HTTPError(400, "<script>alert(1)</script>"))
        self.assertNotIn(b"<script>alert", response.body)
        self.assertIn(b"&lt;script&gt;", response.body)


class HeaderTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        self.client = TestClient(main.app)

    def test_security_headers_on_success_and_error(self):
        for path in ("/", "/nope", "/api/me"):
            response = self.client.get(path)
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual(response.headers["x-frame-options"], "DENY")
            self.assertEqual(
                response.headers["referrer-policy"], "strict-origin-when-cross-origin"
            )
            self.assertIn("content-security-policy", response.headers)

    def test_csp_has_no_unsafe_escape_hatches(self):
        policy = self.client.get("/").headers["content-security-policy"]
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertNotIn("nonce-", policy)
        self.assertIn("script-src 'self'", policy)

    def test_api_responses_are_not_cacheable(self):
        response = self.client.get("/api/me")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_shell_must_be_revalidated(self):
        self.assertEqual(self.client.get("/").headers["cache-control"], "no-cache")

    def test_hsts_only_when_cookies_are_secure(self):
        self.assertNotIn("strict-transport-security", self.client.get("/").headers)
        with override(session_cookie_secure=True):
            self.assertIn("strict-transport-security", self.client.get("/").headers)


class SessionTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        self.client = TestClient(main.app)

    def test_anonymous_me(self):
        response = self.client.get("/api/me")
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.json()["user"])
        self.assertEqual(response.json()["csrf"], "")

    def test_forged_cookie_is_accepted(self):
        user_id = create_user("owner@test.local", "Owner")
        self.client.cookies[settings.cookie_name] = session_cookie(user_id)
        data = self.client.get("/api/me").json()
        self.assertEqual(data["user"]["email"], "owner@test.local")
        self.assertEqual(data["user"]["name"], "Owner")
        self.assertEqual(data["csrf"], "test-csrf")

    def test_tampered_cookie_is_ignored(self):
        user_id = create_user()
        cookie = session_cookie(user_id)
        self.client.cookies[settings.cookie_name] = cookie[:-2] + "aa"
        self.assertIsNone(self.client.get("/api/me").json()["user"])

    def test_expired_cookie_is_ignored(self):
        user_id = create_user()
        self.client.cookies[settings.cookie_name] = session_cookie(user_id, ttl=-10)
        self.assertIsNone(self.client.get("/api/me").json()["user"])

    def test_deleted_user_is_signed_out(self):
        user_id = create_user()
        self.client.cookies[settings.cookie_name] = session_cookie(user_id)
        with database.connection() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.assertIsNone(self.client.get("/api/me").json()["user"])

    def test_logout_requires_csrf(self):
        user_id = create_user()
        self.client.cookies[settings.cookie_name] = session_cookie(user_id)
        response = self.client.post("/auth/logout", data={})
        self.assertEqual(response.status, 403)

    def test_logout_clears_the_session(self):
        user_id = create_user()
        self.client.cookies[settings.cookie_name] = session_cookie(user_id, csrf="abc123")
        response = self.client.post("/auth/logout", data={"csrf_token": "abc123"})
        self.assertEqual(response.status, 303)
        self.assertNotIn(settings.cookie_name, self.client.cookies)
        self.assertIsNone(self.client.get("/api/me").json()["user"])

    def test_cross_origin_logout_is_refused(self):
        user_id = create_user()
        self.client.cookies[settings.cookie_name] = session_cookie(user_id, csrf="abc123")
        response = self.client.post(
            "/auth/logout",
            data={"csrf_token": "abc123"},
            headers={"origin": "https://evil.example"},
        )
        self.assertEqual(response.status, 403)

    def test_same_origin_logout_is_allowed(self):
        user_id = create_user()
        self.client.cookies[settings.cookie_name] = session_cookie(user_id, csrf="abc123")
        response = self.client.post(
            "/auth/logout",
            data={"csrf_token": "abc123"},
            headers={"origin": "http://testserver"},
        )
        self.assertEqual(response.status, 303)

    def test_dev_login_is_404_when_disabled(self):
        with override(dev_auth_enabled=False):
            self.assertEqual(self.client.get("/auth/dev-login").status, 404)

    def test_dev_login_creates_a_session(self):
        response = self.client.get("/auth/dev-login?email=Dev@Example.com")
        self.assertEqual(response.status, 303)
        self.assertEqual(response.location, "/")
        self.assertEqual(self.client.get("/api/me").json()["user"]["email"], "dev@example.com")


class LimitTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        self.client = TestClient(main.app)

    def test_oversized_body_is_rejected(self):
        response = self.client.post("/auth/logout", body=b"x" * (70 * 1024))
        self.assertEqual(response.status, 413)

    def test_limiter_allows_then_refuses_then_recovers(self):
        limiter = ratelimit.SlidingWindowLimiter()
        self.assertEqual(limiter.check("general", "k", 2, 60), (True, 0))
        self.assertEqual(limiter.check("general", "k", 2, 60), (True, 0))
        allowed, retry_after = limiter.check("general", "k", 2, 60)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)

        limiter.reset()
        self.assertTrue(limiter.check("general", "k", 2, 60)[0])

    def test_limiter_tiers_are_independent(self):
        limiter = ratelimit.SlidingWindowLimiter()
        self.assertTrue(limiter.check("general", "k", 1, 60)[0])
        self.assertFalse(limiter.check("general", "k", 1, 60)[0])
        self.assertTrue(limiter.check("auth", "k", 1, 60)[0])


class ListApiTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        self.uid = create_user("owner@test.local", "Owner")
        self.other_uid = create_user("other@test.local", "Other")
        self.client = TestClient(main.app)
        self.client.cookies[settings.cookie_name] = session_cookie(
            self.uid, csrf="csrf-owner"
        )
        self.other = TestClient(main.app)
        self.other.cookies[settings.cookie_name] = session_cookie(
            self.other_uid, csrf="csrf-other"
        )

    # -- helpers -------------------------------------------------------

    def post(self, path, body, client=None):
        client = client or self.client
        token = "csrf-owner" if client is self.client else "csrf-other"
        return client.post(path, json_body=body, headers={"x-csrf-token": token})

    def patch(self, path, body, client=None):
        client = client or self.client
        token = "csrf-owner" if client is self.client else "csrf-other"
        return client.patch(path, json_body=body, headers={"x-csrf-token": token})

    def delete(self, path, client=None):
        client = client or self.client
        token = "csrf-owner" if client is self.client else "csrf-other"
        return client.delete(path, headers={"x-csrf-token": token})

    def make_list(self, title="Chores", resettable=False):
        response = self.post(
            "/api/lists", {"title": title, "is_resettable": resettable}
        )
        self.assertEqual(response.status, 201)
        return response.json()

    def make_task(self, list_id, title="Milk"):
        response = self.post(f"/api/lists/{list_id}/tasks", {"title": title})
        self.assertEqual(response.status, 201)
        return response.json()["task"]

    # -- lists ---------------------------------------------------------

    def test_unauthenticated_access_is_refused(self):
        anon = TestClient(main.app)
        self.assertEqual(anon.get("/api/lists").status, 401)
        self.assertEqual(
            anon.post("/api/lists", json_body={"title": "x"}).status, 401
        )

    def test_create_and_read_a_list(self):
        created = self.make_list("Groceries", resettable=True)
        self.assertEqual(created["title"], "Groceries")
        self.assertTrue(created["is_resettable"])
        self.assertEqual(created["role"], "owner")
        self.assertEqual(created["version"], 1)
        self.assertEqual(created["task_count"], 0)

        listing = self.client.get("/api/lists").json()["lists"]
        self.assertEqual([entry["id"] for entry in listing], [created["id"]])

        single = self.client.get(f"/api/lists/{created['id']}").json()
        self.assertEqual(single["title"], "Groceries")

    def test_create_requires_csrf(self):
        response = self.client.post("/api/lists", json_body={"title": "x"})
        self.assertEqual(response.status, 403)

    def test_cross_origin_write_is_refused(self):
        response = self.client.post(
            "/api/lists",
            json_body={"title": "x"},
            headers={"x-csrf-token": "csrf-owner", "origin": "https://evil.example"},
        )
        self.assertEqual(response.status, 403)

    def test_title_validation(self):
        self.assertEqual(self.post("/api/lists", {"title": ""}).status, 422)
        self.assertEqual(self.post("/api/lists", {"title": "   "}).status, 422)
        self.assertEqual(self.post("/api/lists", {"title": "a" * 201}).status, 422)
        self.assertEqual(
            self.post("/api/lists", {"title": "x", "is_resettable": "yes"}).status,
            422,
        )

    def test_rename_requires_ownership(self):
        created = self.make_list()
        response = self.patch(f"/api/lists/{created['id']}", {"title": "Renamed"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["title"], "Renamed")
        self.assertEqual(response.json()["version"], 2)

        self.assertEqual(
            self.patch(
                f"/api/lists/{created['id']}", {"title": "Nope"}, client=self.other
            ).status,
            404,
        )

    def test_delete_removes_tasks(self):
        created = self.make_list()
        self.make_task(created["id"])
        self.assertEqual(self.delete(f"/api/lists/{created['id']}").status, 204)
        self.assertEqual(self.client.get("/api/lists").json()["lists"], [])

        with database.connection() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE list_id = ?", (created["id"],)
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_non_member_sees_nothing(self):
        created = self.make_list()
        self.assertEqual(self.other.get(f"/api/lists/{created['id']}").status, 404)
        self.assertEqual(
            self.other.get(f"/api/lists/{created['id']}/tasks").status, 404
        )
        self.assertEqual(self.other.get("/api/lists").json()["lists"], [])

    # -- tasks ---------------------------------------------------------

    def test_create_task_appends_and_bumps_version(self):
        created = self.make_list()
        first = self.make_task(created["id"], "Milk")
        second = self.make_task(created["id"], "Bread")

        self.assertEqual(first["position"], 1024)
        self.assertEqual(second["position"], 2048)
        self.assertEqual(first["created_by_name"], "Owner")

        tasks = self.client.get(f"/api/lists/{created['id']}/tasks").json()
        self.assertEqual([task["title"] for task in tasks["tasks"]], ["Milk", "Bread"])
        self.assertEqual(tasks["version"], 3)  # create + two tasks

    def test_task_title_validation(self):
        created = self.make_list()
        self.assertEqual(
            self.post(f"/api/lists/{created['id']}/tasks", {"title": ""}).status, 422
        )
        self.assertEqual(
            self.post(f"/api/lists/{created['id']}/tasks", {"title": "a" * 501}).status,
            422,
        )

    def test_completing_a_task_records_who_and_when(self):
        created = self.make_list()
        task = self.make_task(created["id"])

        response = self.patch(f"/api/tasks/{task['id']}", {"status": "completed"})
        self.assertEqual(response.status, 200)
        updated = response.json()["task"]
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["completed_by"], self.uid)
        self.assertEqual(updated["completed_by_name"], "Owner")
        self.assertIsNotNone(updated["completed_at"])

        reverted = self.patch(f"/api/tasks/{task['id']}", {"status": "pending"}).json()
        self.assertIsNone(reverted["task"]["completed_at"])
        self.assertIsNone(reverted["task"]["completed_by"])

    def test_invalid_status_is_refused(self):
        created = self.make_list()
        task = self.make_task(created["id"])
        self.assertEqual(
            self.patch(f"/api/tasks/{task['id']}", {"status": "evil"}).status, 422
        )

    def test_soft_delete_and_restore(self):
        created = self.make_list()
        task = self.make_task(created["id"])

        self.assertEqual(self.delete(f"/api/tasks/{task['id']}").status, 200)
        remaining = self.client.get(f"/api/lists/{created['id']}/tasks").json()
        self.assertEqual(remaining["tasks"], [])

        restored = self.post(f"/api/tasks/{task['id']}/restore", {})
        self.assertEqual(restored.status, 200)
        self.assertEqual(restored.json()["task"]["title"], "Milk")
        again = self.client.get(f"/api/lists/{created['id']}/tasks").json()
        self.assertEqual(len(again["tasks"]), 1)

    def test_reorder_rewrites_positions(self):
        created = self.make_list()
        first = self.make_task(created["id"], "one")
        second = self.make_task(created["id"], "two")
        third = self.make_task(created["id"], "three")

        response = self.post(
            f"/api/lists/{created['id']}/reorder",
            {"task_ids": [third["id"], first["id"], second["id"]]},
        )
        self.assertEqual(response.status, 200)

        tasks = self.client.get(f"/api/lists/{created['id']}/tasks").json()["tasks"]
        self.assertEqual([task["title"] for task in tasks], ["three", "one", "two"])
        self.assertEqual([task["position"] for task in tasks], [1024, 2048, 3072])

    def test_reorder_refuses_a_stale_set(self):
        created = self.make_list()
        first = self.make_task(created["id"], "one")
        self.make_task(created["id"], "two")

        # Omitting a task means the client's view is stale.
        response = self.post(
            f"/api/lists/{created['id']}/reorder", {"task_ids": [first["id"]]}
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(
            self.post(
                f"/api/lists/{created['id']}/reorder", {"task_ids": [first["id"], 9999]}
            ).status,
            409,
        )
        self.assertEqual(
            self.post(
                f"/api/lists/{created['id']}/reorder", {"task_ids": [first["id"], first["id"]]}
            ).status,
            422,
        )

    def test_reset_returns_everything_to_pending(self):
        created = self.make_list(resettable=True)
        task = self.make_task(created["id"])
        self.patch(f"/api/tasks/{task['id']}", {"status": "completed"})

        response = self.post(f"/api/lists/{created['id']}/reset", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["tasks"][0]["status"], "pending")
        self.assertIsNone(response.json()["tasks"][0]["completed_at"])

        self.assertEqual(
            self.post(
                f"/api/lists/{created['id']}/reset", {}, client=self.other
            ).status,
            404,
        )

    def test_idempotent_create_replays_the_first_response(self):
        created = self.make_list()
        headers = {
            "x-csrf-token": "csrf-owner",
            "idempotency-key": "queue-op-12345678",
        }
        first = self.client.post(
            f"/api/lists/{created['id']}/tasks",
            json_body={"title": "Milk"},
            headers=headers,
        )
        second = self.client.post(
            f"/api/lists/{created['id']}/tasks",
            json_body={"title": "Milk"},
            headers=headers,
        )
        self.assertEqual((first.status, second.status), (201, 201))
        self.assertEqual(first.json()["task"]["id"], second.json()["task"]["id"])
        self.assertEqual(second.headers["idempotent-replay"], "true")

        tasks = self.client.get(f"/api/lists/{created['id']}/tasks").json()["tasks"]
        self.assertEqual(len(tasks), 1)

        different = self.client.post(
            f"/api/lists/{created['id']}/tasks",
            json_body={"title": "Milk"},
            headers={
                "x-csrf-token": "csrf-owner",
                "idempotency-key": "queue-op-87654321",
            },
        )
        self.assertEqual(different.status, 201)
        self.assertNotEqual(
            different.json()["task"]["id"], first.json()["task"]["id"]
        )

    def test_idempotency_key_is_validated(self):
        created = self.make_list()
        response = self.client.post(
            f"/api/lists/{created['id']}/tasks",
            json_body={"title": "Milk"},
            headers={"x-csrf-token": "csrf-owner", "idempotency-key": "bad key!"},
        )
        self.assertEqual(response.status, 422)

    # -- roles and blocks ----------------------------------------------

    def test_viewer_can_read_but_not_write(self):
        created = self.make_list()
        self.make_task(created["id"])
        with database.connection() as conn:
            conn.execute(
                "INSERT INTO list_shares (list_id, user_id, role) VALUES (?, ?, 'viewer')",
                (created["id"], self.other_uid),
            )

        self.assertEqual(
            self.other.get(f"/api/lists/{created['id']}/tasks").status, 200
        )
        self.assertEqual(
            self.post(
                f"/api/lists/{created['id']}/tasks", {"title": "nope"}, client=self.other
            ).status,
            403,
        )

    def test_blocked_user_loses_access_even_with_a_share(self):
        created = self.make_list()
        with database.connection() as conn:
            conn.execute(
                "INSERT INTO list_shares (list_id, user_id, role) VALUES (?, ?, 'editor')",
                (created["id"], self.other_uid),
            )
            conn.execute(
                "INSERT INTO list_blocks (list_id, user_id) VALUES (?, ?)",
                (created["id"], self.other_uid),
            )

        self.assertEqual(
            self.other.get(f"/api/lists/{created['id']}/tasks").status, 404
        )
        self.assertEqual(self.other.get("/api/lists").json()["lists"], [])

    def test_editor_can_modify(self):
        created = self.make_list()
        with database.connection() as conn:
            conn.execute(
                "INSERT INTO list_shares (list_id, user_id, role) VALUES (?, ?, 'editor')",
                (created["id"], self.other_uid),
            )
        response = self.post(
            f"/api/lists/{created['id']}/tasks", {"title": "added"}, client=self.other
        )
        self.assertEqual(response.status, 201)
        listing = self.other.get("/api/lists").json()["lists"]
        self.assertEqual(listing[0]["role"], "editor")

    # -- state, activity, presence --------------------------------------

    def test_state_returns_everything_and_304_when_unchanged(self):
        created = self.make_list()
        self.make_task(created["id"], "Milk")

        state = self.client.get(f"/api/lists/{created['id']}/state").json()
        self.assertEqual(state["version"], 2)
        self.assertEqual([task["title"] for task in state["tasks"]], ["Milk"])
        self.assertEqual(state["list"]["title"], "Chores")
        for key in ("activity", "presence", "members", "server_time"):
            self.assertIn(key, state)

        unchanged = self.client.get(
            f"/api/lists/{created['id']}/state?since={state['version']}"
        )
        self.assertEqual(unchanged.status, 304)
        self.assertEqual(unchanged.body, b"")

        changed = self.client.get(f"/api/lists/{created['id']}/state?since=1")
        self.assertEqual(changed.status, 200)

    def test_state_records_activity_with_names(self):
        created = self.make_list()
        task = self.make_task(created["id"], "Milk")
        self.patch(f"/api/tasks/{task['id']}", {"status": "completed"})

        state = self.client.get(f"/api/lists/{created['id']}/state").json()
        actions = [entry["action"] for entry in state["activity"]]
        self.assertEqual(actions[0], "task.completed")
        self.assertIn("task.added", actions)
        self.assertIn("list.created", actions)

        newest = state["activity"][0]
        self.assertEqual(newest["user_name"], "Owner")
        self.assertEqual(newest["detail"], "Milk")

    def test_state_requires_access(self):
        created = self.make_list()
        self.assertEqual(
            self.other.get(f"/api/lists/{created['id']}/state").status, 404
        )

    def test_presence_lists_everyone_viewing(self):
        created = self.make_list()
        with database.connection() as conn:
            conn.execute(
                "INSERT INTO list_shares (list_id, user_id, role) VALUES (?, ?, 'editor')",
                (created["id"], self.other_uid),
            )

        self.client.get(
            f"/api/lists/{created['id']}/state",
            headers={"x-client-id": "client-owner-1"},
        )
        state = self.other.get(
            f"/api/lists/{created['id']}/state",
            headers={"x-client-id": "client-other-2"},
        ).json()

        names = {entry["name"] for entry in state["presence"]}
        self.assertIn("Owner", names)
        self.assertIn("Other", names)

    def test_if_match_rejects_a_stale_version(self):
        created = self.make_list()
        first = self.make_task(created["id"], "one")
        second = self.make_task(created["id"], "two")
        current = self.client.get(f"/api/lists/{created['id']}/state").json()["version"]
        ids = [second["id"], first["id"]]

        stale = self.client.post(
            f"/api/lists/{created['id']}/reorder",
            json_body={"task_ids": ids},
            headers={"x-csrf-token": "csrf-owner", "if-match": str(current - 1)},
        )
        self.assertEqual(stale.status, 409)

        fresh = self.client.post(
            f"/api/lists/{created['id']}/reorder",
            json_body={"task_ids": ids},
            headers={"x-csrf-token": "csrf-owner", "if-match": str(current)},
        )
        self.assertEqual(fresh.status, 200)

    def test_if_match_on_rename(self):
        created = self.make_list()
        stale = self.client.patch(
            f"/api/lists/{created['id']}",
            json_body={"title": "Renamed"},
            headers={"x-csrf-token": "csrf-owner", "if-match": "99"},
        )
        self.assertEqual(stale.status, 409)

        fresh = self.client.patch(
            f"/api/lists/{created['id']}",
            json_body={"title": "Renamed"},
            headers={"x-csrf-token": "csrf-owner", "if-match": "1"},
        )
        self.assertEqual(fresh.status, 200)
        self.assertEqual(fresh.json()["title"], "Renamed")


class SharingTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        self.owner_id = create_user("owner@test.local", "Owner")
        self.other_id = create_user("other@test.local", "Other")
        self.third_id = create_user("third@test.local", "Third")

        self.client = TestClient(main.app)
        self.client.cookies[settings.cookie_name] = session_cookie(
            self.owner_id, csrf="csrf-owner"
        )
        self.other = TestClient(main.app)
        self.other.cookies[settings.cookie_name] = session_cookie(
            self.other_id, csrf="csrf-other"
        )
        self.third = TestClient(main.app)
        self.third.cookies[settings.cookie_name] = session_cookie(
            self.third_id, csrf="csrf-third"
        )

    # -- helpers -------------------------------------------------------

    def post(self, path, body, client=None, token="csrf-owner"):
        client = client or self.client
        return client.post(path, json_body=body, headers={"x-csrf-token": token})

    def patch(self, path, body, client=None, token="csrf-owner"):
        client = client or self.client
        return client.patch(path, json_body=body, headers={"x-csrf-token": token})

    def delete(self, path, client=None, token="csrf-owner"):
        client = client or self.client
        return client.delete(path, headers={"x-csrf-token": token})

    def make_list(self, title="Chores"):
        response = self.post("/api/lists", {"title": title})
        self.assertEqual(response.status, 201)
        return response.json()

    def make_link(self, list_id, **overrides):
        payload = {"role": "editor", "max_uses": 1, "expires_in_days": 7}
        payload.update(overrides)
        response = self.post(f"/api/lists/{list_id}/invite-links", payload)
        self.assertEqual(response.status, 201)
        link = response.json()
        link["token"] = link["url"].rsplit("/join/", 1)[1]
        return link

    # -- invite links --------------------------------------------------

    def test_link_grants_access_once(self):
        created = self.make_list()
        link = self.make_link(created["id"])

        joined = self.other.get(f"/join/{link['token']}")
        self.assertEqual(joined.status, 303)
        self.assertEqual(joined.location, f"/#/list/{created['id']}")

        listing = self.other.get("/api/lists").json()["lists"]
        self.assertEqual([(entry["id"], entry["role"]) for entry in listing], [(created["id"], "editor")])

        # A single-use link is spent.
        again = self.third.get(f"/join/{link['token']}")
        self.assertEqual(again.status, 410)

    def test_viewer_link_is_read_only(self):
        created = self.make_list()
        link = self.make_link(created["id"], role="viewer")
        self.other.get(f"/join/{link['token']}")

        self.assertEqual(
            self.other.get(f"/api/lists/{created['id']}/tasks").status, 200
        )
        response = self.other.post(
            f"/api/lists/{created['id']}/tasks",
            json_body={"title": "nope"},
            headers={"x-csrf-token": "csrf-other"},
        )
        self.assertEqual(response.status, 403)

    def test_revoked_link_cannot_be_used(self):
        created = self.make_list()
        link = self.make_link(created["id"], max_uses=5)
        self.assertEqual(
            self.delete(f"/api/lists/{created['id']}/invite-links/{link['id']}").status,
            204,
        )
        self.assertEqual(self.other.get(f"/join/{link['token']}").status, 410)

    def test_expired_link_cannot_be_used(self):
        created = self.make_list()
        link = self.make_link(created["id"], max_uses=5)
        with database.connection() as conn:
            conn.execute(
                "UPDATE invite_tokens SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (link["id"],),
            )
        self.assertEqual(self.other.get(f"/join/{link['token']}").status, 410)

    def test_signed_out_visitor_is_sent_to_login(self):
        created = self.make_list()
        link = self.make_link(created["id"])
        anon = TestClient(main.app)
        response = anon.get(f"/join/{link['token']}")
        self.assertEqual(response.status, 302)
        self.assertTrue(response.location.startswith("/auth/login?next=/join/"))

    def test_dev_login_can_resume_an_invite(self):
        created = self.make_list()
        link = self.make_link(created["id"])
        anon = TestClient(main.app)

        login = anon.get(
            f"/auth/dev-login?email=new@test.local&next=/join/{link['token']}"
        )
        self.assertEqual(login.status, 303)
        self.assertEqual(login.location, f"/join/{link['token']}")

        joined = anon.get(f"/join/{link['token']}")
        self.assertEqual(joined.status, 303)
        self.assertEqual(joined.location, f"/#/list/{created['id']}")

        listing = anon.get("/api/lists").json()["lists"]
        self.assertEqual([(entry["id"], entry["role"]) for entry in listing], [(created["id"], "editor")])

    def test_link_validation(self):
        created = self.make_list()
        for payload in (
            {"role": "owner"},
            {"max_uses": 0},
            {"max_uses": 101},
            {"expires_in_days": 0},
            {"expires_in_days": 91},
        ):
            self.assertEqual(
                self.post(f"/api/lists/{created['id']}/invite-links", payload).status,
                422,
            )

    # -- membership ----------------------------------------------------

    def test_removal_blocks_rejoining_and_unblock_restores_it(self):
        created = self.make_list()
        link = self.make_link(created["id"], max_uses=5)
        self.other.get(f"/join/{link['token']}")

        removed = self.delete(f"/api/lists/{created['id']}/members/{self.other_id}")
        self.assertEqual(removed.status, 204)
        self.assertEqual(self.other.get(f"/api/lists/{created['id']}/tasks").status, 404)

        # The link is still active, but a removed person is blocked.
        self.assertEqual(self.other.get(f"/join/{link['token']}").status, 403)

        unblocked = self.delete(f"/api/lists/{created['id']}/blocks/{self.other_id}")
        self.assertEqual(unblocked.status, 204)
        self.assertEqual(self.other.get(f"/join/{link['token']}").status, 303)
        self.assertEqual(
            self.other.get(f"/api/lists/{created['id']}/tasks").status, 200
        )

    def test_role_change_takes_effect(self):
        created = self.make_list()
        link = self.make_link(created["id"], max_uses=5)
        self.other.get(f"/join/{link['token']}")

        response = self.patch(
            f"/api/lists/{created['id']}/members/{self.other_id}", {"role": "viewer"}
        )
        self.assertEqual(response.status, 200)

        blocked = self.other.post(
            f"/api/lists/{created['id']}/tasks",
            json_body={"title": "nope"},
            headers={"x-csrf-token": "csrf-other"},
        )
        self.assertEqual(blocked.status, 403)

    def test_only_the_owner_can_manage_sharing(self):
        created = self.make_list()
        link = self.make_link(created["id"], max_uses=5)
        self.other.get(f"/join/{link['token']}")

        self.assertEqual(
            self.post(
                f"/api/lists/{created['id']}/invite-links",
                {"role": "editor"},
                client=self.other,
                token="csrf-other",
            ).status,
            403,
        )
        self.assertEqual(
            self.patch(
                f"/api/lists/{created['id']}/members/{self.owner_id}",
                {"role": "viewer"},
                client=self.other,
                token="csrf-other",
            ).status,
            403,
        )
        self.assertEqual(
            self.delete(
                f"/api/lists/{created['id']}/members/{self.other_id}",
                client=self.other,
                token="csrf-other",
            ).status,
            403,
        )

    def test_owner_extras_only_reach_the_owner(self):
        created = self.make_list()
        self.make_link(created["id"])
        link = self.make_link(created["id"], max_uses=5)
        self.other.get(f"/join/{link['token']}")

        owner_state = self.client.get(f"/api/lists/{created['id']}/state").json()
        self.assertIn("invite_links", owner_state)
        self.assertIn("blocks", owner_state)

        member_state = self.other.get(f"/api/lists/{created['id']}/state").json()
        self.assertNotIn("invite_links", member_state)
        self.assertNotIn("blocks", member_state)

    # -- scheduled reset -----------------------------------------------

    def test_scheduled_reset_runs_exactly_once(self):
        created = self.make_list()
        response = self.patch(
            f"/api/lists/{created['id']}",
            {"reset_hour": 0, "reset_tz": "UTC"},
        )
        self.assertEqual(response.status, 200)

        task = self.post(f"/api/lists/{created['id']}/tasks", {"title": "Milk"}).json()["task"]
        self.patch(f"/api/tasks/{task['id']}", {"status": "completed"})

        with database.connection() as conn:
            conn.execute(
                "UPDATE lists SET next_reset_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (created["id"],),
            )

        state = self.client.get(f"/api/lists/{created['id']}/state").json()
        self.assertEqual(state["tasks"][0]["status"], "pending")

        with database.connection() as conn:
            resets = conn.execute(
                "SELECT COUNT(*) FROM activity WHERE list_id = ? AND action = 'list.auto_reset'",
                (created["id"],),
            ).fetchone()[0]
            next_at = conn.execute(
                "SELECT next_reset_at FROM lists WHERE id = ?", (created["id"],)
            ).fetchone()[0]
        self.assertEqual(resets, 1)
        self.assertGreater(next_at, utcnow())

        # Reading again must not reset a second time.
        self.client.get(f"/api/lists/{created['id']}/state")
        with database.connection() as conn:
            resets = conn.execute(
                "SELECT COUNT(*) FROM activity WHERE list_id = ? AND action = 'list.auto_reset'",
                (created["id"],),
            ).fetchone()[0]
        self.assertEqual(resets, 1)

    def test_schedule_validation(self):
        created = self.make_list()
        self.assertEqual(
            self.patch(f"/api/lists/{created['id']}", {"reset_hour": 24}).status, 422
        )
        self.assertEqual(
            self.patch(
                f"/api/lists/{created['id']}", {"reset_hour": 8, "reset_tz": "not a zone!"}
            ).status,
            422,
        )
        cleared = self.patch(
            f"/api/lists/{created['id']}", {"reset_hour": None}
        )
        self.assertEqual(cleared.status, 200)
        with database.connection() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT next_reset_at FROM lists WHERE id = ?", (created["id"],)
                ).fetchone()[0]
            )

    # -- export / import -----------------------------------------------

    def test_export_and_import_round_trip(self):
        created = self.make_list("Groceries")
        first = self.post(f"/api/lists/{created['id']}/tasks", {"title": "Milk"}).json()["task"]
        self.post(f"/api/lists/{created['id']}/tasks", {"title": "Bread"})
        self.patch(f"/api/tasks/{first['id']}", {"status": "completed"})

        exported = self.client.get(f"/api/lists/{created['id']}/export")
        self.assertEqual(exported.status, 200)
        self.assertIn("attachment", exported.headers["content-disposition"])
        payload = exported.json()
        self.assertEqual(payload["format"], "checklist-v2")
        self.assertEqual([task["title"] for task in payload["tasks"]], ["Milk", "Bread"])

        imported = self.client.post(
            "/api/import",
            json_body={"list": payload["list"], "tasks": payload["tasks"]},
            headers={"x-csrf-token": "csrf-owner"},
        )
        self.assertEqual(imported.status, 201)
        new_list = imported.json()["list"]
        self.assertNotEqual(new_list["id"], created["id"])

        tasks = self.client.get(f"/api/lists/{new_list['id']}/tasks").json()["tasks"]
        self.assertEqual([task["title"] for task in tasks], ["Milk", "Bread"])
        self.assertEqual(tasks[0]["status"], "completed")

    def test_import_is_bounded(self):
        response = self.client.post(
            "/api/import",
            json_body={
                "list": {"title": "Too many"},
                "tasks": [{"title": f"item {index}"} for index in range(1001)],
            },
            headers={"x-csrf-token": "csrf-owner"},
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(
            self.client.post(
                "/api/import",
                json_body={"tasks": []},
                headers={"x-csrf-token": "csrf-owner"},
            ).status,
            422,
        )


class MigrationTests(unittest.TestCase):
    def test_schema_version_is_current(self):
        self.assertEqual(database.init_db(), database.SCHEMA_VERSION)

    def test_migrations_are_idempotent(self):
        first = database.init_db()
        second = database.init_db()
        self.assertEqual(first, second)
        self.assertEqual(first, database.SCHEMA_VERSION)

    def test_legacy_database_upgrades_without_data_loss(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            for statement in database.BASE_SCHEMA:
                conn.execute(statement)

            conn.execute("INSERT INTO users (email, name) VALUES ('a@b.c', 'A')")
            conn.execute("INSERT INTO lists (user_id, title) VALUES (1, 'Chores')")
            for title in ("one", "two", "three"):
                conn.execute(
                    "INSERT INTO tasks (list_id, title, position) VALUES (1, ?, 0)",
                    (title,),
                )

            database._migrate_2(conn)
            database._migrate_3(conn)

            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            self.assertTrue({"created_by", "completed_at", "deleted_at"} <= columns)

            list_columns = {row[1] for row in conn.execute("PRAGMA table_info(lists)")}
            self.assertIn("version", list_columns)

            share_columns = {row[1] for row in conn.execute("PRAGMA table_info(list_shares)")}
            self.assertIn("role", share_columns)

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {"invite_tokens", "list_blocks", "activity", "idempotency"} <= tables
            )

            # The old display order was position ASC, id DESC, i.e. newest
            # first. It must survive the renumbering.
            positions = {
                row["id"]: row["position"]
                for row in conn.execute("SELECT id, position FROM tasks")
            }
            self.assertEqual(positions[3], 1024)
            self.assertEqual(positions[2], 2048)
            self.assertEqual(positions[1], 3072)
            self.assertEqual(conn.execute("SELECT version FROM lists").fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
