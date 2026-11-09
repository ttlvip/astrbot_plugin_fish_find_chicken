"""外部浏览器（独立 Chrome 容器）相关单测：地址归一化 + 配置解析。

真实连接由 `.dev/test_remote_cdp.py` 做（要起一个浏览器），这里只测纯函数，
保证用户各种写法都能被正确理解、配错了也能给出可读的错误。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# core.browser 直接 `from astrbot.api import logger`（审查要求：不许用内置 logging），
# 所以离线跑单测前必须先把 AstrBot 桩装进 sys.modules。
import stub_astrbot  # noqa: E402

stub_astrbot.install()

from core.browser import BrowserError, normalize_cdp_base  # noqa: E402
from core.config import Settings  # noqa: E402


class NormalizeCdpBaseTest(unittest.TestCase):
    def test_common_forms(self):
        for raw in (
            "http://chrome:9222",
            "http://chrome:9222/",
            "chrome:9222",
            "  http://chrome:9222  ",
            "http://chrome:9222/json/version",
            "http://chrome:9222/json/list",
        ):
            self.assertEqual(normalize_cdp_base(raw)[0], "http://chrome:9222", raw)

    def test_ws_url_is_accepted(self):
        """有人会把 /json/version 里的 webSocketDebuggerUrl 整条贴进来。"""
        base, host, port = normalize_cdp_base("ws://chrome:9222/devtools/browser/abc-123")
        self.assertEqual(base, "http://chrome:9222")
        self.assertEqual(host, "chrome")
        self.assertEqual(port, 9222)

    def test_default_port(self):
        self.assertEqual(normalize_cdp_base("http://chrome")[2], 9222)

    def test_ip_address(self):
        base, host, port = normalize_cdp_base("http://192.168.1.10:9333")
        self.assertEqual((base, host, port), ("http://192.168.1.10:9333", "192.168.1.10", 9333))

    def test_https_is_kept(self):
        self.assertEqual(normalize_cdp_base("https://chrome.example.com:9222")[0],
                         "https://chrome.example.com:9222")

    def test_rejects_empty_and_bad_scheme(self):
        with self.assertRaises(BrowserError):
            normalize_cdp_base("")
        with self.assertRaises(BrowserError):
            normalize_cdp_base("ftp://chrome:9222")
        with self.assertRaises(BrowserError):
            normalize_cdp_base("http://")


class SettingsCdpTest(unittest.TestCase):
    def test_read_and_trim(self):
        settings = Settings.from_config({"browser_cdp": "  http://chrome:9222  "})
        self.assertEqual(settings.browser_cdp, "http://chrome:9222")

    def test_default_empty_means_local(self):
        self.assertEqual(Settings.from_config({}).browser_cdp, "")

    def test_cdp_alone_does_not_require_local_exe(self):
        """填了 cdp 就不该因为本地没有浏览器而被判定不可用。"""
        from core.lens import browser_available

        self.assertTrue(browser_available("", "http://chrome:9222"))

    def test_no_browser_anywhere_is_unavailable(self):
        """没配 cdp、探测也找不到浏览器时，必须老实说不可用（不能假装能查）。"""
        from unittest.mock import patch

        from core.lens import browser_available

        with patch("core.lens.find_browser_exe", return_value=""):
            self.assertFalse(browser_available("", ""))

    def test_explicit_bad_path_still_counts_as_available(self):
        """用户明确填了路径就尊重它：路径写错要让启动阶段报真实错误，而不是在这里静默跳过。"""
        from core.lens import browser_available

        self.assertTrue(browser_available("/no/such/chrome", ""))


class PlaywrightDiscoveryTest(unittest.TestCase):
    """装在容器里的 Playwright chromium 不在 PATH 里，必须能自动找到。

    真实场景：AstrBot 镜像里没有系统浏览器，但用户跑过 `playwright install chromium`，
    真正的可执行文件在 ms-playwright 缓存目录下。找不到它，插件就只能报「没找到 Chrome/Edge」。
    """

    def setUp(self):
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="mspw_")) / "ms-playwright"
        self.root.mkdir(parents=True)

    def _make(self, version_dir: str, rel: str) -> Path:
        exe = self.root / version_dir / rel
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"")
        return exe

    def test_full_chromium_preferred_and_newest_first(self):
        from unittest.mock import patch

        from core.browser import playwright_browser_candidates

        old = self._make("chromium-1091", "chrome-linux/chrome")
        new = self._make("chromium-1200", "chrome-linux/chrome")
        shell = self._make(
            "chromium_headless_shell-1091", "chrome-linux/headless_shell"
        )
        with patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": str(self.root)}):
            found = playwright_browser_candidates()

        self.assertIn(str(new), found)
        self.assertIn(str(old), found)
        self.assertIn(str(shell), found)
        # 完整版排在 headless_shell 前面，完整版内部版本号大的在前
        self.assertEqual(found[0], str(new))
        self.assertEqual(found[1], str(old))
        self.assertEqual(found[-1], str(shell))

    def test_only_headless_shell_still_found(self):
        from unittest.mock import patch

        from core.browser import playwright_browser_candidates

        shell = self._make(
            "chromium_headless_shell-1100", "chrome-linux/headless_shell"
        )
        with patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": str(self.root)}):
            found = playwright_browser_candidates()
        self.assertEqual(found, [str(shell)])

    def test_windows_layout(self):
        from unittest.mock import patch

        from core.browser import playwright_browser_candidates

        exe = self._make("chromium-1200", "chrome-win/chrome.exe")
        with patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": str(self.root)}):
            found = playwright_browser_candidates()
        self.assertIn(str(exe), found)

    def test_new_layout_linux64(self):
        """issue #2：新版 Playwright 的内层目录叫 `chrome-linux64`，不是 `chrome-linux`。"""
        from unittest.mock import patch

        from core.browser import playwright_browser_candidates

        exe = self._make("chromium-1248", "chrome-linux64/chrome")
        with patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": str(self.root)}):
            found = playwright_browser_candidates()
        self.assertIn(str(exe), found)

    def test_new_layout_windows64_and_macos_arch(self):
        """新版 Windows 是 `chrome-win64`，macOS 带 arm64 / x64 后缀。"""
        from unittest.mock import patch

        from core.browser import playwright_browser_candidates

        win = self._make("chromium-1248", "chrome-win64/chrome.exe")
        mac_arm = self._make(
            "chromium-1250", "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium"
        )
        mac_x64 = self._make(
            "chromium-1250", "chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium"
        )
        with patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": str(self.root)}):
            found = playwright_browser_candidates()
        for exe in (win, mac_arm, mac_x64):
            self.assertIn(str(exe), found)

    def test_new_layout_headless_shell(self):
        """新版 headless shell 目录和文件名都变了，两个新名字都要认。"""
        from unittest.mock import patch

        from core.browser import playwright_browser_candidates

        shell = self._make(
            "chromium_headless_shell-1248",
            "chrome-headless-shell-linux64/chrome-headless-shell",
        )
        win_shell = self._make(
            "chromium_headless_shell-1248",
            "chrome-headless-shell-win64/chrome-headless-shell.exe",
        )
        with patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": str(self.root)}):
            found = playwright_browser_candidates()
        for exe in (shell, win_shell):
            self.assertIn(str(exe), found)

    def test_headless_shell_not_given_headless_flag(self):
        """headless_shell 天生无头，多传 --headless=new 会让它直接退出。"""
        from core.browser import _build_args, _is_headless_shell

        shell = "/ms-playwright/chromium_headless_shell-1091/chrome-linux/headless_shell"
        full = "/ms-playwright/chromium-1091/chrome-linux/chrome"
        self.assertTrue(_is_headless_shell(shell))
        self.assertFalse(_is_headless_shell(full))
        self.assertNotIn(
            "--headless=new",
            _build_args(shell, "", Path("."), headless=True),
        )
        self.assertIn("--headless=new", _build_args(full, "", Path("."), headless=True))


if __name__ == "__main__":
    unittest.main()
