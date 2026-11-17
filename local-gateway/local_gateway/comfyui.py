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


class ComfyUIError(Exception):
    """Structured client error: message, machine-readable code, optional details."""

    def __init__(self, message: str, code: str = "comfyui_error",
                 details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


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
        except requests.exceptions.RequestException as e:
            raise ComfyUIError(
                f"Cannot reach ComfyUI at {self.base_url}: {e}",
                code="comfyui_unreachable",
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

    def view(self, filename: str, subfolder: str = "", file_type: str = "output") -> bytes:
        """GET /view with the three query parameters; returns raw bytes."""
        response = self._request(
            "GET", "/view",
            params={"filename": filename, "subfolder": subfolder, "type": file_type},
        )
        if response.status_code != 200:
            raise ComfyUIError(
                f"ComfyUI returned HTTP {response.status_code} for GET /view:"
                f" {response.text[:200]}",
                code="comfyui_error",
            )
        return response.content

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
