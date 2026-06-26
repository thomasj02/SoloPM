"""Thin HTTP client the CLI uses to talk to the local backend.

Translates HTTP failures into :class:`ApiError`, which carries the backend's
``{code, message}`` so the CLI can render the ``--json`` failure contract faithfully.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """A backend or transport failure, with a stable code for the JSON contract."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class Api:
    """A small wrapper over httpx with actor attribution and uniform error handling."""

    def __init__(self, base_url: str, *, agent: str | None = None, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self._client = client or httpx.Client(base_url=self.base_url, timeout=30.0)

    def _headers(self) -> dict:
        if self.agent:
            return {"X-SoloPM-Actor": self.agent}
        return {}

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = self._client.request(method, path, headers=self._headers(), **kwargs)
        except httpx.ConnectError as exc:
            raise ApiError(
                "unreachable",
                f"Could not reach the SoloPM backend at {self.base_url}. "
                "Is `solopm serve` running?",
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError("transport", f"Request failed: {exc}") from exc

        if resp.status_code >= 400:
            payload = _safe_json(resp)
            if isinstance(payload, dict) and "error" in payload:
                err = payload["error"]
                raise ApiError(
                    err.get("code", "error"),
                    err.get("message", resp.text),
                    status=resp.status_code,
                )
            raise ApiError("http_error", resp.text or f"HTTP {resp.status_code}", status=resp.status_code)

        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def get(self, path: str, **kwargs) -> Any:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self._request("POST", path, json=json, **kwargs)

    def patch(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self._request("PATCH", path, json=json, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self._request("DELETE", path, **kwargs)

    def close(self) -> None:
        # Closing transport must never surface as a command failure.
        try:
            self._client.close()
        except Exception:
            pass


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None
