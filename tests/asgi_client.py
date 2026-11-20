"""A minimal ASGI test client.

Deliberately does not use httpx or requests: the suite should run with nothing
installed but uvicorn, and driving the ASGI callable directly is both faster
and more deterministic than binding a port. The HTTP layer itself is uvicorn's
job, and it is not what these tests are checking.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode, urlsplit


class TestResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def location(self) -> str:
        return self.headers.get("location", "")

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class TestClient:
    """One client is one browser: cookies persist between requests."""

    def __init__(self, app):
        self.app = app
        self.cookies: dict[str, str] = {}

    # -- verbs ---------------------------------------------------------

    def get(self, path: str, **kwargs) -> TestResponse:
        return self.request("GET", path, **kwargs)

    def head(self, path: str, **kwargs) -> TestResponse:
        return self.request("HEAD", path, **kwargs)

    def post(self, path: str, **kwargs) -> TestResponse:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs) -> TestResponse:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs) -> TestResponse:
        return self.request("DELETE", path, **kwargs)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body=None,
        data: dict | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        parsed = urlsplit(path)
        request_headers: dict[str, str] = {"host": "testserver"}
        if self.cookies:
            request_headers["cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )

        payload = b""
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            request_headers["content-type"] = "application/json"
        elif data is not None:
            payload = urlencode(data).encode("utf-8")
            request_headers["content-type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            payload = body

        if headers:
            request_headers.update({name.lower(): value for name, value in headers.items()})

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": parsed.path or "/",
            "raw_path": (parsed.path or "/").encode("latin-1"),
            "query_string": parsed.query.encode("latin-1"),
            "root_path": "",
            "headers": [
                (name.encode("latin-1"), value.encode("latin-1"))
                for name, value in request_headers.items()
            ],
            "client": ("127.0.0.1", 54321),
            "server": ("testserver", 80),
        }

        return asyncio.run(self._call(scope, payload))

    async def _call(self, scope: dict, payload: bytes) -> TestResponse:
        sent = False
        status = 500
        response_headers: dict[str, str] = {}
        body = bytearray()

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                for name, value in message["headers"]:
                    key = name.decode("latin-1").lower()
                    text = value.decode("latin-1")
                    if key == "set-cookie":
                        self._store_cookie(text)
                    else:
                        response_headers[key] = text
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))

        await self.app(scope, receive, send)
        return TestResponse(status, response_headers, bytes(body))

    def _store_cookie(self, header: str) -> None:
        pair, _, attributes = header.partition(";")
        name, _, value = pair.partition("=")
        name = name.strip()
        if not name:
            return
        if "max-age=0" in attributes.lower():
            self.cookies.pop(name, None)
        else:
            self.cookies[name] = value.strip()
