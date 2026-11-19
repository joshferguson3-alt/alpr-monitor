"""HTTP client: timeout, retries, descriptive User-Agent, robots.txt, per-host delay."""

from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from monitor.constants import HTTP_RETRIES, HTTP_TIMEOUT, MIN_HOST_DELAY, USER_AGENT


def _retry_after(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After") or ""
    try:
        return min(60.0, max(1.0, float(raw)))
    except ValueError:
        return min(60.0, 5.0 * (2 ** (attempt - 1)))


class RobotsDenied(RuntimeError):
    """The host's robots.txt disallows this URL."""


class HttpClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = HTTP_TIMEOUT,
        retries: int = HTTP_RETRIES,
        min_delay: float = MIN_HOST_DELAY,
        user_agent: str = USER_AGENT,
        sleep=time.sleep,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.min_delay = min_delay
        self._sleep = sleep
        self._next_request_at: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._crawl_delay: dict[str, float] = {}
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
        )

    def close(self) -> None:
        self._client.close()

    def pause(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        self._ensure_allowed(url)
        return self._request("GET", url, headers=headers, timeout=timeout, retries=retries)

    def post(
        self,
        url: str,
        *,
        data: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        self._ensure_allowed(url)
        return self._request("POST", url, data=data, headers=headers, timeout=timeout, retries=retries)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        host = urlparse(url).netloc
        delay = max(self.min_delay, self._crawl_delay.get(host, 0))
        attempts = self.retries if retries is None else retries
        request_timeout = self.timeout if timeout is None else timeout
        last_error: Exception | None = None
        rate_waits = 0
        attempt = 0
        while attempt <= attempts:
            self._wait(host, delay)
            try:
                response = self._client.request(
                    method, url, headers=headers, data=data, timeout=request_timeout
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                self._sleep(min(2 ** attempt, 4))
                attempt += 1
                continue
            if response.status_code == 429 and rate_waits < 4:
                rate_waits += 1
                last_error = httpx.HTTPStatusError(
                    "HTTP 429", request=response.request, response=response
                )
                self._sleep(_retry_after(response, rate_waits))
                continue
            if response.status_code in {500, 502, 503, 504} and attempt < attempts:
                last_error = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                self._sleep(min(2 ** attempt, 4))
                attempt += 1
                continue
            response.raise_for_status()
            return response
        if last_error:
            raise last_error
        raise RuntimeError(f"request failed: {url}")

    def _wait(self, host: str, delay: float) -> None:
        now = time.monotonic()
        ready = self._next_request_at.get(host, 0.0)
        if ready > now:
            self._sleep(ready - now)
        self._next_request_at[host] = time.monotonic() + delay

    def _ensure_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.path.endswith("robots.txt"):
            return
        host = parsed.netloc
        if host not in self._robots:
            self._load_robots(parsed.scheme, host)
        parser = self._robots.get(host)
        if parser is None:
            return
        if not parser.can_fetch(self.user_agent, url):
            raise RobotsDenied(f"robots.txt disallows automated access, skipped: {url}")

    def _load_robots(self, scheme: str, host: str) -> None:
        robots_url = f"{scheme}://{host}/robots.txt"
        parser = RobotFileParser()
        try:
            # robots.txt itself is not gated, and must not wait on its own crawl-delay.
            response = self._client.get(robots_url)
        except httpx.HTTPError:
            self._robots[host] = None
            return
        if response.status_code == 404:
            self._robots[host] = None
            return
        if response.status_code >= 400:
            self._robots[host] = None
            return
        parser.parse(response.text.splitlines())
        self._robots[host] = parser
        delay = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*")
        if delay:
            self._crawl_delay[host] = float(delay)
