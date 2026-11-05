"""插件层集成测试：指令 → 取图 → 反查（打桩）→ 回话。

用 tests/stub_astrbot.py 顶替 AstrBot，所以不联网、不需要装 AstrBot 也能跑，
验证的是「接得住的这一层」：指令参数解析、图片提取、配额/冷却、回话排版与错误兜底。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stub_astrbot  # noqa: E402

stub_astrbot.install()

import astrbot.api.message_components as Comp  # noqa: E402


def _load_plugin_module():
    """把插件目录当成包加载，这样 main.py 里的相对导入才能工作。"""
    pkg_name = "fish_plugin_pkg"
    if pkg_name not in sys.modules:
        pkg = type(sys)(pkg_name)
        pkg.__path__ = [str(PLUGIN_DIR)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
        importlib.import_module(f"{pkg_name}.core")
    return importlib.import_module(f"{pkg_name}.main")


main_mod = _load_plugin_module()


def _png_bytes() -> bytes:
    # 1x1 PNG，够 sniff_mime 认出类型
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )


class PluginFlowTest(unittest.TestCase):
    def setUp(self):
        # 测试机可能带着 HTTP_PROXY 环境变量（CI/开发机常见），会污染「直连」断言
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                      "ALL_PROXY", "all_proxy")
        }
        self.tmp = Path(tempfile.mkdtemp(prefix="fish_flow_"))
        self.image_path = self.tmp / "query.png"
        self.image_path.write_bytes(_png_bytes())
        config = {
            "enabled": True,
            "engines": ["lens"],
            "proxy": "",
            "use_astrbot_proxy": False,
            "cooldown_sec": 0,
            "notify_start": True,
            "send_on_miss": True,
            "cache_days": 0,
        }
        self.plugin = main_mod.FishFindChicken(stub_astrbot.Context({}), config)
        self.plugin._store = main_mod.StateStore(self.tmp / "state.json")
        asyncio.run(self.plugin._store.load())

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value

    def _event(self, text="/鱼鱼找鸡", messages=None, admin=False):
        return stub_astrbot.AstrMessageEvent(
            message_str=text, messages=messages or [], admin=admin
        )

    def _collect(self, handler, event):
        async def run():
            out = []
            async for result in handler(event):
                out.append(result)
            return out

        return asyncio.run(run())

    @staticmethod
    def _texts(results):
        return [c.text for r in results for c in r.chain if isinstance(c, Comp.Plain)]

    def test_help(self):
        results = self._collect(self.plugin.fish_find_chicken, self._event("/鱼鱼找鸡 帮助"))
        self.assertIn("鱼鱼找鸡 · 用法", self._texts(results)[0])

    def test_disabled(self):
        self.plugin.config["enabled"] = False
        results = self._collect(self.plugin.fish_find_chicken, self._event())
        self.assertIn("现在是关的", self._texts(results)[0])

    def test_no_image(self):
        results = self._collect(self.plugin.fish_find_chicken, self._event())
        self.assertIn("要引用一张图", self._texts(results)[0])

    def test_image_in_same_message(self):
        """图跟指令在同一条消息里：走通并给出结果文案。"""
        canned = {
            "accounts": [
                {
                    "platform": "twitter",
                    "handle": "@izayoill",
                    "url": "https://x.com/izayoill",
                    "authorName": "阿薰kaOri",
                    "confidence": "direct",
                    "evidence": {"engine": "lens", "similarity": None, "sourceUrl": ""},
                }
            ],
            "candidates": [{"sourceUrl": "https://x.com/izayoill", "title": "x"}],
            "text": "🎯 **鱼鱼找鸡结果**\n\n🌐 平台：Twitter",
            "enginesUsed": ["lens"],
            "failures": [],
            "cached": False,
        }
        calls = {}

        async def fake_search(data, mime, settings):
            calls["mime"] = mime
            calls["size"] = len(data)
            return canned

        self.plugin._search = fake_search
        event = self._event(messages=[Comp.Image(path=str(self.image_path))])
        texts = self._texts(self._collect(self.plugin.fish_find_chicken, event))
        self.assertEqual(texts[0], main_mod.WELCOME)
        self.assertIn("鱼鱼找鸡结果", texts[1])
        self.assertNotIn("**", texts[1])  # QQ 不认 markdown 加粗
        self.assertEqual(calls["mime"], "image/png")

    def test_miss_but_send_on_miss_off_is_silent(self):
        self.plugin.config["send_on_miss"] = False
        self.plugin.config["notify_start"] = False

        async def fake_search(data, mime, settings):
            return {"accounts": [], "candidates": [], "text": "❌ 没查到账号。", "enginesUsed": ["lens"], "failures": []}

        self.plugin._search = fake_search
        event = self._event(messages=[Comp.Image(path=str(self.image_path))])
        self.assertEqual(self._texts(self._collect(self.plugin.fish_find_chicken, event)), [])

    def test_cooldown_blocks_second_call(self):
        self.plugin.config["cooldown_sec"] = 30

        async def fake_search(data, mime, settings):
            return {"accounts": [], "candidates": [], "text": "❌ 没查到账号。", "enginesUsed": ["lens"], "failures": []}

        self.plugin._search = fake_search
        event = self._event(messages=[Comp.Image(path=str(self.image_path))])
        self._collect(self.plugin.fish_find_chicken, event)
        again = self._collect(self.plugin.fish_find_chicken, self._event(
            messages=[Comp.Image(path=str(self.image_path))]))
        self.assertIn("秒后再发", self._texts(again)[0])

    def test_quoted_image_via_reply_chain(self):
        canned = {
            "accounts": [],
            "candidates": [],
            "text": "❌ 没查到账号。a",
            "enginesUsed": ["lens"],
            "failures": [],
        }

        async def fake_search(data, mime, settings):
            return canned

        self.plugin._search = fake_search
        reply = Comp.Reply(id=123, chain=[Comp.Image(path=str(self.image_path))])
        texts = self._texts(self._collect(self.plugin.fish_find_chicken, self._event(messages=[reply])))
        self.assertIn("没查到账号", texts[-1])

    def test_search_exception_becomes_text(self):
        async def boom(data, mime, settings):
            raise RuntimeError("谷歌连不上")

        self.plugin._search = boom
        event = self._event(messages=[Comp.Image(path=str(self.image_path))])
        texts = self._texts(self._collect(self.plugin.fish_find_chicken, event))
        self.assertIn("反查出错了：谷歌连不上", texts[-1])

    def test_selfcheck_admin_only(self):
        texts = self._texts(self._collect(self.plugin.fish_selftest, self._event("/找鸡自检")))
        self.assertIn("只有管理员能用", texts[0])

    def test_selfcheck_reports_state(self):
        texts = self._texts(
            self._collect(self.plugin.fish_selftest, self._event("/找鸡自检", admin=True))
        )
        body = texts[0]
        self.assertIn("引擎：lens", body)
        self.assertIn("代理：直连", body)

    def test_initialize_and_terminate(self):
        """插件生命周期：加载自检不能抛，卸载要能收掉临时文件与浏览器。"""
        asyncio.run(self.plugin.initialize())
        self.assertIsNotNone(self.plugin._store)
        asyncio.run(self.plugin.terminate())
        self.assertEqual(self.plugin._temp_dirs, [])

    def test_split_all_arg(self):
        split = self.plugin._split_all_arg
        self.assertEqual(split("全部"), ("", True))
        self.assertEqual(split("全部 https://a.com/x.png"), ("https://a.com/x.png", True))
        self.assertEqual(split("all"), ("", True))
        self.assertEqual(split("全部的反面"), ("全部的反面", False))
        self.assertEqual(split("https://a.com/x.png"), ("https://a.com/x.png", False))
        self.assertEqual(split(""), ("", False))

    def test_command_arg_parsing(self):
        for raw, expected in (
            ("/鱼鱼找鸡 帮助", "帮助"),
            ("鱼鱼找鸡 https://a.com/x.png", "https://a.com/x.png"),
            ("/找鸡", ""),
            ("随便一句话", "随便一句话"),
        ):
            self.assertEqual(self.plugin._command_arg(self._event(raw)), expected)

    def test_group_blacklist(self):
        self.plugin.config["group_blacklist"] = ["888"]
        event = self._event(messages=[Comp.Image(path=str(self.image_path))])
        event._group_id = "888"
        texts = self._texts(self._collect(self.plugin.fish_find_chicken, event))
        self.assertIn("这个群没开", texts[0])


if __name__ == "__main__":
    unittest.main()
