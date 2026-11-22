"""HTTP client for the Confucius4-TTS worker (health + one multipart post).

Deliberately independent of ComfyUIClient: no prompts, no history, no
/view, no upload. Each call opens one requests.Session with trust_env=False
and closes it; the POST is sent exactly once (no retry) with a short
connect timeout and the remaining request budget as the read timeout.
Response bodies are streamed and bounded; worker tracebacks and absolute
paths never cross this boundary.
"""
from __future__ import annotations

import time

import requests

CONNECT_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_CHUNK_BYTES = 256 * 1024


class ConfuciusError(Exception):
    """Structured worker error: message + route-level code."""

    def __init__(self, message: str, code: str = "tts_backend_error",
                 details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


def _from_requests(e: Exception) -> ConfuciusError:
    if isinstance(e, requests.exceptions.Timeout):
        return ConfuciusError(
            f"Confucius worker HTTP request timed out: {e}",
            code="tts_backend_timeout")
    if isinstance(e, requests.exceptions.ConnectionError):
        return ConfuciusError(
            f"Cannot reach the Confucius worker: {e}",
            code="tts_backend_unavailable")
    return ConfuciusError(
        f"Confucius worker request failed: {e}", code="tts_backend_error")


def _status_code_to_error(status_code: int) -> str:
    if status_code == 503:
        return "tts_backend_unavailable"
    if status_code == 504:
        return "tts_backend_timeout"
    return "tts_backend_error"


class ConfuciusClient:
    """Small typed wrapper over the worker HTTP API."""

    def __init__(self, base_url: str, connect_timeout: float = CONNECT_TIMEOUT,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        self.base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._max_response_bytes = max_response_bytes

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        return session

    def health(self) -> dict:
        """GET /health as a dict; transport failures raise ConfuciusError."""
        try:
            with self._session() as session:
                response = session.get(
                    self.base_url + "/health",
                    timeout=(self._connect_timeout, 10.0))
        except requests.exceptions.RequestException as e:
            raise _from_requests(e) from e
        try:
            if response.status_code not in (200, 503):
                raise ConfuciusError(
                    "Confucius worker /health returned HTTP"
                    f" {response.status_code}",
                    code=_status_code_to_error(response.status_code))
            try:
                # A worker deliberately serves starting/error as HTTP 503 with
                # a valid JSON state. Preserve that state for health aggregate;
                # preflight, not this transport method, decides readiness.
                payload = response.json()
            except ValueError as e:
                code = ("tts_backend_unavailable"
                        if response.status_code == 503 else "tts_backend_error")
                raise ConfuciusError(
                    "Confucius worker /health returned a non-JSON body",
                    code=code) from e
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise ConfuciusError(
                "Confucius worker /health returned JSON that is not an"
                " object", code="tts_backend_error")
        if not isinstance(payload.get("status"), str) or not payload["status"]:
            raise ConfuciusError(
                "Confucius worker /health has no status string",
                code="tts_backend_error")
        return payload

    def generate(self, *, text: str, lang: str, reference_bytes: bytes,
                 filename: str, content_type: str,
                 deadline: float) -> bytes:
        """One multipart POST /api/tts; returns bounded PCM16 WAV bytes."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConfuciusError(
                "clone deadline reached before the worker POST",
                code="tts_backend_timeout")
        try:
            with self._session() as session:
                response = session.post(
                    self.base_url + "/api/tts",
                    data={"text": text, "lang": lang},
                    files={"reference": (filename, reference_bytes,
                                         content_type)},
                    stream=True,
                    timeout=(self._connect_timeout, remaining))
        except requests.exceptions.RequestException as e:
            raise _from_requests(e) from e
        try:
            if response.status_code != 200:
                raise ConfuciusError(
                    f"Confucius worker returned HTTP {response.status_code}"
                    " for POST /api/tts; response body is intentionally not"
                    " forwarded",
                    code=_status_code_to_error(response.status_code))
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                if time.monotonic() >= deadline:
                    raise ConfuciusError(
                        "clone deadline reached while reading the worker WAV",
                        code="tts_backend_timeout")
                if not chunk:
                    continue
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise ConfuciusError(
                        "Confucius worker response exceeds the"
                        f" {self._max_response_bytes}-byte limit after"
                        f" {total} bytes", code="tts_backend_error")
                chunks.append(chunk)
        except requests.exceptions.RequestException as e:
            raise _from_requests(e) from e
        finally:
            response.close()
        payload = b"".join(chunks)
        if not payload.startswith(b"RIFF"):
            raise ConfuciusError(
                "Confucius worker returned a non-WAV body",
                code="tts_backend_error")
        return payload
