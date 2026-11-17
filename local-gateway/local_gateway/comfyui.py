"""Original ComfyUI HTTP client: protocol and poll termination semantics.

Written against the public ComfyUI HTTP API only (no ComfyUI Python packages,
no third-party client code). One client owns one requests.Session with
trust_env=False; create a client per gateway request and close it, so no
Session is shared across threads.
"""
from __future__ import annotations

import time
from urllib.parse import quote

import requests

DEFAULT_TIMEOUT = (2, 10)

VIEW_CHUNK_BYTES = 256 * 1024
DEFAULT_VIEW_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB per /view response


class ComfyUIError(Exception):
    """Structured client error: message, machine-readable code, optional details."""

    def __init__(self, message: str, code: str = "comfyui_error",
                 details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


class ComfyUITimeout(ComfyUIError):
    """wait() passed its deadline; the ComfyUI job was NOT cancelled."""

    def __init__(self, prompt_id: str, deadline: float):
        super().__init__(
            f"Timed out waiting for ComfyUI prompt {prompt_id}: monotonic deadline"
            f" {deadline:.1f} reached. The ComfyUI job keeps running; this client"
            f" never calls /interrupt.",
            code="comfyui_timeout",
        )
        self.prompt_id = prompt_id


def input_values(meta: dict, input_name: str) -> list | None:
    """Combo choices for one node input, or None when it is not a combo.

    Understands the current dict-keyed object_info shape, the legacy list
    shape, and ComfyUI 0.45.0's COMFY_DYNAMICCOMBO_V3 shape, whose options
    are objects with a string `key` (flac/mp3/opus and their quality fields).
    """
    inputs = meta.get("input", {}) if isinstance(meta, dict) else {}
    for section in ("required", "optional"):
        fields = inputs.get(section)
        if isinstance(fields, dict):
            spec = fields.get(input_name)
            if (isinstance(spec, (list, tuple)) and spec
                    and isinstance(spec[0], list)):
                return spec[0]
            if (isinstance(spec, (list, tuple)) and len(spec) >= 2
                    and spec[0] == "COMFY_DYNAMICCOMBO_V3"
                    and isinstance(spec[1], dict)):
                options = spec[1].get("options")
                if isinstance(options, list):
                    return [option["key"] for option in options
                            if isinstance(option, dict)
                            and isinstance(option.get("key"), str)]
                return None
            if spec is not None:
                return None
        elif isinstance(fields, list):
            for spec in fields:
                if (isinstance(spec, (list, tuple)) and len(spec) >= 2
                        and spec[0] == input_name):
                    return spec[1] if isinstance(spec[1], list) else None
    return None


def input_names(meta: dict) -> set[str]:
    """Input names across required+optional, both object_info shapes."""
    names: set[str] = set()
    inputs = meta.get("input", {}) if isinstance(meta, dict) else {}
    for section in ("required", "optional"):
        fields = inputs.get(section)
        if isinstance(fields, dict):
            names.update(str(k) for k in fields)
        elif isinstance(fields, list):
            for entry in fields:
                if isinstance(entry, (list, tuple)) and entry:
                    names.add(str(entry[0]))
    return names


class ComfyUIClient:
    """Small typed wrapper over the ComfyUI HTTP API."""

    def __init__(self, base_url: str, timeout: tuple[float, float] = DEFAULT_TIMEOUT,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False

    def __enter__(self) -> "ComfyUIClient":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._session.close()

    # -- request helpers ---------------------------------------------------

    def _request(self, method: str, path: str, **kwargs):
        url = self.base_url + path
        try:
            if method == "GET":
                return self._session.get(url, timeout=self._timeout, **kwargs)
            return self._session.post(url, timeout=self._timeout, **kwargs)
        except requests.exceptions.Timeout as e:
            raise ComfyUIError(
                f"ComfyUI HTTP request timed out ({method} {path}): {e}",
                code="comfyui_request_timeout",
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise ComfyUIError(
                f"Cannot reach ComfyUI at {self.base_url}: {e}",
                code="comfyui_unreachable",
            ) from e
        except (requests.exceptions.InvalidURL,
                requests.exceptions.MissingSchema) as e:
            raise ComfyUIError(
                f"Invalid ComfyUI URL for {method} {path}: {e}",
                code="comfyui_error",
            ) from e
        except requests.exceptions.RequestException as e:
            raise ComfyUIError(
                f"ComfyUI request failed for {method} {path}: {e}",
                code="comfyui_error",
            ) from e

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        response = self._request("GET", path, params=params)
        return self._json_or_error(response, "GET " + path)

    def _post_json(self, path: str, *, json=None, files=None, data=None) -> dict:
        response = self._request("POST", path, json=json, files=files, data=data)
        return self._json_or_error(response, "POST " + path)

    @staticmethod
    def _json_or_error(response, what: str) -> dict:
        if response.status_code != 200:
            raise ComfyUIError(
                f"ComfyUI returned HTTP {response.status_code} for {what}:"
                f" {response.text[:200]}",
                code="comfyui_error",
            )
        try:
            body = response.json()
        except ValueError as e:
            raise ComfyUIError(
                f"ComfyUI returned a non-JSON body for {what}: {response.text[:200]}",
                code="comfyui_error",
            ) from e
        if not isinstance(body, dict):
            raise ComfyUIError(
                f"ComfyUI returned JSON that is not an object for {what}.",
                code="comfyui_error",
            )
        return body

    # -- API methods (spec 8.1) --------------------------------------------

    def system_stats(self) -> dict:
        """GET /system_stats as a JSON dict."""
        return self._get_json("/system_stats")

    def object_info(self, node_name: str | None = None) -> dict:
        """GET /object_info, unwrapping the single-node envelope used by 0.45.0."""
        if node_name is None:
            return self._get_json("/object_info")
        body = self._get_json(f"/object_info/{quote(node_name)}")
        schema = body.get(node_name)
        if not isinstance(schema, dict):
            raise ComfyUIError(
                f"ComfyUI has no object_info schema for node {node_name!r}.",
                code="node_missing",
            )
        return schema

    def submit(self, prompt: dict) -> str:
        """POST /prompt with the workflow; returns the prompt_id string."""
        body = self._post_json("/prompt", json={"prompt": prompt})
        prompt_id = body.get("prompt_id")
        if not prompt_id or not isinstance(prompt_id, str):
            raise ComfyUIError(
                f"ComfyUI accepted POST /prompt but returned no prompt_id:"
                f" {str(body)[:200]}",
                code="comfyui_error",
            )
        return prompt_id

    def history(self, prompt_id: str) -> dict:
        """GET /history/{prompt_id}; callers treat a missing id as not-finished."""
        return self._get_json(f"/history/{quote(prompt_id)}")

    def view(self, filename: str, subfolder: str = "", file_type: str = "output",
             max_bytes: int = DEFAULT_VIEW_MAX_BYTES) -> bytes:
        """GET /view streamed in chunks and bounded by max_bytes.

        Reading stops (and the response is closed) as soon as the running
        total exceeds max_bytes, so an oversized or hostile output cannot
        make the gateway allocate unbounded memory. Overflow raises
        generation_failed per the P2 spec.
        """
        response = self._request(
            "GET", "/view", stream=True,
            params={"filename": filename, "subfolder": subfolder, "type": file_type},
        )
        if response.status_code != 200:
            response.close()
            raise ComfyUIError(
                f"ComfyUI returned HTTP {response.status_code} for GET /view:"
                f" {response.text[:200]}",
                code="comfyui_error",
            )
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=VIEW_CHUNK_BYTES):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ComfyUIError(
                        f"ComfyUI /view output for {filename!r} exceeds the"
                        f" {max_bytes}-byte limit after {total} bytes.",
                        code="generation_failed",
                    )
                chunks.append(chunk)
        finally:
            response.close()
        return b"".join(chunks)

    def upload(self, filename: str, content: bytes,
               content_type: str = "application/octet-stream") -> dict:
        """multipart POST /upload/image with type=input and overwrite=false.

        The multipart field stays `image` even for audio: that is the field
        name ComfyUI's protocol uses for input uploads.
        """
        return self._post_json(
            "/upload/image",
            files={"image": (filename, content, content_type)},
            data={"type": "input", "overwrite": "false"},
        )

    def wait(self, prompt_id: str, deadline: float, poll_interval: float = 1.0) -> dict:
        """Poll history until the prompt finishes, fails, or the deadline passes.

        `deadline` is an absolute time.monotonic() timestamp. Individual poll
        timeouts are not a total deadline, and this method never calls
        /interrupt: a ComfyUITimeout leaves the backend job running.
        """
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be > 0, got {poll_interval}")
        if deadline <= time.monotonic():
            raise ValueError(
                f"deadline must be in the future of time.monotonic(), got {deadline}"
            )
        while True:
            data = self.history(prompt_id)
            entry = data.get(prompt_id) if isinstance(data, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                status_str = status.get("status_str")
                completed = bool(status.get("completed"))
                if completed and status_str == "success":
                    return entry
                if completed or status_str in ("error", "cancelled"):
                    raise ComfyUIError(
                        f"ComfyUI execution failed for prompt {prompt_id}:"
                        f" status_str={status_str!r}, completed={completed},"
                        f" status={status}",
                        code="execution_error",
                        details=status,
                    )
            if time.monotonic() >= deadline:
                raise ComfyUITimeout(prompt_id, deadline)
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
