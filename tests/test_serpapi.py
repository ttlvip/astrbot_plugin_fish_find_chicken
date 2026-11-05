"""SerpApi 通道单测：解析、压缩、配置开关、以及「什么时候真的走 SerpApi」。

真实调用要 key，这里全部离线：用官方文档里的响应结构做 fixture。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Settings  # noqa: E402
from core.engines import reverse_search  # noqa: E402
from core.serpapi import (  # noqa: E402
    IMAGE_LIMIT_BYTES,
    SerpApiError,
    parse_search_payload,
    shrink_to_limit,
)

# 结构取自官方文档示例（visual_matches 的核心字段）
PAYLOAD = {
    "search_metadata": {"status": "Success", "google_lens_url": "https://lens.google.com/x"},
    "visual_matches": [
        {
            "position": 1,
            "title": "Ria-chan's Yuuka cosplay is amazing",
            "link": "https://x.com/H3NCHAN4LIFE/status/2116519252991889677",
            "source": "X",
            "thumbnail": "https://encrypted-tbn0.gstatic.com/images?q=tbn:1",
        },
        {
            "position": 2,
            "title": "阿薰kaOri 尤诺 正片",
            "link": "https://weibo.com/u/1234567890",
            "source": "微博",
        },
        {
            # 同一条被重复给出（真实响应里会有），要能去重
            "position": 3,
            "title": "重复",
            "link": "https://x.com/H3NCHAN4LIFE/status/2116519252991889677",
            "source": "X",
        },
        {
            # 谷歌中转链接，要能解出真实地址
            "position": 4,
            "title": "中转",
            "link": "https://www.google.com/url?q=https%3A%2F%2Fspace.bilibili.com%2F88888&sa=D",
            "source": "bilibili",
        },
        {"position": 5, "title": "没有链接的", "source": "某站"},
    ],
    "organic_results": [
        {"title": "文章", "link": "https://example.com/a", "source": "example"}
    ],
}

ORGANIC_ONLY = {
    "visual_matches": [],
    "organic_results": [
        {"title": "相关内容", "link": "https://example.com/post", "source": "Example"},
        {"title": "没有链接", "source": "X"},
    ],
}


class ParsePayloadTest(unittest.TestCase):
    def test_visual_matches_mapped(self):
        rows = parse_search_payload(PAYLOAD)
        urls = [r["sourceUrl"] for r in rows]
        self.assertEqual(rows[0]["host"], "x.com")
        self.assertEqual(rows[0]["title"], "Ria-chan's Yuuka cosplay is amazing")
        self.assertEqual(rows[0]["siteName"], "X")
        self.assertEqual(rows[0]["engine"], "lens")
        # 去重：同一个 URL 只出一条
        self.assertEqual(urls.count("https://x.com/H3NCHAN4LIFE/status/2116519252991889677"), 1)
        # 没有 link 的条目被丢掉
        self.assertEqual(len(rows), 3)
        # 谷歌中转被解开
        self.assertIn("https://space.bilibili.com/88888", urls)

    def test_organic_fallback_is_weak(self):
        rows = parse_search_payload(ORGANIC_ONLY)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get("weak"))

    def test_visual_preferred_over_organic(self):
        rows = parse_search_payload(PAYLOAD)
        self.assertFalse(any(r.get("weak") for r in rows))

    def test_garbage_input(self):
        self.assertEqual(parse_search_payload({}), [])
        self.assertEqual(parse_search_payload({"visual_matches": "x"}), [])


class ShrinkTest(unittest.TestCase):
    def _bmp(self, size=(1200, 1200)) -> bytes:
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", size, (30, 120, 200)).save(buffer, "BMP")
        return buffer.getvalue()

    def test_small_image_passthrough(self):
        data = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        out, mime = shrink_to_limit(data, "image/png")
        self.assertEqual(out, data)
        self.assertEqual(mime, "image/png")

    def test_big_image_compressed_under_limit(self):
        raw = self._bmp()
        self.assertGreater(len(raw), IMAGE_LIMIT_BYTES)
        out, mime = shrink_to_limit(raw, "image/bmp")
        self.assertLessEqual(len(out), IMAGE_LIMIT_BYTES)
        self.assertEqual(mime, "image/jpeg")
        # 压完还得是能打开的图
        from PIL import Image

        self.assertEqual(Image.open(BytesIO(out)).format, "JPEG")

    def test_corrupt_image_raises(self):
        # 必须超过 500KB 才会进入压缩分支（小图是原样放行的）
        with self.assertRaises(SerpApiError):
            shrink_to_limit(b"not an image" * 100_000, "image/jpeg")


class SettingsTest(unittest.TestCase):
    def test_ready_requires_both_switch_and_key(self):
        self.assertFalse(Settings.from_config({}).serpapi_ready)
        self.assertFalse(Settings.from_config({"serpapi_enabled": True}).serpapi_ready)
        self.assertFalse(
            Settings.from_config({"serpapi_enabled": False, "serpapi_api_key": "k"}).serpapi_ready
        )
        self.assertTrue(
            Settings.from_config({"serpapi_enabled": True, "serpapi_api_key": "k"}).serpapi_ready
        )

    def test_key_is_trimmed(self):
        settings = Settings.from_config(
            {"serpapi_enabled": True, "serpapi_api_key": "  abc  "}
        )
        self.assertEqual(settings.serpapi_api_key, "abc")


class RoutingTest(unittest.TestCase):
    """开关决定走谁：配好就走 SerpApi，没配就走本地浏览器。"""

    def _run(self, settings):
        return asyncio.run(
            reverse_search(b"img", "image/jpeg", settings=settings, on_log=None)
        )

    def test_serpapi_path_does_not_touch_browser(self):
        settings = Settings.from_config(
            {"engines": ["lens"], "serpapi_enabled": True, "serpapi_api_key": "k"}
        )
        fake = [
            {
                "engine": "lens",
                "title": "t",
                "sourceUrl": "https://x.com/a_user",
                "host": "x.com",
                "links": ["https://x.com/a_user"],
            }
        ]
        with patch(
            "core.serpapi.serpapi_lens_search", AsyncMock(return_value=fake)
        ), patch("core.lens.browser_available", side_effect=AssertionError("不该碰浏览器")):
            result = self._run(settings)
        self.assertEqual(result["enginesUsed"], ["lens"])
        self.assertEqual(len(result["candidates"]), 1)

    def test_falls_back_to_browser_when_key_missing(self):
        settings = Settings.from_config(
            {"engines": ["lens"], "serpapi_enabled": True, "serpapi_api_key": ""}
        )
        with patch("core.lens.browser_available", return_value=False):
            result = self._run(settings)
        self.assertIn("没找到 Chrome/Edge", str(result["failures"]))
        self.assertEqual(result["enginesUsed"], [])

    def test_serpapi_error_is_reported(self):
        settings = Settings.from_config(
            {"engines": ["lens"], "serpapi_enabled": True, "serpapi_api_key": "bad"}
        )
        with patch(
            "core.serpapi.serpapi_lens_search",
            AsyncMock(side_effect=SerpApiError("SerpApi 额度用完了（429）", code="QUOTA")),
        ):
            result = self._run(settings)
        self.assertIn("额度用完", str(result["failures"]))
        self.assertEqual(result["enginesUsed"], [])


if __name__ == "__main__":
    unittest.main()
