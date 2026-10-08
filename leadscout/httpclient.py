"""Shared HTTP helpers: a polite fetch (timeout, real UA) and a tiny result type."""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

try:
    import certifi

    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    # certifi is optional; some Python installs (notably python.org builds on
    # macOS) ship without a usable system CA bundle, so HTTPS can fail with
    # CERTIFICATE_VERIFY_FAILED. `pip install certifi` fixes it; without it we
    # fall back to the interpreter's default context.
    _SSL_CONTEXT = None


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict
    body: bytes
    elapsed_ms: float
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

    def text(self, encoding: str = "utf-8") -> str:
        try:
            return self.body.decode(encoding, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def fetch(url: str, user_agent: str, timeout: float = 10.0, method: str = "GET",
          data: Optional[bytes] = None, extra_headers: Optional[dict] = None) -> FetchResult:
    """Fetch a URL with a timeout and an honest User-Agent. Never raises."""
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            # Read in chunks with a total deadline and a size cap: `timeout` alone is
            # per socket operation, so a server that drips bytes could hang us forever.
            chunks, size, deadline = [], 0, start + timeout * 3
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 8_000_000 or time.monotonic() > deadline:
                    break
            body = b"".join(chunks)
            elapsed = (time.monotonic() - start) * 1000
            return FetchResult(
                url=url,
                final_url=resp.geturl(),
                status=resp.status,
                headers=dict(resp.headers.items()),
                body=body,
                elapsed_ms=elapsed,
            )
    except urllib.error.HTTPError as e:
        elapsed = (time.monotonic() - start) * 1000
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return FetchResult(
            url=url, final_url=url, status=e.code, headers=dict(e.headers.items()) if e.headers else {},
            body=body, elapsed_ms=elapsed, error=None,
        )
    except Exception as e:  # noqa: BLE001 - network calls fail in many ways; report, don't crash
        elapsed = (time.monotonic() - start) * 1000
        return FetchResult(
            url=url, final_url=url, status=0, headers={}, body=b"", elapsed_ms=elapsed, error=str(e)
        )
