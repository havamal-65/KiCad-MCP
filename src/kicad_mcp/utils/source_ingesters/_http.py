"""Minimal stdlib HTTP helpers shared by API-backed ingesters.

We deliberately avoid pulling in ``requests`` — the project keeps its
dependency list short, and the API ingesters only need plain GET/POST
with a JSON body.
"""

from __future__ import annotations

import json as _json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "kicad-mcp/0.1 (+https://github.com/havamal-65/kicad-mcp)"
DEFAULT_TIMEOUT_S = 30


class HTTPError(RuntimeError):
    """Raised when an API call returns a non-2xx response."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")
        self.status = status
        self.body = body
        self.url = url


def get_env_token(env_var: str) -> str | None:
    """Return the value of *env_var* if set and non-empty, else None."""
    val = os.environ.get(env_var)
    return val if val else None


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> Any:
    """GET *url* and parse the response as JSON."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return _send_json(req, timeout)


def http_post_json(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> Any:
    """POST *body* (JSON) to *url* and parse the response as JSON."""
    data = _json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return _send_json(req, timeout)


def http_download(url: str, dest: str, *, headers: dict[str, str] | None = None,
                  timeout: int = DEFAULT_TIMEOUT_S) -> int:
    """Stream *url* to *dest*. Returns bytes written."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    written = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    return written


def _send_json(req: urllib.request.Request, timeout: int) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            if not payload:
                return {}
            return _json.loads(payload)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise HTTPError(exc.code, body, req.full_url) from exc
    except urllib.error.URLError as exc:
        raise HTTPError(0, str(exc.reason), req.full_url) from exc
