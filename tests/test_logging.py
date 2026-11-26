"""日志可见性回归测试。

背景：AstrBot 4.29.4+ 给每个插件分配独立 logger（默认跟随全局级别）。
全局是 INFO 时，插件里所有 DEBUG 记录根本不会生成 —— 之前引擎失败、
超时、浏览器错误全走 DEBUG，控制台就表现为「插件报错了但什么都没打印」。

这里锁死三条底线：
1. 引擎故障不能沉在 DEBUG（至少 WARNING/ERROR，INFO 模式下也可见）；
2. 任何模式下异常都有一行摘要；
3. DEBUG 模式（或 verbose_errors）下异常带完整 Traceback。

只用 tests/stub_astrbot.py，不联网、不需要装 AstrBot。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stub_astrbot  # noqa: E402

stub_astrbot.install()


def _load_plugin_module():
    """把插件目录当包加载，main.py 里的相对导入才能工作。"""
    pkg_name = "fish_plugin_pkg"
    if pkg_name not in sys.modules:
        pkg = type(sys)(pkg_name)
        pkg.__path__ = [str(PLUGIN_DIR)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
        importlib.import_module(f"{pkg_name}.core")
    return importlib.import_module(f"{pkg_name}.main")


main_mod = _load_plugin_module()

from core import engines as engines_mod  # noqa: E402
from core.config import Settings  # noqa: E402


def _raised(message: str) -> RuntimeError:
    """造一个真正抛出过的异常（带 __traceback__），模拟真实故障现场。

    直接 `RuntimeError("boom")` 是没有 traceback 的，format_exception 只会输出
    「RuntimeError: boom」；线上异常都是被抛出来的，测试必须按线上形态来。
    """
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


class LogVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.saved_threshold = stub_astrbot.logger.threshold
        stub_astrbot.logger.records.clear()
        # 模拟 AstrBot 全局 INFO：插件专属 logger 的有效级别 = 20
        stub_astrbot.logger.threshold = 20
        self.info_ctx = stub_astrbot.Context({"http_proxy": "", "log_level": "INFO"})
        self.debug_ctx = stub_astrbot.Context({"http_proxy": "", "log_level": "DEBUG"})

    def tearDown(self):
        stub_astrbot.logger.threshold = self.saved_threshold
        stub_astrbot.logger.records.clear()

    def _text(self) -> str:
        return "\n".join(text for _, text in stub_astrbot.logger.records)

    def _levels(self) -> list[str]:
        return [level for level, _ in stub_astrbot.logger.records]

    # ── 普通（INFO）模式：至少看得见 ──────────────────────────────────
    def test_engine_failure_is_warning_in_info_mode(self):
        plugin = main_mod.FishFindChicken(self.info_ctx, {})
        exc = RuntimeError("代理连不上")
        plugin._on_engine_log("[鱼鱼找鸡] lens 失败：代理连不上", "WARNING", exc)

        self.assertIn("WARNING", self._levels())
        self.assertIn("代理连不上", self._text())
        self.assertNotIn("Traceback", self._text())

    def test_exception_always_has_error_summary(self):
        plugin = main_mod.FishFindChicken(self.info_ctx, {})
        plugin._log_exception("[鱼鱼找鸡] 反查失败", _raised("boom"))

        self.assertIn("ERROR", self._levels())
        self.assertIn("RuntimeError: boom", self._text())
        self.assertNotIn("Traceback", self._text())

    def test_verbose_errors_forces_stacktrace(self):
        plugin = main_mod.FishFindChicken(self.info_ctx, {"verbose_errors": True})
        plugin._log_exception("[鱼鱼找鸡] 反查失败", _raised("boom"))

        self.assertIn("Traceback (most recent call last)", self._text())
        self.assertIn("RuntimeError: boom", self._text())

    # ── DEBUG 模式：完整堆栈 ──────────────────────────────────────────
    def test_debug_mode_adds_full_traceback(self):
        stub_astrbot.logger.threshold = 10  # 10 = DEBUG
        plugin = main_mod.FishFindChicken(self.debug_ctx, {})
        self.assertTrue(plugin._debug_enabled())
        plugin._log_exception("[鱼鱼找鸡] 反查失败", _raised("boom"))

        self.assertIn("ERROR", self._levels())
        self.assertIn("Traceback (most recent call last)", self._text())
        self.assertIn("RuntimeError: boom", self._text())

    def test_debug_mode_expands_engine_exception(self):
        stub_astrbot.logger.threshold = 10
        plugin = main_mod.FishFindChicken(self.debug_ctx, {})
        plugin._on_engine_log(
            "[鱼鱼找鸡] saucenao 失败：限流", "WARNING", _raised("429")
        )

        self.assertIn("WARNING", self._levels())
        self.assertIn("Traceback (most recent call last)", self._text())
        self.assertIn("RuntimeError: 429", self._text())

    # ── 引擎层：故障不能再全走 DEBUG ─────────────────────────────────
    def test_reverse_search_reports_failure_as_error_with_exc(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("engine down")

        original = engines_mod._NETWORK_RUNNERS.get("saucenao")
        engines_mod._NETWORK_RUNNERS["saucenao"] = boom
        seen: list[tuple[str, str, object]] = []
        settings = Settings.from_config({"engines": ["saucenao"], "timeout_sec": 5})
        try:
            result = asyncio.run(
                engines_mod.reverse_search(
                    b"img",
                    "image/jpeg",
                    settings=settings,
                    on_log=lambda line, level="DEBUG", exc=None: seen.append(
                        (level, line, exc)
                    ),
                )
            )
        finally:
            if original is not None:
                engines_mod._NETWORK_RUNNERS["saucenao"] = original

        self.assertTrue(any(level == "ERROR" for level, _, _ in seen), seen)
        self.assertTrue(any(exc is not None for _, _, exc in seen), seen)
        self.assertEqual(result["failures"][0]["reason"], "engine down")

    def test_legacy_single_arg_callback_still_works(self):
        """旧式 on_log(line) 不能被破坏（lens/serpapi 内部就是这么调的）。"""
        async def boom(*_args, **_kwargs):
            raise RuntimeError("engine down")

        original = engines_mod._NETWORK_RUNNERS.get("saucenao")
        engines_mod._NETWORK_RUNNERS["saucenao"] = boom
        seen: list[str] = []
        settings = Settings.from_config({"engines": ["saucenao"], "timeout_sec": 5})
        try:
            asyncio.run(
                engines_mod.reverse_search(
                    b"img", "image/jpeg", settings=settings, on_log=seen.append
                )
            )
        finally:
            if original is not None:
                engines_mod._NETWORK_RUNNERS["saucenao"] = original
        self.assertTrue(seen)

    # ── 配置 ──────────────────────────────────────────────────────────
    def test_verbose_errors_defaults_off(self):
        self.assertFalse(Settings.from_config({}).verbose_errors)
        self.assertTrue(Settings.from_config({"verbose_errors": "true"}).verbose_errors)

    def test_global_log_level_is_read(self):
        plugin = main_mod.FishFindChicken(self.debug_ctx, {})
        self.assertEqual(plugin._global_config().get("log_level"), "DEBUG")


if __name__ == "__main__":
    unittest.main()
