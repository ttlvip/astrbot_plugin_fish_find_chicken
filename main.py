"""鱼鱼找鸡 —— AstrBot 插件（纯 Python 实现）。

给一张图，反查这人是哪个 coser、账号在哪。

核心链路（全部在进程内完成，不依赖 node，也不再调用任何外部脚本）：

    收图 → 谷歌 Lens（无头 Edge/Chrome + CDP）反查原帖 → 抽社媒账号 → 回一句话
           └ 同时并发跑 SauceNAO / trace.moe / IQDB 作为补充

**Lens 是主力**：真人照片只有它能从「一张图」走到「某个人的 X 主页」；
另外三个引擎只对动漫、动画截图有效。所以配置里的 `engines` 默认就带着 lens，
并且代理（AstrBot 全局 http_proxy）是让它跑起来的前提。

这个文件只负责三件事：怎么收图、怎么回话、怎么把配置读成 Settings。
引擎、账号抽取、回话排版都在 core/ 里。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import tempfile
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core import (
    Settings,
    StateStore,
    download_image,
    engine_labels,
    search_image,
    sniff_mime,
)
from .core.browser import resolve_locale
from .core.net import HttpError, normalize_proxy, proxy_host


PLUGIN_NAME = "astrbot_plugin_fish_find_chicken"
PLUGIN_DIR = Path(__file__).resolve().parent

WELCOME = "收到，开始找鸡 🐔"


def _label_of(engine_id) -> str:
    """引擎 id → 展示名（`lens` → 谷歌 Lens），拿不到就原样返回。"""
    labels = engine_labels([engine_id])
    return labels[0] if labels else str(engine_id or "未知")
HELP_TEXT = """🐔 鱼鱼找鸡 · 用法

① 引用一张带图的消息，发 /鱼鱼找鸡
② 图跟指令发在同一条消息里：/鱼鱼找鸡
③ 直接给图片网址：/鱼鱼找鸡 https://…
④ 想看不筛选的全部账号：/鱼鱼找鸡 全部

常用的：
/鱼鱼找鸡            反查当前引用/消息里的图
/鱼鱼找鸡 全部        这次不按名字筛选，把翻到的号全列出来（自己核对）
/鱼鱼找鸡 帮助        看这段说明
/找鸡自检            管理员用：看浏览器、代理、引擎是否就绪
/找鸡重置浏览器      管理员用：清掉浏览器 profile（被谷歌标记、或换代理后建议清）

别名：/找鸡、/coser、/反查

说明：反查会把图片上传给搜索引擎（谷歌 Lens / SauceNAO / trace.moe）。
查不到是常态 —— 私图、新图、裁过的图都查不到；查到了也是「最可能」的账号，点开核对一下。"""

# 匹配「/鱼鱼找鸡 参数」，兼容各种前缀写法（AstrBot 有时会把指令前缀吃掉）
COMMAND_RE = re.compile(r"^[/!！]?\s*(?:鱼鱼找鸡|找鸡|coser|反查)\s*(.*)$", re.IGNORECASE | re.DOTALL)
HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)


class FishFindChicken(Star):
    """鱼鱼找鸡：图片反查 coser 社交账号。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._store: StateStore | None = None
        # 反查很重（Lens 要开无头浏览器），并发必须收着点，否则一次来三张图会把机器压满
        self._semaphore: asyncio.Semaphore | None = None
        self._temp_dirs: list[str] = []

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def initialize(self):
        """插件加载时把「用哪套引擎、代理通不通、浏览器在不在」提前写进日志。"""
        settings = self._settings()
        self._store = StateStore(self._data_dir() / "state.json")
        await self._store.load()

        proxy_text = (
            f"{proxy_host(settings.proxy)}（来自 {settings.proxy_source}）"
            if settings.proxy
            else "直连（国内连不上谷歌，Lens 会失败）"
        )
        logger.info(
            f"[鱼鱼找鸡] 引擎：{','.join(settings.engines)}；代理：{proxy_text}；"
            f"超时：{settings.timeout_sec}s"
        )
        if settings.browser_region:
            logger.info(
                f"[鱼鱼找鸡] 浏览器地区对齐：{settings.browser_region}"
                "（时区/语言跟随出口 IP，减少「容器时区 + 出口 IP」互相矛盾）"
            )

        if settings.lens_enabled:
            try:
                if settings.serpapi_ready:
                    logger.info(
                        "[鱼鱼找鸡] Lens 通道：SerpApi（不启动本地浏览器，不会撞人机验证）"
                    )
                elif settings.serpapi_enabled:
                    logger.warning(
                        "[鱼鱼找鸡] 开了 SerpApi 但没填 serpapi_api_key，已回退本地浏览器"
                    )
                if settings.serpapi_ready:
                    pass
                elif settings.browser_cdp:
                    logger.info(f"[鱼鱼找鸡] Lens 可用，外部浏览器：{settings.browser_cdp}")
                elif self._browser_exe(settings):
                    logger.info(f"[鱼鱼找鸡] Lens 可用，浏览器：{self._browser_exe(settings)}")
                else:
                    logger.warning(
                        "[鱼鱼找鸡] 没找到 Chrome/Edge，Lens（图片找人）不可用，"
                        "只剩 SauceNAO / trace.moe / IQDB 这类补充引擎。"
                        "装一个 Chrome/Edge，或在配置里填 browser_exe / "
                        "browser_cdp（外部浏览器）指向可用的浏览器。"
                    )
            except Exception as exc:  # noqa: BLE001 - 自检失败不能拖垮插件加载
                logger.warning(f"[鱼鱼找鸡] Lens 自检失败：{exc}")

        if not settings.proxy and settings.lens_enabled:
            logger.warning(
                "[鱼鱼找鸡] 没配置代理。谷歌在国内直连不通，"
                "请到 AstrBot 设置里填 http_proxy（或插件配置的 proxy）。"
            )

    async def terminate(self):
        """插件被停用/卸载时收掉浏览器和临时文件。"""
        for path in self._temp_dirs:
            shutil.rmtree(path, ignore_errors=True)
        self._temp_dirs.clear()
        try:
            from .core.browser import close_browser

            await close_browser()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[鱼鱼找鸡] 关闭浏览器时出错：{exc}")

    # ── 指令 ────────────────────────────────────────────────────────────
    @filter.command("鱼鱼找鸡", alias={"找鸡", "coser", "反查"})
    async def fish_find_chicken(self, event: AstrMessageEvent):
        """给一张图，反查这人是哪个 coser、账号在哪"""
        settings = self._settings()
        if not settings.enabled:
            yield event.plain_result("鱼鱼找鸡现在是关的（插件配置里 enabled 没开）")
            return

        arg = self._command_arg(event)
        if arg in ("帮助", "help", "-h", "--help"):
            yield event.plain_result(HELP_TEXT)
            return

        # `/鱼鱼找鸡 全部`：这次不按「名字主轴」筛选，把翻到的号都列出来。
        # 有些人的账号显示名和帖子里写的名字不一样（罗马音/换名/简称），
        # 严格筛选会把人挡掉，这时候需要手动放宽。
        arg, want_all = self._split_all_arg(arg)
        if want_all:
            settings.strict_consensus = False

        if not self._group_allowed(event, settings):
            yield event.plain_result("这个群没开鱼鱼找鸡")
            return

        store = await self._ensure_store()
        ok, reason = store.quota_check(
            key=self._quota_key(event),
            user_id=self._user_id(event),
            is_admin=self._is_admin(event),
            cooldown_sec=settings.cooldown_sec,
            per_user_day=settings.per_user_day,
            global_day=settings.global_day,
        )
        if not ok:
            yield event.plain_result(reason)
            return

        # 1. 先把图弄到本地：当前消息 → 引用消息 → 参数里的网址
        image_path = ""
        try:
            image_path = await self._resolve_image(event, arg, settings)
        except HttpError as exc:
            yield event.plain_result(f"图没取到：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[鱼鱼找鸡] 取图失败：{exc}")
            yield event.plain_result(f"图没取到：{exc}")
            return

        if not image_path:
            yield event.plain_result("要引用一张图（或者图跟指令一起发）再发 /鱼鱼找鸡")
            return

        await store.charge(
            key=self._quota_key(event),
            user_id=self._user_id(event),
            is_admin=self._is_admin(event),
        )

        if settings.notify_start:
            yield event.plain_result(WELCOME)

        try:
            data = Path(image_path).read_bytes()
            mime = sniff_mime(data)
            logger.info(
                f"[鱼鱼找鸡] 开始反查：{len(data) // 1024}KB {mime}，"
                f"引擎 {','.join(settings.engines)}"
            )
            result = await self._search(data, mime, settings)
        except asyncio.TimeoutError:
            yield event.plain_result(
                "查太久了，先放弃。\n"
                "通常是谷歌 Lens 连不上：检查 AstrBot 设置里的 http_proxy 是否可用。"
            )
            return
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成一句人话
            logger.error(f"[鱼鱼找鸡] 反查失败：{exc}")
            yield event.plain_result(f"反查出错了：{exc}")
            return
        finally:
            # 只删我们自己下的临时图，别动 AstrBot 的媒体缓存
            self._drop_temp(image_path)

        accounts = result.get("accounts") or []
        if not accounts and not settings.send_on_miss:
            logger.info("[鱼鱼找鸡] 没查到账号，按配置不回复")
            return

        text = self._format_text(result, settings)

        # 命中时把「找到了」那张图跟文字合成一条发（两条容易被刷走）
        found = self._found_image(settings)
        if accounts and found is not None:
            yield event.chain_result([found, Comp.Plain(text)])
            return
        yield event.plain_result(text)

    @filter.command("找鸡帮助", alias={"鱼鱼找鸡帮助", "coser帮助"})
    async def fish_help(self, event: AstrMessageEvent):
        """鱼鱼找鸡怎么用"""
        yield event.plain_result(HELP_TEXT)

    @filter.command("找鸡自检", alias={"鱼鱼找鸡自检"})
    async def fish_selftest(self, event: AstrMessageEvent):
        """检查浏览器、代理、引擎是否就绪"""
        if not self._is_admin(event):
            yield event.plain_result("自检只有管理员能用")
            return

        settings = self._settings()
        lines = ["🐔 鱼鱼找鸡 · 自检", ""]

        # 引擎与 Lens 通道
        lines.append(f"引擎：{','.join(settings.engines) or '无'}")
        if not settings.lens_enabled:
            lines.append("Lens 通道：未启用 lens，不检查")
        elif settings.serpapi_ready:
            key = settings.serpapi_api_key
            lines.append(
                f"Lens 通道：SerpApi（key …{key[-4:]}，不启动本地浏览器，也不会撞人机验证）"
            )
        elif settings.serpapi_enabled:
            lines.append("Lens 通道：本地浏览器（SerpApi 开关开着但没填 key，已回退）")
        elif settings.browser_cdp:
            try:
                ok, detail = await self._probe_cdp(
                    settings.browser_cdp, settings.browser_cdp_insecure
                )
                mark = "" if ok else "（连不上，Lens 会失败）"
                lines.append(f"Lens 通道：外部 CDP {settings.browser_cdp}{mark}·{detail}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"Lens 通道：外部 CDP 检查失败（{exc}）")
        else:
            try:
                exe = self._browser_exe(settings)
                lines.append(f"Lens 通道：本地浏览器 {exe or '（没找到，Lens 不可用）'}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"Lens 通道：检查失败（{exc}）")

        # 代理
        lines.append(
            f"代理：{proxy_host(settings.proxy) or '直连'}（来自 {settings.proxy_source}）"
        )
        if settings.proxy:
            reachable = await self._probe_google(settings.proxy)
            lines.append(f"谷歌连通性：{'通' if reachable else '不通（Lens 会失败）'}")

        # 地区对齐：这行是「被验证码挡住」时最先要看的东西之一
        if settings.browser_region:
            try:
                loc = await resolve_locale(settings.browser_region, settings.proxy)
                lines.append(f"地区对齐：{settings.browser_region} → {loc.describe()}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"地区对齐：{settings.browser_region}（解析失败：{exc}）")
        else:
            lines.append("地区对齐：未开启（browser_region 留空，保持 zh-CN + 容器时区）")

        lines.append(f"超时：{settings.timeout_sec} 秒/引擎；配额：单人 {settings.per_user_day} 次/天")
        lines.append(f"状态文件：{self._data_dir() / 'state.json'}")
        yield event.plain_result("\n".join(lines))

    async def _ensure_store(self) -> StateStore:
        """状态文件懒加载：热重载时 initialize() 可能还没跑到，指令就先来了。"""
        if self._store is None:
            self._store = StateStore(self._data_dir() / "state.json")
            await self._store.load()
        return self._store

    @filter.command("找鸡重置浏览器", alias={"鱼鱼找鸡重置浏览器"})
    async def fish_reset_browser(self, event: AstrMessageEvent):
        """清掉本地浏览器的 profile（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能重置浏览器")
            return

        settings = self._settings()
        if settings.browser_cdp:
            yield event.plain_result(
                "当前用的是外部浏览器（browser_cdp），profile 不归插件管。"
                "要清的话得去那个浏览器容器里删它的 user-data-dir。"
            )
            return

        from .core.browser import close_browser

        try:
            await close_browser()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[鱼鱼找鸡] 关闭浏览器失败：{exc}")

        profile = Path(settings.browser_profile) if settings.browser_profile else None
        removed = False
        if profile and profile.is_dir():
            removed = shutil.rmtree(profile, ignore_errors=True) is None

        yield event.plain_result(
            (
                f"已清掉浏览器 profile：{profile}\n"
                "下次查询会用全新身份重新开始（cookie 也一起没了）。"
            )
            if removed
            else "没找到可清的 profile 目录，浏览器已关闭。下次查询会重建。"
        )

    # ── 配置 ────────────────────────────────────────────────────────────
    def _settings(self) -> Settings:
        settings = Settings.from_config(self.config, self._global_config())
        # 固定 profile 不是可有可无的优化：谷歌会把「已通过验证」记在 cookie 里，
        # 临时 profile 每次查询都是新访客，人机验证概率明显更高（实测）。
        if not settings.browser_profile:
            settings.browser_profile = str(self._data_dir() / "browser_profile")
        return settings

    def _global_config(self) -> dict:
        try:
            config = self.context.get_config() if self.context is not None else None
        except Exception:  # noqa: BLE001
            return {}
        if not config:
            return {}
        try:
            # AstrBotConfig 是 dict 子类，直接取 http_proxy 即可
            return {"http_proxy": config.get("http_proxy", "")}
        except Exception:  # noqa: BLE001
            return {}

    def _data_dir(self) -> Path:
        """插件数据目录：优先用 AstrBot 的 data 目录（插件目录会被更新覆盖）。"""
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001 - 老版本 AstrBot 没有 StarTools 时退回插件目录
            path = PLUGIN_DIR / "data"
            path.mkdir(parents=True, exist_ok=True)
            return path

    # ── 权限与身份 ──────────────────────────────────────────────────────
    @staticmethod
    def _user_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _quota_key(self, event: AstrMessageEvent) -> str:
        try:
            origin = event.unified_msg_origin
        except Exception:  # noqa: BLE001
            origin = ""
        return f"{origin}|{self._user_id(event)}"

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _group_allowed(event: AstrMessageEvent, settings: Settings) -> bool:
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            group_id = ""
        if not group_id:
            return True  # 私聊不受群白/黑名单影响
        if group_id in settings.group_blacklist:
            return False
        if settings.group_whitelist and group_id not in settings.group_whitelist:
            return False
        return True

    # ── 取图 ────────────────────────────────────────────────────────────
    @staticmethod
    def _split_all_arg(arg: str) -> tuple[str, bool]:
        """把 `全部` / `all` 从参数里摘出来，返回 (剩余参数, 是否要求全部账号)。"""
        text = (arg or "").strip()
        for keyword in ("全部", "所有", "all", "--all"):
            if text == keyword:
                return "", True
            if text.startswith(keyword + " "):
                return text[len(keyword) :].strip(), True
        return text, False

    def _command_arg(self, event: AstrMessageEvent) -> str:
        raw = (getattr(event, "message_str", "") or "").strip()
        match = COMMAND_RE.match(raw)
        if match:
            return match.group(1).strip()
        return raw

    async def _resolve_image(
        self, event: AstrMessageEvent, arg: str, settings: Settings
    ) -> str:
        """把要查的那张图落到本地，返回路径；没有图返回空串。"""
        # ① 当前消息里直接带的图
        comp = self._first_image(event.get_messages())
        if comp is not None:
            return await self._component_to_path(comp, settings)

        # ② 引用消息里的图
        for item in event.get_messages():
            if not isinstance(item, Comp.Reply):
                continue
            comp = self._first_image(getattr(item, "chain", None) or [])
            if comp is not None:
                return await self._component_to_path(comp, settings)
            quoted = await self._quoted_image_from_platform(event, item.id)
            if quoted is not None:
                return await self._component_to_path(quoted, settings)

        # ③ 参数里给的网址
        if arg and HTTP_RE.match(arg):
            if not settings.allow_url_query:
                raise HttpError("这个群没开「网址反查」")
            data, mime = await download_image(
                arg, proxy=settings.proxy, max_bytes=settings.max_image_bytes
            )
            return self._write_temp(data, mime)

        # ④ 参数里给的本机路径（默认关，只有机器人主人自己方便用）
        if arg and settings.allow_local_path:
            path = Path(arg).expanduser()
            if path.is_file():
                self._check_size(path.stat().st_size, settings)
                return str(path)
        return ""

    @staticmethod
    def _first_image(components) -> Comp.Image | None:
        for comp in components or []:
            if isinstance(comp, Comp.Image):
                return comp
        return None

    async def _quoted_image_from_platform(
        self, event: AstrMessageEvent, reply_id
    ) -> Comp.Image | None:
        """通过 OneBot 的 get_msg 把被引用消息的图捞回来（只有 aiocqhttp 支持）。"""
        if not reply_id:
            return None
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if call_action is None:
            return None
        try:
            data = await call_action("get_msg", message_id=int(reply_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[鱼鱼找鸡] get_msg 失败：{exc}")
            return None
        segments = (data or {}).get("message") if isinstance(data, dict) else None
        if isinstance(segments, str):
            # 部分实现返回 CQ 码字符串，取出 url 参数就够了
            found = re.search(r"\[CQ:image,[^\]]*url=([^,\]]+)", segments)
            return Comp.Image(file=found.group(1)) if found else None
        for seg in segments or []:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            payload = seg.get("data") or {}
            url = payload.get("url") or payload.get("file") or ""
            if url:
                return Comp.Image(file=url)
        return None

    async def _component_to_path(self, comp: Comp.Image, settings: Settings) -> str:
        """把 AstrBot 的图片组件落成本地文件（统一放进我们自己的临时目录）。"""
        try:
            path = await comp.convert_to_file_path()
            if path and Path(path).is_file():
                self._check_size(Path(path).stat().st_size, settings)
                return self._stage(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[鱼鱼找鸡] 组件转本地文件失败，改自己下载：{exc}")
        raw = (getattr(comp, "url", "") or getattr(comp, "file", "") or "").strip()
        if not raw:
            raise HttpError("这条消息里的图没有可用的地址")
        return await self._materialize(raw, settings)

    def _stage(self, path: str) -> str:
        """把外部文件拷进临时目录 —— 这样后面能安心删，也不会碰到 AstrBot 的媒体缓存。"""
        if self._is_temp(path):
            return path
        target = Path(self._new_temp_dir()) / f"query{Path(path).suffix or '.jpg'}"
        shutil.copyfile(path, target)
        return str(target)

    def _new_temp_dir(self) -> str:
        directory = tempfile.mkdtemp(prefix="astrbot_fish_")
        self._temp_dirs.append(directory)
        return directory

    def _is_temp(self, path: str) -> bool:
        try:
            resolved = Path(path).resolve()
        except OSError:
            return False
        for directory in self._temp_dirs:
            try:
                resolved.relative_to(Path(directory).resolve())
                return True
            except ValueError:
                continue
        return False

    def _drop_temp(self, path: str) -> None:
        """只删我们自己造的临时文件，别人的文件一概不动。"""
        if not path or not self._is_temp(path):
            return
        parent = Path(path).parent.resolve()
        shutil.rmtree(parent, ignore_errors=True)
        for directory in list(self._temp_dirs):
            try:
                if Path(directory).resolve() == parent:
                    self._temp_dirs.remove(directory)
                    break
            except OSError:
                continue

    async def _materialize(self, raw: str, settings: Settings) -> str:
        if raw.startswith("base64://"):
            data = base64.b64decode(raw[len("base64://") :])
            self._check_size(len(data), settings)
            return self._write_temp(data, sniff_mime(data))
        if raw.startswith("data:image/"):
            data = base64.b64decode(raw.split(",", 1)[1])
            self._check_size(len(data), settings)
            return self._write_temp(data, sniff_mime(data))
        if raw.startswith("file://"):
            path = Path(raw[len("file://") :])
            if path.is_file():
                self._check_size(path.stat().st_size, settings)
                return self._stage(str(path))
        if HTTP_RE.match(raw):
            data, mime = await download_image(
                raw, proxy=settings.proxy, max_bytes=settings.max_image_bytes
            )
            return self._write_temp(data, mime)
        path = Path(raw)
        if path.is_file():
            self._check_size(path.stat().st_size, settings)
            return self._stage(str(path))
        raise HttpError("图片地址既不是网址也不是本地文件")

    def _check_size(self, size: int, settings: Settings) -> None:
        limit = settings.max_image_bytes
        if size > limit:
            raise HttpError(f"图太大了（{size / 1048576:.1f}MB，上限 {limit // 1048576}MB）")

    def _write_temp(self, data: bytes, mime: str) -> str:
        suffix = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(mime, "jpg")
        path = Path(self._new_temp_dir()) / f"query.{suffix}"
        path.write_bytes(data)
        return str(path)

    # ── 反查 ────────────────────────────────────────────────────────────
    async def _search(self, data: bytes, mime: str, settings: Settings) -> dict:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent))
        async with self._semaphore:
            return await search_image(
                data,
                mime,
                settings=settings,
                store=self._store,
                on_log=lambda line: logger.debug(line),
            )

    @staticmethod
    async def _probe_cdp(cdp_url: str, insecure: bool = False) -> tuple[bool, str]:
        """探一下外部浏览器的调试端口，返回 (通不通, 版本或错误摘要)。"""
        try:
            from .core.browser import normalize_cdp_base
            from .core.net import request

            base, _host, _port = normalize_cdp_base(cdp_url)
            result = await request(
                "GET", f"{base}/json/version", timeout=5.0, insecure=insecure
            )
            if result.status != 200:
                return False, f"HTTP {result.status}"
            info = json.loads(result.text or "{}")
            return True, str(info.get("Browser") or "已连通")
        except Exception as exc:  # noqa: BLE001 - 自检不能抛
            return False, f"{type(exc).__name__}: {str(exc)[:60]}"

    @staticmethod
    def _browser_exe(settings: Settings) -> str:
        """本机可用的 Chrome/Edge 路径；没有就返回空串（只影响 lens 引擎）。"""
        try:
            from .core.browser import find_browser_exe
        except Exception:  # noqa: BLE001 - 老版本没有这个模块
            return ""
        return find_browser_exe(settings.browser_exe)

    async def _probe_google(self, proxy: str) -> bool:
        """自检用：带代理探一下 lens.google.com 通不通（8 秒超时）。"""
        try:
            from .core.net import request

            result = await request(
                "GET", "https://lens.google.com/", proxy=normalize_proxy(proxy), timeout=8.0
            )
            return result.status < 500
        except Exception:  # noqa: BLE001
            return False

    # ── 回话 ────────────────────────────────────────────────────────────
    def _format_text(self, data: dict, settings: Settings) -> str:
        # 排版由 core/reply.py 负责（含三种「没查到」的区分）；这里只去掉 QQ 不认的加粗
        text = str(data.get("text") or "").replace("**", "").strip()
        if not text:
            text = "🎯 鱼鱼找鸡结果\n\n❌ 没查到账号。"
        if data.get("cached"):
            text += "\n\n· 这张图之前查过，直接给的上次结果"

        if not settings.show_engine_detail:
            return text

        engines = data.get("enginesUsed") or []
        labels = " / ".join(engine_labels(engines)) or "无"
        footer = f"⏱ 引擎 {labels}"
        # 只报真故障（连不上/被验证/限流）。「空结果」是引擎正常工作但没匹配到，
        # 属于正常业务结果，写进警告只会让人以为插件坏了。
        failures = [
            f
            for f in (data.get("failures") or [])
            if str(f.get("reason") or "").strip() not in ("空结果", "")
        ]
        if failures:
            detail = "、".join(
                f"{_label_of(f.get('engine'))}（{str(f.get('reason'))[:30]}）" for f in failures[:4]
            )
            footer += f"\n⚠️ 没出结果：{detail}"
        return f"{text}\n\n{footer}"

    def _found_image(self, settings: Settings):
        """命中时要附带的那张「已找到」图，支持网址和路径，空 = 不发。"""
        raw = settings.found_image
        if not raw:
            return None
        if HTTP_RE.match(raw):
            try:
                return Comp.Image.fromURL(raw)
            except Exception:  # noqa: BLE001
                return None
        path = Path(raw)
        if not path.is_absolute():
            path = PLUGIN_DIR / path
        if path.is_file():
            try:
                return Comp.Image.fromFileSystem(str(path))
            except Exception:  # noqa: BLE001
                return None
        return None
