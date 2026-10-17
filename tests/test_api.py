"""End-to-end tests for authentication, access control, and validation.

Runs standalone, no pytest required:

    .\\.venv\\Scripts\\python.exe tests\\test_api.py

Exits non-zero if any check fails. Covers the endpoints that mutate data, since
those are the ones where an authorization mistake is actually exploitable.
"""

import base64
import json
import os
import shutil
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    # Point the database at a temporary file via the documented override, so the
    # suite never touches the real checklist.db. No working-directory juggling
    # is needed: every path in the app is anchored to the project directory.
    workdir = tempfile.mkdtemp(prefix="checklist-test-")
    os.environ["DATABASE_PATH"] = os.path.join(workdir, "test.db")
    os.environ.setdefault("SESSION_SECRET", "z" * 48)
    sys.path.insert(0, PROJECT_ROOT)

    import itsdangerous
    from starlette.testclient import TestClient

    import main
    from database import connect

    secret = os.environ["SESSION_SECRET"]

    def make_cookie(user_id, email, name):
        payload = base64.b64encode(
            json.dumps({"user": {"id": user_id, "email": email, "name": name}}).encode()
        )
        return itsdangerous.TimestampSigner(secret).sign(payload).decode()

    conn = connect()
    conn.execute("INSERT INTO users (email,name) VALUES ('owner@test.local','Owner')")
    conn.execute("INSERT INTO users (email,name) VALUES ('stranger@test.local','Stranger')")
    conn.commit()
    conn.close()

    owner = TestClient(main.app)
    stranger = TestClient(main.app)
    owner.cookies.set("session", make_cookie(1, "owner@test.local", "Owner"))
    stranger.cookies.set("session", make_cookie(2, "stranger@test.local", "Stranger"))

    results = []

    def check(label, got, want):
        ok = got == want
        results.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got}, want {want}")

    print("== unauthenticated requests are rejected ==")
    anon = TestClient(main.app)
    check("GET /api/lists", anon.get("/api/lists").status_code, 401)
    check("POST /api/lists", anon.post("/api/lists", json={"title": "x"}).status_code, 401)
    check("DELETE /api/lists/1", anon.delete("/api/lists/1").status_code, 401)

    print("== owner creates a list and a task ==")
    resp = owner.post("/api/lists", json={"title": "Groceries", "is_resettable": True})
    check("create list", resp.status_code, 200)
    list_id = resp.json()["id"]

    resp = owner.post(f"/api/lists/{list_id}/tasks", json={"title": "Milk"})
    check("create task", resp.status_code, 200)
    task_id = resp.json()["id"]

    print("== owner has access ==")
    check("GET tasks", owner.get(f"/api/lists/{list_id}/tasks").status_code, 200)
    check("GET lists", owner.get("/api/lists").status_code, 200)

    print("== a user with no share is denied everywhere ==")
    check("GET tasks", stranger.get(f"/api/lists/{list_id}/tasks").status_code, 403)
    check("POST task", stranger.post(f"/api/lists/{list_id}/tasks", json={"title": "x"}).status_code, 403)
    check("PATCH task", stranger.patch(f"/api/tasks/{task_id}", json={"status": "completed"}).status_code, 403)
    check("DELETE task", stranger.delete(f"/api/tasks/{task_id}").status_code, 403)
    check("DELETE list", stranger.delete(f"/api/lists/{list_id}").status_code, 404)
    check("RESET list", stranger.post(f"/api/lists/{list_id}/reset").status_code, 403)
    check(
        "SHARE list",
        stranger.post(f"/api/lists/{list_id}/share", json={"email": "stranger@test.local"}).status_code,
        403,
    )

    print("== development login is inert unless explicitly enabled ==")
    check(
        "/auth/dev-login is 404 when DEV_AUTH_ENABLED is off",
        owner.get("/auth/dev-login").status_code,
        404,
    )

    print("== sharing and revoking ==")
    check(
        "owner shares with the stranger",
        owner.post(f"/api/lists/{list_id}/share", json={"email": "stranger@test.local"}).status_code,
        200,
    )
    shares = owner.get(f"/api/lists/{list_id}/shares")
    check("owner can list shares", shares.status_code, 200)
    check("one share present", len(shares.json()), 1)

    check("stranger can now read tasks", stranger.get(f"/api/lists/{list_id}/tasks").status_code, 200)
    check(
        "stranger cannot see the share list",
        stranger.get(f"/api/lists/{list_id}/shares").status_code,
        404,
    )

    check(
        "owner cannot share with themselves",
        owner.post(f"/api/lists/{list_id}/share", json={"email": "owner@test.local"}).status_code,
        400,
    )

    check(
        "re-sharing is idempotent",
        owner.post(f"/api/lists/{list_id}/share", json={"email": "stranger@test.local"}).json().get(
            "already"
        ),
        True,
    )

    sharee_id = shares.json()[0]["user_id"]
    check(
        "stranger cannot revoke their own access",
        stranger.request("DELETE", f"/api/lists/{list_id}/shares", json={"email": "stranger@test.local"}).status_code,
        404,
    )

    check(
        "owner revokes the share",
        owner.request(
            "DELETE", f"/api/lists/{list_id}/shares", json={"email": "stranger@test.local"}
        ).status_code,
        200,
    )
    check("share list is now empty", len(owner.get(f"/api/lists/{list_id}/shares").json()), 0)
    check(
        "stranger has lost access",
        stranger.get(f"/api/lists/{list_id}/tasks").status_code,
        403,
    )
    check(
        "revoking twice reports not found",
        owner.request(
            "DELETE", f"/api/lists/{list_id}/shares", json={"email": "stranger@test.local"}
        ).status_code,
        404,
    )

    print("== email enumeration is closed ==")
    registered = owner.post(f"/api/lists/{list_id}/share", json={"email": "stranger@test.local"})
    unregistered = owner.post(f"/api/lists/{list_id}/share", json={"email": "never-seen@test.local"})
    check("registered and unregistered both return 200",
          (registered.status_code, unregistered.status_code), (200, 200))
    check("both responses carry the same keys",
          sorted(registered.json()), sorted(unregistered.json()))
    check("only the status value differs",
          (registered.json()["status"], unregistered.json()["status"]), ("shared", "invited"))

    print("== revocation while the sharee already has the list open ==")
    open_tasks = stranger.get(f"/api/lists/{list_id}/tasks")
    check("sharee loads tasks before revocation", open_tasks.status_code, 200)
    check("sharee sees the task title", open_tasks.json()[0]["title"], "Milk")

    owner.request("DELETE", f"/api/lists/{list_id}/shares", json={"email": "stranger@test.local"})

    # Access is re-evaluated per request, so every fresh call is denied.
    check("sharee cannot re-read the list", stranger.get(f"/api/lists/{list_id}/tasks").status_code, 403)
    check("sharee cannot add a task",
          stranger.post(f"/api/lists/{list_id}/tasks", json={"title": "sneaky"}).status_code, 403)
    check("sharee cannot reorder",
          stranger.post(f"/api/lists/{list_id}/reorder", json={"task_ids": [task_id]}).status_code, 403)
    check("sharee cannot reset",
          stranger.post(f"/api/lists/{list_id}/reset").status_code, 403)
    check("the list is gone from the sharee's list view",
          any(x["id"] == list_id for x in stranger.get("/api/lists").json()), False)

    print("== pending invites ==")
    owner.post(f"/api/lists/{list_id}/share", json={"email": "never-seen@test.local"})
    entries = owner.get(f"/api/lists/{list_id}/shares").json()
    pending = [e for e in entries if e["status"] == "pending"]
    check("the unseen address is listed as pending", len(pending), 1)
    check("pending entry carries the email", pending[0]["email"], "never-seen@test.local")
    check("confirmed entries are listed first",
          [e["status"] for e in entries].index("pending") == len(entries) - 1, True)

    # First sign-in for that address claims the invite, same path login uses.
    main.upsert_user("never-seen@test.local", "Newcomer")
    after = owner.get(f"/api/lists/{list_id}/shares").json()
    check("the invite became a confirmed share on signup",
          any(e["status"] == "confirmed" and e["email"] == "never-seen@test.local" for e in after), True)
    check("no pending entries remain", [e for e in after if e["status"] == "pending"], [])
    db = main.connect()
    check("the invite table was emptied",
          db.execute("SELECT COUNT(*) FROM list_invites").fetchone()[0], 0)
    check("the share landed in list_shares",
          db.execute("""
              SELECT COUNT(*) FROM list_shares ls JOIN users u ON u.id = ls.user_id
              WHERE u.email = 'never-seen@test.local'
          """).fetchone()[0], 1)
    db.close()

    print("== a revoked invite is never granted ==")
    owner.post(f"/api/lists/{list_id}/share", json={"email": "future@test.local"})
    check("it shows as pending",
          any(e["status"] == "pending" and e["email"] == "future@test.local"
              for e in owner.get(f"/api/lists/{list_id}/shares").json()), True)
    check("owner revokes the invite",
          owner.request("DELETE", f"/api/lists/{list_id}/shares", json={"email": "future@test.local"}).status_code,
          200)
    main.upsert_user("future@test.local", "Future")
    check("signing up does not resurrect it",
          any(e["email"] == "future@test.local"
              for e in owner.get(f"/api/lists/{list_id}/shares").json()), False)

    print("== invites are case-insensitive ==")
    mixed = owner.post(f"/api/lists/{list_id}/share", json={"email": "MixedCase@Test.LOCAL"})
    main.upsert_user("mixedcase@test.local", "Mixed")
    check("an address invited in mixed case still matches on signup",
          any(e["email"] == "mixedcase@test.local" and e["status"] == "confirmed"
              for e in owner.get(f"/api/lists/{list_id}/shares").json()), True)

    print("== body validation ==")
    check(
        "PATCH invalid status value",
        owner.patch(f"/api/tasks/{task_id}", json={"status": "evil"}).status_code,
        422,
    )
    check(
        "PATCH malformed JSON",
        owner.patch(
            f"/api/tasks/{task_id}",
            content="{not json",
            headers={"content-type": "application/json"},
        ).status_code,
        422,
    )

    print("== deleting a list cascades to its tasks and shares ==")
    conn = connect()
    before = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE list_id = ?", (list_id,)
    ).fetchone()[0]
    conn.close()
    check("task existed before delete", before, 1)

    owner.delete(f"/api/lists/{list_id}")

    conn = connect()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE list_id = ?", (list_id,)
    ).fetchone()[0]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM tasks t LEFT JOIN lists l ON t.list_id = l.id WHERE l.id IS NULL"
    ).fetchone()[0]
    conn.close()
    check("tasks for the deleted list are gone", remaining, 0)
    check("no orphaned tasks remain", orphans, 0)

    passed = sum(results)
    print()
    print(f"{passed}/{len(results)} checks passed")

    shutil.rmtree(workdir, ignore_errors=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
