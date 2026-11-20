"""Request/response primitives shared by every module.

Deliberately tiny: a dataclass for each direction, plus the error type the
dispatcher knows how to turn into a response. No framework, no magic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]  # lower-case names
    cookies: dict[str, str]
    body: bytes
    client_host: str
    scheme: str
    session: dict[str, Any] = field(default_factory=dict)
    user: dict[str, Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_scope(cls, scope: dict, body: bytes) -> "Request":
        headers: dict[str, str] = {}
        for raw_name, raw_value in scope.get("headers", []):
            name = raw_name.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
            # A repeated header is joined rather than dropped; Cookie is the
            # only one that realistically repeats, and joining is correct there.
            headers[name] = f"{headers[name]}, {value}" if name in headers else value

        client = scope.get("client")
        return cls(
            method=scope.get("method", "GET").upper(),
            path=scope.get("path", "/"),
            query=parse_qs(scope.get("query_string", b"").decode("latin-1")),
            headers=headers,
            cookies=parse_cookie_header(headers.get("cookie", "")),
            body=body,
            client_host=client[0] if client else "unknown",
            scheme=scope.get("scheme", "http"),
        )

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        if not self.body:
            raise HTTPError(400, "A JSON body is required")
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPError(400, "Malformed JSON body") from None


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)

    def set_cookie(
        self,
        name: str,
        value: str,
        *,
        max_age: int | None = None,
        path: str = "/",
        secure: bool = False,
        http_only: bool = True,
        same_site: str = "Lax",
    ) -> None:
        parts = [f"{name}={value}", f"Path={path}"]
        if max_age is not None:
            parts.append(f"Max-Age={int(max_age)}")
        if http_only:
            parts.append("HttpOnly")
        if secure:
            parts.append("Secure")
        if same_site:
            parts.append(f"SameSite={same_site}")
        self.cookies.append("; ".join(parts))

    def delete_cookie(self, name: str, *, path: str = "/", secure: bool = False) -> None:
        self.set_cookie(name, "", max_age=0, path=path, secure=secure)


class HTTPError(Exception):
    """A deliberate error with a status code, raised from any handler."""

    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers or {}


def parse_cookie_header(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def json_response(
    data: Any, status: int = 200, headers: dict[str, str] | None = None
) -> Response:
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        merged.update(headers)
    return Response(status=status, body=body, headers=merged)


def text_response(text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> Response:
    return Response(status=status, body=text.encode("utf-8"), headers={"Content-Type": content_type})


def html_response(html: str, status: int = 200) -> Response:
    return Response(
        status=status,
        body=html.encode("utf-8"),
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


def redirect(url: str, status: int = 303) -> Response:
    return Response(status=status, headers={"Location": url})


def no_content() -> Response:
    return Response(status=204)
