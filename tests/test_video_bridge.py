"""Unit tests for the guarded FreeBrain video bridge (no network, no paid services)."""
import json
import os
import unittest
from unittest.mock import patch

from integrations import video_bridge


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.payload[:limit]


class VideoBridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "FREEBRAIN_VIDEO_ACCESS_TOKEN": "local-test-secret",
            "FREEBRAIN_VIDEO_STUDIO_TOKEN": "tenant-test-secret",
            "FREEBRAIN_VIDEO_STUDIO_URL": "http://127.0.0.1:8000",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_auth_requires_configured_token_and_constant_time_match(self):
        video_bridge.authorize("local-test-secret")
        with self.assertRaises(video_bridge.VideoBridgeError) as caught:
            video_bridge.authorize("wrong")
        self.assertEqual(caught.exception.status, 401)

    def test_submit_requires_idempotency_key_before_network(self):
        with patch("integrations.video_bridge.urllib.request.urlopen") as urlopen:
            with self.assertRaises(video_bridge.VideoBridgeError) as caught:
                video_bridge.submit_video({"prompt": "tower collapse"}, "")
        self.assertEqual(caught.exception.status, 400)
        urlopen.assert_not_called()

    def test_submit_posts_one_durable_job_and_returns_correlation_digest(self):
        with patch("integrations.video_bridge.urllib.request.urlopen", return_value=Response({
            "job_id": "job_123", "status": "queued"
        })) as urlopen:
            result = video_bridge.submit_video(
                {"prompt": "photoreal futuristic tower collapse", "provider": "video-cascade"},
                "request-unique-1",
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8000/v1/videos/generate")
        self.assertEqual(request.get_header("Authorization"), "Bearer tenant-test-secret")
        self.assertEqual(request.get_header("X-idempotency-key"), "request-unique-1")
        self.assertEqual(json.loads(request.data)["provider"], "video-cascade")
        self.assertEqual(result["job_id"], "job_123")
        self.assertEqual(result["freebrain_handoff"]["schema"], "freebrain-video-handoff-v1")
        self.assertEqual(len(result["freebrain_handoff"]["request_sha256"]), 64)

    def test_submit_refuses_missing_job_id(self):
        with patch("integrations.video_bridge.urllib.request.urlopen", return_value=Response({"status": "queued"})):
            with self.assertRaises(video_bridge.VideoBridgeError) as caught:
                video_bridge.submit_video({"prompt": "tower"}, "stable-key")
        self.assertEqual(caught.exception.status, 502)

    def test_job_id_rejects_path_traversal(self):
        with self.assertRaises(video_bridge.VideoBridgeError) as caught:
            video_bridge.get_video_job("../other")
        self.assertEqual(caught.exception.status, 400)

    def test_studio_url_rejects_non_local_http_and_non_https(self):
        os.environ["FREEBRAIN_VIDEO_STUDIO_URL"] = "http://evil.example"
        with self.assertRaises(video_bridge.VideoBridgeError) as caught:
            video_bridge.submit_video({"prompt": "tower"}, "stable-key")
        self.assertEqual(caught.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
