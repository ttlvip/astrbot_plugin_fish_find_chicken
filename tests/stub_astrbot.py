"""最小 AstrBot 桩：让插件层（main.py）能在没有装 AstrBot 的机器上跑测试。

只实现插件真正用到的那一小撮 API：Star / Context / StarTools / filter.command /
AstrMessageEvent 的若干方法 / message_components。(Image, Plain, Reply)

**不是** AstrBot 的替代品，只用于离线验证「指令能不能接住、图能不能取到、回话对不对」。
"""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass, field

logger = logging.getLogger("astrbot_stub")


# ── message_components ───────────────────────────────────────────────────
class BaseMessageComponent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Plain(BaseMessageComponent):
    type = "plain"

    def __init__(self, text: str = "", **kwargs):
        super().__init__(**kwargs)
        self.text = text


class Image(BaseMessageComponent):
    type = "image"

    def __init__(self, file: str = "", url: str = "", path: str = "", **kwargs):
        super().__init__(**kwargs)
        self.file = file
        self.url = url
        self.path = path

    @classmethod
    def fromURL(cls, url: str, **_):
        return cls(url=url, file=url)

    @classmethod
    def fromFileSystem(cls, path: str, **_):
        return cls(file=path, path=path)

    async def convert_to_file_path(self) -> str:
        return self.path or self.file


class Reply(BaseMessageComponent):
    type = "reply"

    def __init__(self, id=None, chain=None, **kwargs):
        super().__init__(**kwargs)
        self.id = id
        self.chain = chain or []


class At(BaseMessageComponent):
    type = "at"


# ── event / filter / star ────────────────────────────────────────────────
@dataclass
class MessageEventResult:
    chain: list = field(default_factory=list)


class AstrMessageEvent:
    """只保留插件用到的方法。"""

    def __init__(self, message_str="", messages=None, group_id="", sender_id="10001",
                 admin=False, origin="stub:FriendMessage:10001"):
        self.message_str = message_str
        self._messages = messages or []
        self._group_id = group_id
        self._sender_id = sender_id
        self._admin = admin
        self.unified_msg_origin = origin
        self.bot = None
        self._extras = {}

    def get_messages(self):
        return self._messages

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def is_admin(self):
        return self._admin

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult([Plain(text)])

    def chain_result(self, chain) -> MessageEventResult:
        return MessageEventResult(list(chain))

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class _Filter:
    """`@filter.command(...)` 只做登记，不做真实过滤（测试里直接调 handler）。"""

    commands: list[str] = []

    def command(self, name, alias=None, **_):
        _Filter.commands.append(name)
        for extra in alias or []:
            _Filter.commands.append(extra)

        def decorator(func):
            func.__astrbot_command__ = name
            return func

        return decorator

    def event_message_type(self, *_args, **_kwargs):
        def decorator(func):
            return func

        return decorator


filter = _Filter()


class Star:
    def __init__(self, context=None):
        self.context = context


class Context:
    def __init__(self, config=None):
        self._config = config or {}

    def get_config(self):
        return self._config


class StarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix=f"astrbot_data_{plugin_name}_"))
        return path


AstrBotConfig = dict


def install() -> None:
    """把桩装进 sys.modules；必须在 import main 之前调用。"""
    if "astrbot" in sys.modules and getattr(sys.modules["astrbot"], "__stub__", False):
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__stub__ = True

    api = types.ModuleType("astrbot.api")
    api.logger = logger
    api.AstrBotConfig = AstrBotConfig

    components = types.ModuleType("astrbot.api.message_components")
    for name in ("BaseMessageComponent", "Plain", "Image", "Reply", "At"):
        setattr(components, name, globals()[name])

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = filter

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools

    api.message_components = components
    api.event = event_mod
    api.star = star_mod

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.message_components"] = components
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
