"""Small HTTP client for ripperr's documented consumer contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ApiError(Exception):
    status: int
    body: dict

    def __str__(self) -> str:
        return f"ripperr HTTP {self.status}: {self.body.get('error', 'unknown error')}"


class RipperrClient:
    def __init__(self, base_url: str, token_file: Path, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.timeout = timeout

    def _request(
        self, path: str, *, method: str = "GET", etag: str | None = None, body: dict | None = None
    ) -> tuple[dict | None, str | None, int]:
        token = self.token_file.read_text().strip()
        if not token:
            raise ValueError(f"empty API token file: {self.token_file}")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else b"" if method == "POST" else None,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response), response.headers.get("ETag"), response.status
        except HTTPError as error:
            if error.code == 304:
                return None, error.headers.get("ETag"), 304
            try:
                body = json.load(error)
            except (ValueError, OSError):
                body = {"error": error.reason}
            raise ApiError(error.code, body) from error

    def changes(self, after: int, limit: int = 100) -> dict:
        path = "/v1/changes?" + urlencode({"after": after, "limit": limit})
        body, _, _ = self._request(path)
        assert body is not None
        return body

    def bootstrap(self) -> dict:
        body, _, _ = self._request("/v1/changes/bootstrap", method="POST")
        assert body is not None
        return body

    def episode(self, guid: str, etag: str | None = None) -> tuple[dict | None, str | None]:
        try:
            body, new_etag, _ = self._request(
                "/v1/episodes/" + quote(guid, safe=""), etag=etag
            )
            return body, new_etag
        except ApiError as error:
            if error.status == 404:
                return None, None
            raise

    def feeds(self) -> list[dict]:
        body, _, _ = self._request("/v1/feeds")
        assert body is not None
        return body["feeds"]

    def add_feed(self, url: str, title: str | None = None, backfill: int | None = None) -> dict:
        """Subscribe ripperr to a feed (idempotent). `backfill` also records the
        feed's N newest episodes instead of only the latest."""
        payload: dict = {"url": url}
        if title:
            payload["title"] = title
        if backfill:
            payload["backfill"] = backfill
        body, _, _ = self._request("/v1/feeds", method="POST", body=payload)
        assert body is not None
        return body
