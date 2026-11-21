"""Minimal, polite HTTP client for source adapters (standard library only).

* Identifies itself honestly; never imitates a browser.
* Retries only transient failures (429, 5xx, network errors) with backoff and
  honours `Retry-After`. Authentication/permission errors are never retried
  or worked around (PRD §6.2).
* Enforces a minimum interval between requests (source rate limits).
* Redacts credentials from every URL it reports.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from idx_trading import __version__

USER_AGENT = f"idx-trading/{__version__} (personal research; python-urllib)"
_SECRET_PARAMS = re.compile(r"(?i)(api_token|apikey|api_key|token|key)=[^&]*")
RETRYABLE = {429, 500, 502, 503, 504}


def redact(url: str) -> str:
    return _SECRET_PARAMS.sub(lambda m: f"{m.group(1)}=***", url)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    content_type: str
    url: str  # redacted
    headers: Mapping[str, str]


#: (url, headers, timeout) -> (status, headers, body). Injected in tests.
Transport = Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]


def urllib_transport(
    url: str, headers: Mapping[str, str], timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b""


class HttpClient:
    def __init__(
        self,
        *,
        transport: Transport = urllib_transport,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        min_interval_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff_seconds
        self.min_interval = min_interval_seconds
        self.sleep = sleep
        self.clock = clock
        self._last: float | None = None
        self.requests_sent = 0

    def _throttle(self) -> None:
        if self._last is not None and self.min_interval > 0:
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def get(self, url: str, params: Mapping[str, str] | None = None) -> HttpResponse:
        if not url.startswith("https://"):
            raise ValueError("only https URLs are allowed")
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/csv;q=0.9"}
        attempt = 0
        while True:
            self._throttle()
            self.requests_sent += 1
            try:
                status, resp_headers, body = self.transport(full, headers, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise ConnectionError(f"{redact(full)}: {exc}") from exc
                status, resp_headers, body = -1, {}, b""
            if status in RETRYABLE or status == -1:
                if attempt >= self.max_retries:
                    return HttpResponse(status, body, "", redact(full), resp_headers)
                delay = self.backoff * (2**attempt)
                retry_after = {k.lower(): v for k, v in resp_headers.items()}.get("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                self.sleep(min(delay, 300.0))
                attempt += 1
                continue
            ctype = {k.lower(): v for k, v in resp_headers.items()}.get("content-type", "")
            return HttpResponse(status, body, ctype.split(";")[0].strip(), redact(full), resp_headers)
