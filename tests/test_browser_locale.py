"""出口地区对齐（browser_region）与伪装脚本的单测。

测的都是纯逻辑：国家码 → 时区/语言、auto 模式的三条分支（查到 / 缓存 / 查不到）、
启动参数里有没有 --lang、注入脚本里的 navigator.languages 与 plugins 形状。
真实浏览器行为由 `.dev/` 下的手测脚本覆盖 —— 这里不启动任何进程、不发任何请求。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# core.browser 直接 `from astrbot.api import logger`，离线跑单测前先装桩。
import stub_astrbot  # noqa: E402

stub_astrbot.install()

import core.browser as browser_mod  # noqa: E402
from core.browser import (  # noqa: E402
    DEFAULT_LOCALE,
    STEALTH_JS,
    STEALTH_LANGUAGE,
    locale_for_country,
    resolve_locale,
    stealth_js,
)
from core.config import Settings  # noqa: E402


class LocaleTest(unittest.TestCase):
    def test_default_keeps_legacy_accept_language(self):
        """不配 browser_region 时必须和 0.1.x 完全一致，否则等于偷偷改了老用户的行为。"""
        self.assertEqual(DEFAULT_LOCALE.accept_language, STEALTH_LANGUAGE)
        self.assertEqual(DEFAULT_LOCALE.accept_language, "zh-CN,zh;q=0.9,en;q=0.8")
        self.assertEqual(DEFAULT_LOCALE.languages, ("zh-CN", "zh", "en"))
        self.assertFalse(DEFAULT_LOCALE.active)
        self.assertEqual(DEFAULT_LOCALE.timezone, "")

    def test_country_profiles(self):
        us = locale_for_country("us")
        self.assertEqual(us.timezone, "America/Los_Angeles")
        self.assertEqual(us.languages, ("en-US", "en"))
        self.assertEqual(us.accept_language, "en-US,en;q=0.9")
        self.assertTrue(us.active)

        jp = locale_for_country("JP")
        self.assertEqual(jp.timezone, "Asia/Tokyo")
        self.assertEqual(jp.languages, ("ja-JP", "ja", "en"))
        self.assertEqual(jp.accept_language, "ja-JP,ja;q=0.9,en;q=0.8")

    def test_unknown_country_falls_back_to_english(self):
        loc = locale_for_country("ZZ")
        self.assertEqual(loc.timezone, "")
        self.assertEqual(loc.language, "en-US")

    def test_timezone_from_ip_wins_over_table(self):
        loc = locale_for_country("US", "America/New_York")
        self.assertEqual(loc.timezone, "America/New_York")
        self.assertEqual(loc.language, "en-US")


class StealthScriptTest(unittest.TestCase):
    def test_languages_are_injected(self):
        script = stealth_js(("ja-JP", "ja", "en"))
        self.assertIn('["ja-JP", "ja", "en"]', script)
        self.assertNotIn("__FFC_LANGUAGES__", script)

    def test_default_script_keeps_chinese(self):
        self.assertIn('"zh-CN"', STEALTH_JS)

    def test_plugin_array_is_not_a_bare_array(self):
        """item/namedItem/refresh + Symbol.iterator 缺一个都会让检测脚本看出破绽。"""
        for token in (
            "namedItem",
            "refresh",
            "Symbol.iterator",
            "pdfViewerEnabled",
            "'application/pdf'",
        ):
            self.assertIn(token, STEALTH_JS)


class LaunchArgsTest(unittest.TestCase):
    def test_lang_flags_follow_locale(self):
        args = browser_mod._build_args(
            "/usr/bin/chromium", "", Path("/tmp/profile"), True, locale_for_country("us")
        )
        self.assertIn("--lang=en-US", args)
        self.assertIn("--accept-lang=en-US,en;q=0.9", args)
        self.assertEqual(args[-1], "about:blank")

    def test_no_lang_flags_by_default(self):
        args = browser_mod._build_args(
            "/usr/bin/chromium", "", Path("/tmp/profile"), True, DEFAULT_LOCALE
        )
        self.assertFalse([item for item in args if item.startswith(("--lang=", "--accept-lang="))])

    def test_no_sandbox_only_when_bypass_needed(self):
        # 本机可能是 Windows；这里强制走 Linux 分支验证沙箱参数。
        with patch("core.browser.os.name", "posix"):
            with patch("core.browser._needs_chromium_sandbox_bypass", return_value=False):
                args = browser_mod._build_args(
                    "/usr/bin/chromium", "", Path("/tmp/profile"), True, DEFAULT_LOCALE
                )
            self.assertNotIn("--no-sandbox", args)
            self.assertIn("--disable-dev-shm-usage", args)

            with patch("core.browser._needs_chromium_sandbox_bypass", return_value=True):
                args = browser_mod._build_args(
                    "/usr/bin/chromium", "", Path("/tmp/profile"), True, DEFAULT_LOCALE
                )
            self.assertIn("--no-sandbox", args)
            self.assertIn("--disable-dev-shm-usage", args)


class GoogleAuthCookieTest(unittest.TestCase):
    def test_auth_cookie_names_detected(self):
        for name in ("SID", "SSID", "HSID", "__Secure-1PSID", "__Secure-1PSIDTS", "SAPISID"):
            self.assertTrue(browser_mod._is_google_auth_cookie(name), name)
        for name in ("NID", "CONSENT", "SOCS", "AEC"):
            self.assertFalse(browser_mod._is_google_auth_cookie(name), name)


class ResolveLocaleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        browser_mod._locale_cache.clear()

    async def test_off_returns_default_without_network(self):
        with patch(
            "core.browser._fetch_ip_info", side_effect=AssertionError("不该联网")
        ):
            for raw in ("", "   ", "off", "none"):
                self.assertIs(await resolve_locale(raw), DEFAULT_LOCALE)

    async def test_country_code_does_not_touch_network(self):
        with patch(
            "core.browser._fetch_ip_info", side_effect=AssertionError("不该联网")
        ):
            loc = await resolve_locale("jp")
        self.assertEqual(loc.timezone, "Asia/Tokyo")
        self.assertIn("配置", loc.source)

    async def test_auto_uses_ip_country_and_timezone(self):
        with patch(
            "core.browser._fetch_ip_info",
            new=AsyncMock(return_value={"country": "US", "timezone": "America/Chicago"}),
        ):
            loc = await resolve_locale("auto", "http://10.0.0.1:7890")
        self.assertEqual(loc.timezone, "America/Chicago")
        self.assertEqual(loc.language, "en-US")
        self.assertEqual(loc.source, "auto")

    async def test_auto_result_is_cached(self):
        probe = AsyncMock(return_value={"country": "JP", "timezone": "Asia/Tokyo"})
        with patch("core.browser._fetch_ip_info", new=probe):
            await resolve_locale("auto", "http://10.0.0.2:7890")
            await resolve_locale("auto", "http://10.0.0.2:7890")
        self.assertEqual(probe.await_count, 1)

    async def test_auto_failure_falls_back_to_default(self):
        with patch(
            "core.browser._fetch_ip_info", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            loc = await resolve_locale("auto", "http://10.0.0.3:7890")
        self.assertIs(loc, DEFAULT_LOCALE)


class SettingsRegionTest(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(Settings.from_config({}).browser_region, "")

    def test_value_is_stripped(self):
        self.assertEqual(
            Settings.from_config({"browser_region": "  auto "}).browser_region, "auto"
        )


if __name__ == "__main__":
    unittest.main()
