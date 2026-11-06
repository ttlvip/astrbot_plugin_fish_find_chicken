"""离线单测：配置解析、网络层小工具、状态（配额/缓存）。"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Settings  # noqa: E402
from core.net import (  # noqa: E402
    HttpError,
    ensure_public_host,
    normalize_proxy,
    proxy_host,
    sniff_mime,
)
from core.state import StateStore, day_key  # noqa: E402


class ProxyTest(unittest.TestCase):
    def test_normalize_adds_scheme(self):
        self.assertEqual(normalize_proxy("127.0.0.1:7890"), "http://127.0.0.1:7890")

    def test_normalize_rejects_socks(self):
        with self.assertRaises(HttpError):
            normalize_proxy("socks5://127.0.0.1:1080")

    def test_mask_hides_credentials(self):
        masked = proxy_host("http://user:pass@10.0.0.2:7890")
        self.assertNotIn("pass", masked)
        self.assertIn("10.0.0.2:7890", masked)

    def test_empty_is_direct(self):
        self.assertEqual(normalize_proxy(""), "")
        self.assertEqual(proxy_host(""), "")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in
                       ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                        "ALL_PROXY", "all_proxy")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value

    def test_plugin_proxy_wins(self):
        settings = Settings.from_config(
            {"proxy": "http://a:1"}, {"http_proxy": "http://b:2"}
        )
        self.assertEqual(settings.proxy, "http://a:1")
        self.assertEqual(settings.proxy_source, "插件配置")

    def test_falls_back_to_astrbot_global(self):
        settings = Settings.from_config({}, {"http_proxy": "http://b:2"})
        self.assertEqual(settings.proxy, "http://b:2")
        self.assertIn("AstrBot", settings.proxy_source)

    def test_can_disable_global_proxy(self):
        settings = Settings.from_config(
            {"use_astrbot_proxy": False}, {"http_proxy": "http://b:2"}
        )
        self.assertEqual(settings.proxy, "")

    def test_engines_filtered_and_fallback(self):
        self.assertEqual(Settings.from_config({"engines": ["lens", "nope"]}).engines, ["lens"])
        self.assertTrue(Settings.from_config({"engines": []}).engines)  # 回落到默认

    def test_ints_clamped(self):
        settings = Settings.from_config({"timeout_sec": 1, "max_accounts": 0, "lens_max_resolve": 999})
        self.assertEqual(settings.timeout_sec, 5)
        self.assertEqual(settings.max_accounts, 1)
        self.assertEqual(settings.lens_max_resolve, 24)

    def test_bool_from_string(self):
        self.assertTrue(Settings.from_config({"enabled": "false"}).enabled is False)


class NetToolTest(unittest.TestCase):
    def test_sniff_mime(self):
        self.assertEqual(sniff_mime(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(sniff_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(sniff_mime(b"RIFF1234WEBPVP8"), "image/webp")
        self.assertEqual(sniff_mime(b"GIF89a"), "image/gif")
        self.assertEqual(sniff_mime(b"?????", "image/jpeg"), "image/jpeg")

    def test_private_hosts_rejected(self):
        loop = asyncio.new_event_loop()
        try:
            for url in (
                "http://127.0.0.1/a.png",
                "http://localhost/a.png",
                "http://10.1.2.3/a.png",
                "http://192.168.1.1/a.png",
                "http://[::1]/a.png",
                "http://169.254.169.254/latest/meta-data",
            ):
                with self.assertRaises(HttpError, msg=url):
                    loop.run_until_complete(ensure_public_host(url))
        finally:
            loop.close()


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fish_state_")
        self.path = Path(self.dir) / "state.json"
        self.store = StateStore(self.path)
        asyncio.run(self.store.load())

    def test_cooldown(self):
        ok, _ = self.store.quota_check(key="g|u", user_id="u", cooldown_sec=30, now=1000)
        self.assertTrue(ok)
        asyncio.run(self.store.charge(key="g|u", user_id="u", now=1000))
        ok, reason = self.store.quota_check(key="g|u", user_id="u", cooldown_sec=30, now=1010)
        self.assertFalse(ok)
        self.assertIn("秒后再发", reason)
        ok, _ = self.store.quota_check(key="g|u", user_id="u", cooldown_sec=30, now=1040)
        self.assertTrue(ok)

    def test_per_user_daily_limit(self):
        for i in range(2):
            asyncio.run(self.store.charge(key=f"g{i}", user_id="u", now=2000 + i))
        ok, reason = self.store.quota_check(
            key="g9", user_id="u", cooldown_sec=0, per_user_day=2, now=2005
        )
        self.assertFalse(ok)
        self.assertIn("今天的 2 次", reason)
        # 换一天就恢复
        ok, _ = self.store.quota_check(
            key="g9", user_id="u", cooldown_sec=0, per_user_day=2, now=2005 + 86400
        )
        self.assertTrue(ok)

    def test_admin_bypasses_quota(self):
        ok, _ = self.store.quota_check(
            key="g|u", user_id="u", is_admin=True, cooldown_sec=30, per_user_day=0, now=1
        )
        self.assertTrue(ok)

    def test_cache_roundtrip_and_expiry(self):
        asyncio.run(self.store.cache_put("k1", {"text": "hi"}))
        self.assertEqual(self.store.cache_get("k1", cache_days=30)["text"], "hi")
        self.assertIsNone(self.store.cache_get("k1", cache_days=0))
        # 迁移 40 天前的时间戳后应当过期
        self.store._data["cache"]["k1"]["at"] -= 40 * 86400
        self.assertIsNone(self.store.cache_get("k1", cache_days=30))

    def test_state_file_is_json(self):
        asyncio.run(self.store.charge(key="g|u", user_id="u", now=5000))
        import json

        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("day", data)
        self.assertEqual(data["day"]["user"]["u"]["count"], 1)

    def test_day_key_is_beijing(self):
        # 2026-01-01 00:30 北京时间 = 2025-12-31 16:30 UTC
        ts = 1767227400
        self.assertEqual(day_key(ts), "2026-01-01")


class LoggingRuleTest(unittest.TestCase):
    """审查硬性规定：日志必须且只能 `from astrbot.api import logger`。

    内置 logging 会生成绕过 AstrBot 日志体系的独立 logger（市场审核直接拒），
    所以这里做成回归测试 —— 以后谁再写 `import logging` 会当场挂掉。
    """

    ROOT = Path(__file__).resolve().parent.parent

    def test_no_builtin_logging_anywhere(self):
        pattern = re.compile(r"^\s*(?:import logging\b|from logging import\b)", re.M)
        offenders = [
            str(path.relative_to(self.ROOT))
            for path in self.ROOT.rglob("*.py")
            if "__pycache__" not in path.parts
            and pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_modules_take_logger_from_astrbot(self):
        for name in ("core/browser.py", "core/lens.py"):
            text = (self.ROOT / name).read_text(encoding="utf-8")
            self.assertIn("from astrbot.api import logger", text, name)
            self.assertNotIn("getLogger", text, name)

    def test_stub_logger_formats_braces(self):
        """AstrBot 的 logger 是 loguru，占位符是 `{}`，`%s` 会被原样打出来。"""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import stub_astrbot

        record = stub_astrbot._StubLogger()
        record.threshold = 0
        self.assertEqual(record._render("已注入 {} 个（{}）", (3, "x.com"), {}), "已注入 3 个（x.com）")
        # 占位符写错也不能抛（日志不许打崩业务）
        self.assertIn("已注入", record._render("已注入 %s", ("x",), {}))


if __name__ == "__main__":
    unittest.main()
