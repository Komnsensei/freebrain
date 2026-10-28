"""FreeBrain -> AI Creative Studio durable video-job bridge.

This is an opt-in HTTP adapter, not a renderer. It refuses to dispatch unless
both the local FreeBrain access token and Studio tenant token are configured.
The Studio worker must have a real render backend configured; simulated output
is never enabled by this bridge.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
import urllib.parse


class VideoBridgeError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise VideoBridgeError("video bridge is not configured: " + name, 503)
    return value


def authorize(provided_token: str | None) -> None:
    expected = _required_env("FREEBRAIN_VIDEO_ACCESS_TOKEN")
    if not provided_token or not hmac.compare_digest(provided_token, expected):
        raise VideoBridgeError("unauthorized", 401)


def _studio_request(method: str, path: str, payload: dict | None = None,
                    idempotency_key: str | None = None) -> dict:
    base = os.environ.get("FREEBRAIN_VIDEO_STUDIO_URL", "http://127.0.0.1:8000").strip().rstrip("/")
    tenant_token = _required_env("FREEBRAIN_VIDEO_STUDIO_TOKEN")
    if not base.startswith(("http://127.0.0.1", "http://localhost", "https://")):
        raise VideoBridgeError("video Studio URL must be localhost or HTTPS", 503)
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Authorization": "Bearer " + tenant_token, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise VideoBridgeError("Studio response exceeded 1 MB", 502)
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise VideoBridgeError("Studio returned a non-object JSON response", 502)
            return parsed
    except urllib.error.HTTPError as exc:
        # Do not expose upstream bodies, which may contain internal details.
        if exc.code in (400, 401, 403, 404, 409, 422, 429):
            raise VideoBridgeError("Studio rejected the request (HTTP %d)" % exc.code, exc.code) from exc
        raise VideoBridgeError("Studio request failed (HTTP %d)" % exc.code, 502) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise VideoBridgeError("Studio unavailable or returned invalid JSON: " + type(exc).__name__, 502) from exc


def submit_video(payload: dict, idempotency_key: str) -> dict:
    """Submit exactly one durable job. Caller must supply a stable idempotency key."""
    if not idempotency_key or len(idempotency_key) > 200:
        raise VideoBridgeError("X-Idempotency-Key is required (1-200 characters)", 400)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise VideoBridgeError("prompt is required", 400)
    request = {
        "prompt": prompt.strip(),
        "provider": payload.get("provider", "video-cascade"),
        "duration_seconds": payload.get("duration_seconds", 4),
        "aspect_ratio": payload.get("aspect_ratio", "16:9"),
        "resolution": payload.get("resolution", "576p"),
        "fps": payload.get("fps", 12),
    }
    for key in ("negative_prompt", "shots", "model"):
        if key in payload:
            request[key] = payload[key]
    result = _studio_request("POST", "/v1/videos/generate", request, idempotency_key)
    job_id = result.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise VideoBridgeError("Studio response did not include a durable job_id", 502)
    # A stable digest permits later evidence correlation without persisting prompts.
    evidence = {
        "schema": "freebrain-video-handoff-v1",
        "job_id": job_id,
        "idempotency_key_sha256": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
        "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "status": result.get("status", "accepted"),
        "provider": request["provider"],
    }
    result["freebrain_handoff"] = evidence
    return result


def get_video_job(job_id: str) -> dict:
    if not job_id or len(job_id) > 200 or "/" in job_id or "\\" in job_id:
        raise VideoBridgeError("invalid job_id", 400)
    return _studio_request("GET", "/v1/videos/jobs/" + urllib.parse.quote(job_id, safe=""))
