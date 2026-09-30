import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from astrbot_plugin_bili_digest import backends, net
from astrbot_plugin_bili_digest.config import settings
from astrbot_plugin_bili_digest.process import DigestError

RealClient = httpx.AsyncClient


class HttpTests(unittest.IsolatedAsyncioTestCase):
    def client_patch(self, handler):
        transport = httpx.MockTransport(handler)
        return patch(
            "httpx.AsyncClient", side_effect=lambda **kw: RealClient(transport=transport, **kw)
        )

    async def test_private_dns_rejected(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]):
            with self.assertRaises(DigestError):
                await net.public_url("http://www.bilibili.com", ["bilibili.com"])

    async def test_bounded_response(self):
        with (
            self.client_patch(lambda request: httpx.Response(200, content=b"abcdef")),
            patch.object(net, "public_url", AsyncMock()),
        ):
            with self.assertRaises(DigestError):
                await net.fetch("https://example.com", limit=3)

    async def test_auth_never_crosses_redirect_origin(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://evil.example"})

        with self.client_patch(handler), patch.object(net, "public_url", AsyncMock()):
            with self.assertRaises(DigestError):
                await net.fetch("https://example.com", headers={"Authorization": "Bearer secret"})
        self.assertEqual(len(requests), 1)

    async def test_bibigpt_contract(self):
        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer token")
            self.assertEqual(request.url.params["includeDetail"], "true")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "summary": "summary",
                    "id": "id",
                    "detail": {"title": "title"},
                },
            )

        with self.client_patch(handler), patch.object(net, "public_url", AsyncMock()):
            result = await backends.bibigpt(
                "https://b23.tv/abc", settings({"bibigpt_api_key": "token"})
            )
        self.assertEqual(result["metadata"]["title"], "title")
        self.assertNotIn("token", json.dumps(result))

    async def test_bibigpt_empty_is_failure(self):
        with (
            self.client_patch(lambda r: httpx.Response(200, json={"success": True, "summary": ""})),
            patch.object(net, "public_url", AsyncMock()),
        ):
            with self.assertRaises(DigestError):
                await backends.bibigpt("https://b23.tv/abc", settings({"bibigpt_api_key": "token"}))

    async def test_gemini_upload_generate_delete(self):
        requests = []

        def handler(request):
            requests.append((request.method, request.url.path))
            if request.url.path == "/upload/v1beta/files":
                return httpx.Response(
                    200,
                    headers={
                        "X-Goog-Upload-URL": "https://generativelanguage.googleapis.com/upload-session"
                    },
                )
            if request.url.path == "/upload-session":
                return httpx.Response(
                    200,
                    json={
                        "file": {
                            "name": "files/test",
                            "uri": "https://generativelanguage.googleapis.com/v1beta/files/test",
                            "mimeType": "video/mp4",
                            "state": "ACTIVE",
                        }
                    },
                )
            if request.url.path.endswith(":generateContent"):
                payload = json.loads(request.content)
                self.assertIn("file_data", payload["contents"][0]["parts"][0])
                return httpx.Response(
                    200, json={"candidates": [{"content": {"parts": [{"text": "video summary"}]}}]}
                )
            if request.method == "DELETE":
                return httpx.Response(200, json={})
            raise AssertionError(request.url)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.mp4"
            path.write_bytes(b"test-video")
            with self.client_patch(handler):
                result = await backends.gemini(
                    path, settings({"gemini_api_key": "token", "gemini_model": "test-model"})
                )
        self.assertEqual(result, "video summary")
        self.assertEqual(requests[-1], ("DELETE", "/v1beta/files/test"))

    async def test_gemini_delete_on_model_failure(self):
        deleted = []

        def handler(request):
            if request.url.path == "/upload/v1beta/files":
                return httpx.Response(
                    200,
                    headers={
                        "X-Goog-Upload-URL": "https://generativelanguage.googleapis.com/upload-session"
                    },
                )
            if request.url.path == "/upload-session":
                return httpx.Response(
                    200, json={"file": {"name": "files/test", "uri": "uri", "state": "ACTIVE"}}
                )
            if request.method == "DELETE":
                deleted.append(True)
                return httpx.Response(200)
            return httpx.Response(500)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.mp4"
            path.write_bytes(b"test")
            with self.client_patch(handler):
                with self.assertRaises(httpx.HTTPStatusError):
                    await backends.gemini(
                        path, settings({"gemini_api_key": "token", "gemini_model": "test"})
                    )
        self.assertTrue(deleted)
