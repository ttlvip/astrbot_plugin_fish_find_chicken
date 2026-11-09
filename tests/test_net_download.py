"""图片下载：重定向 SSRF 与分块限流。"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.net import HttpError, download_image  # noqa: E402


class _FakeContent:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeResp:
    def __init__(self, *, status=200, headers=None, chunks=None, url="https://cdn.example/a.jpg"):
        self.status = status
        self.headers = headers or {"Content-Type": "image/jpeg"}
        self.content = _FakeContent(chunks or [b"\xff\xd8\xff" + b"x" * 10])
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.closed = False

    def request(self, method, url, **kwargs):
        self.last_url = url
        self.last_kwargs = kwargs
        return self._responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class DownloadImageTest(unittest.TestCase):
    def test_redirect_to_private_is_rejected(self):
        public = _FakeResp(
            status=302,
            headers={"Location": "http://127.0.0.1/secret.jpg"},
            url="https://cdn.example/a.jpg",
        )

        async def run():
            session = _FakeSession([public])
            with patch("core.net._session", return_value=session):
                with patch("core.net.ensure_public_host", new=AsyncMock()) as ensure:
                    async def side_effect(url):
                        if "127.0.0.1" in url:
                            raise HttpError("不接受内网地址作为图片网址")

                    ensure.side_effect = side_effect
                    await download_image("https://cdn.example/a.jpg", allow_private=False)

        with self.assertRaises(HttpError) as ctx:
            asyncio.run(run())
        self.assertIn("内网", str(ctx.exception))

    def test_oversized_stream_aborts(self):
        big = _FakeResp(chunks=[b"\xff\xd8\xff" + b"y" * 100])

        async def run():
            session = _FakeSession([big])
            with patch("core.net._session", return_value=session):
                with patch("core.net.ensure_public_host", new=AsyncMock()):
                    await download_image(
                        "https://cdn.example/a.jpg", max_bytes=20, allow_private=True
                    )

        with self.assertRaises(HttpError) as ctx:
            asyncio.run(run())
        self.assertIn("太大", str(ctx.exception))

    def test_manual_redirect_disables_auto_follow(self):
        ok = _FakeResp(chunks=[b"\xff\xd8\xff" + b"ok"])
        redirect = _FakeResp(
            status=302,
            headers={"Location": "https://cdn.example/b.jpg"},
            url="https://cdn.example/a.jpg",
        )

        async def run():
            session = _FakeSession([redirect, ok])
            with patch("core.net._session", return_value=session):
                with patch("core.net.ensure_public_host", new=AsyncMock()):
                    data, mime = await download_image(
                        "https://cdn.example/a.jpg", allow_private=True
                    )
            self.assertEqual(mime, "image/jpeg")
            self.assertTrue(data.startswith(b"\xff\xd8\xff"))
            self.assertFalse(session.last_kwargs.get("allow_redirects", True))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
