"""极简 CDP 客户端 + 无头浏览器单例。

为什么不用 playwright / selenium / pyppeteer：
AstrBot 插件只允许依赖 aiohttp，装不了额外驱动也起不了 node 子进程；而 Chrome DevTools
Protocol 本质只有两件事 —— 「HTTP 端点拿 ws 地址」+「WebSocket 上发 JSON 命令」——
aiohttp 的 ws_connect 直接就能说这个协议，一两百行足够了。

协议要点（都是实测结论，不要凭直觉改）：
- 启动参数带 ``--remote-debugging-port=0``，真实端口写在
  ``<profile_dir>/DevToolsActivePort`` 文件第一行；所以启动前必须先删掉这个旧文件，
  否则会读到上一次运行的死端口（表现为连不上、卡在等 /json/list）。
- ``/json/list`` 拿页面 target 的 ``webSocketDebuggerUrl`` 建主连接；
  新建标签页必须用 ``PUT /json/new?about:blank``（部分版本用 GET 会 405）。
- 本地回环探测（127.0.0.1）必须显式 ``proxy=None``，否则用户配了 http_proxy 时
  连自己的调试端口也会被塞进代理，直接超时。
- 浏览器是「一个进程 + 一堆子进程」的重资源，插件每次查询都启一个的话，
  冷启动 1~2 秒起步还容易攒僵尸进程，所以这里做进程内单例，跨查询复用。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
from urllib.parse import urlsplit
import time
from pathlib import Path
from typing import Any, Callable

import aiohttp

logger = logging.getLogger(__name__)

__all__ = [
    "Browser",
    "BrowserError",
    "Tab",
    "close_browser",
    "find_browser_exe",
    "get_browser",
]

# 用桌面版 Chrome 的 UA：headless 默认 UA 里带 "HeadlessChrome"，谷歌会直接给验证码。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# navigator.webdriver 是最容易被识别的自动化指纹，抹成 undefined 成本极低。
WEBDRIVER_PATCH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

# 更完整的「别像个机器人」补丁。
#
# 为什么需要：Playwright / Puppeteer 或无头 Chromium 会留下几个很容易被识别的痕迹 ——
# `navigator.webdriver === true`、`window.chrome` 缺失、`navigator.plugins` 为空、
# WebGL renderer 是 "SwiftShader"、UA 里带 "HeadlessChrome"。谷歌看到这些就直接给验证码，
# 跟「查得太频繁」是两回事。这里只做**让浏览器看起来正常**，不碰验证码本身
# （验证码是谷歌的判定结果，硬破既不靠谱也没意义）。
STEALTH_JS = r"""(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try {
    if (!window.chrome) {
      window.chrome = {runtime: {}, loadTimes: function () {}, csi: function () {}, app: {}};
    }
  } catch (e) {}
  try {
    const plugins = [
      {name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
      {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
      {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    ];
    Object.defineProperty(navigator, 'plugins', {get: () => plugins});
    Object.defineProperty(navigator, 'mimeTypes', {get: () => ({length: 0})});
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
  } catch (e) {}
  try {
    const origQuery = navigator.permissions && navigator.permissions.query;
    if (origQuery) {
      navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({state: Notification.permission, onchange: null})
          : origQuery.call(navigator.permissions, params);
    }
  } catch (e) {}
  try {
    const patchGL = (proto) => {
      if (!proto || proto.__ffc) return;
      const orig = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return orig.call(this, p);
      };
      proto.__ffc = true;
    };
    patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch (e) {}
})();"""

# 覆盖 UA 时用的语言，和 STEALTH_JS 里的 navigator.languages 保持一致
STEALTH_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"

# 启动前要清掉的 profile 锁/端口文件。上一次进程被 kill 后这些文件会留下来，
# 新进程看到「已有实例占用 profile」会直接退出，日志里只有一行看不懂的 error。
_PROFILE_STALE = (
    "DevToolsActivePort",
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
)

_WINDOWS_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
)

_LINUX_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "chrome",
    "headless_shell",
    "chrome-headless-shell",
)

_MACOS_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


class BrowserError(RuntimeError):
    """浏览器无法启动 / CDP 通信出错。"""


# ── 浏览器可执行文件探测 ────────────────────────────────────────────────────


def _exe_ok(path: str) -> bool:
    try:
        return bool(path) and Path(path).is_file()
    except OSError:
        return False


def _version_of(dirname: str) -> int:
    """从 `chromium-1091` / `chromium_headless_shell-1091` 里取版本号，用于排序。"""
    tail = dirname.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def playwright_browser_candidates() -> list[str]:
    r"""Playwright 装在本机的 Chromium 路径（容器里最常见的一种「现成浏览器」）。

    Playwright 的浏览器不在 PATH 里，而是放在 ms-playwright 缓存目录下，所以要单独找：

        /ms-playwright/chromium-1091/chrome-linux/chrome              ← 官方镜像
        ~/.cache/ms-playwright/chromium-1091/chrome-linux/chrome      ← pip install playwright
        %LOCALAPPDATA%\ms-playwright\chromium-1091\chrome-win\chrome.exe

    优先级：**完整 chromium 高于 chromium_headless_shell**。headless_shell 是精简版，
    天生只能无头运行、缺不少特性，只有在没有完整版时才退而求其次。
    同一类里版本号大的优先。
    """
    roots: list[Path] = []
    env = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if env and env != "0":
        roots.append(Path(env).expanduser())
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "ms-playwright")
    else:
        roots.append(Path("/ms-playwright"))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    roots.append(Path("/root/.cache/ms-playwright"))

    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
            entries = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            continue
        # 完整版优先，其次 headless shell
        for prefix, rel in (
            ("chromium-", ("chrome-linux/chrome", "chrome-win/chrome.exe", "chrome-mac/Chromium.app/Contents/MacOS/Chromium")),
            ("chromium_headless_shell-", ("chrome-linux/headless_shell", "chrome-win/headless_shell.exe")),
        ):
            matches = sorted(
                (p for p in entries if p.name.startswith(prefix)),
                key=lambda p: _version_of(p.name),
                reverse=True,
            )
            for entry in matches:
                for part in rel:
                    exe = entry / part
                    if _exe_ok(str(exe)):
                        key = str(exe)
                        if key not in seen:
                            seen.add(key)
                            out.append(key)
    return out


def find_browser_exe(configured: str = "") -> str:
    """找可用的 Chrome / Edge 可执行文件。配置优先，找不到返回空串。"""
    configured = (configured or "").strip().strip('"')
    if configured:
        # 用户明确配置了就尊重它，即使路径写错也返回，让启动阶段报出真实的错误信息。
        return configured

    if os.name == "nt":
        cands = list(_WINDOWS_CANDIDATES)
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            cands.append(str(Path(base) / "Microsoft/Edge/Application/msedge.exe"))
            cands.append(str(Path(base) / "Google/Chrome/Application/chrome.exe"))
            cands.append(str(Path(base) / "Chromium/Application/chrome.exe"))
        for path in cands:
            if _exe_ok(path):
                return path
        # 系统里没装浏览器时，退到 Playwright 自带的那份（很多 Docker 镜像就是这种情况）
        for path in playwright_browser_candidates():
            return path
        return ""

    # Linux / macOS：先按 PATH 找，再试固定安装路径。
    for name in _LINUX_NAMES:
        found = shutil.which(name)
        if found:
            return found
    # 容器里几乎都没有系统浏览器，但装了 playwright 的话会有一份现成的 chromium
    for path in playwright_browser_candidates():
        return path
    for path in _MACOS_CANDIDATES:
        if _exe_ok(path):
            return path
    return ""


# ── CDP 连接 ────────────────────────────────────────────────────────────────


class _CDP:
    """一条 WebSocket 上的 CDP 会话。

    只做最小闭环：命令按 id 递增、响应按 id 唤醒对应 future、超时由调用方兜底。
    CDP 的事件（没有 id 的消息）本项目一律不订阅，直接丢弃。
    """

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, session: aiohttp.ClientSession) -> None:
        self._ws = ws
        self._session = session
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: dict[str, list[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    async def connect(cls, ws_url: str, *, timeout: float = 15.0, insecure: bool = False) -> "_CDP":
        if not ws_url:
            raise BrowserError("拿不到调试地址（webSocketDebuggerUrl 为空）")
        # sock_connect 单独给 10 秒：本地回环正常是毫秒级，卡住说明浏览器已经死了。
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
            trust_env=False,
            connector=aiohttp.TCPConnector(ssl=False) if insecure else None,
        )
        try:
            ws = await asyncio.wait_for(session.ws_connect(ws_url, max_msg_size=0), timeout)
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
            await session.close()
            raise BrowserError(f"连不上浏览器调试端口：{exc}") from exc
        cdp = cls(ws, session)
        cdp._reader_task = asyncio.create_task(cdp._reader())
        return cdp

    @property
    def closed(self) -> bool:
        return self._closed or self._ws.closed

    def add_listener(self, method: str, handler: Any) -> None:
        """订阅 CDP 事件（只给 lens 抓 302 目标用，不追求通用）。"""
        self._listeners.setdefault(method, []).append(handler)

    def remove_listener(self, method: str, handler: Any) -> None:
        bucket = self._listeners.get(method)
        if not bucket:
            return
        with contextlib.suppress(ValueError):
            bucket.remove(handler)
        if not bucket:
            self._listeners.pop(method, None)

    async def _reader(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    msg_id = data.get("id")
                    if not isinstance(msg_id, int):
                        self._dispatch(data)
                        continue
                    fut = self._pending.get(msg_id)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 网络抖动，按断开处理
            logger.debug("CDP reader 异常退出：%s", exc)
        finally:
            # 连接断了必须把所有在等的 future 唤醒成异常，否则调用方会一直挂到超时。
            self._fail_pending("CDP 连接已断开")

    def _dispatch(self, data: dict[str, Any]) -> None:
        method = data.get("method")
        if not isinstance(method, str):
            return
        for handler in tuple(self._listeners.get(method, ())):
            try:
                handler(data.get("params") or {})
            except Exception as exc:  # 回调出错不能把 reader 带崩
                logger.debug("CDP 事件回调出错（%s）：%s", method, exc)

    def _fail_pending(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(BrowserError(reason))
        self._pending.clear()

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0
    ) -> dict[str, Any]:
        if self.closed:
            raise BrowserError("CDP 连接已关闭")
        self._next_id += 1
        msg_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
            await asyncio.wait_for(self._ws.send_str(payload), timeout)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise BrowserError(f"浏览器没响应 {method}（超过 {timeout:.0f} 秒）") from exc
        except aiohttp.ClientError as exc:
            raise BrowserError(f"CDP 发送失败：{exc}") from exc
        finally:
            self._pending.pop(msg_id, None)

    async def js(
        self, expression: str, *, await_promise: bool = False, timeout: float = 15.0
    ) -> Any:
        params: dict[str, Any] = {"expression": expression, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        resp = await self.send("Runtime.evaluate", params, timeout=timeout)
        if resp.get("error"):
            raise BrowserError(f"CDP 报错：{_short(resp['error'])}")
        result = resp.get("result") or {}
        if result.get("exceptionDetails"):
            raise BrowserError(f"页面脚本报错：{_short(result['exceptionDetails'])}")
        return (result.get("result") or {}).get("value")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending("CDP 连接已关闭")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._ws.close(), 3)
        task = self._reader_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._reader_task = None
        with contextlib.suppress(Exception):
            await self._session.close()


def _short(obj: Any, limit: int = 160) -> str:
    """CDP 的错误体是嵌套 dict，直接丢给日志会刷屏，这里取一小段字符串。"""
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(obj)
    return text if len(text) <= limit else text[:limit] + "…"


async def apply_stealth(cdp: _CDP, *, reason: str = "") -> None:
    """给一个 CDP 会话套上「正常浏览器」的伪装（UA + 指纹补丁）。

    必须对**每个** target 都做一次：Emulation.setUserAgentOverride 只作用于当前页面，
    Page.addScriptToEvaluateOnNewDocument 只作用于之后新开的文档，两者都不能跨 target。

    外部浏览器（尤其是 Playwright 自带的 Chromium）最关键的一步就是 UA 覆盖 ——
    它的默认 UA 里带 "HeadlessChrome"，谷歌一眼就能认出来。
    """
    with contextlib.suppress(BrowserError):
        await cdp.send(
            "Emulation.setUserAgentOverride",
            {
                "userAgent": USER_AGENT,
                "acceptLanguage": STEALTH_LANGUAGE,
                "platform": "Win32",
            },
            timeout=6,
        )
    with contextlib.suppress(BrowserError):
        await cdp.send(
            "Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS}, timeout=8
        )
    with contextlib.suppress(BrowserError):
        await cdp.js(STEALTH_JS, timeout=5)
    if reason:
        logger.debug("已应用浏览器伪装（%s）", reason)


# ── 标签页 ──────────────────────────────────────────────────────────────────


class Tab:
    """独立标签页 + 独立 CDP 连接。

    lens 的 token 解析是并发跑的，每个标签页一条连接，互不干扰；
    共用一条连接的话 CDP 的 Page.navigate 会互相打断。
    """

    def __init__(self, cdp: _CDP, browser: "Browser", *, owned: bool) -> None:
        self._cdp = cdp
        self._browser = browser
        self._owned = owned
        self._closed = False

    async def navigate(self, url: str, *, timeout: float = 20.0) -> None:
        """导航到 url。

        Page.navigate 的失败是「页面加载失败」而不是「CDP 失败」，两种要区别对待：
        - ``net::ERR_ABORTED``：重定向/新导航打断上一次加载，属于正常，吞掉；
        - 其它 ``net::ERR_*``（拒绝连接、DNS、代理隧道失败）：必须抛出去，
          上层据此给出「谷歌连不上（代理没开）」的提示，而不是傻等超时。
        """
        if self._closed:
            raise BrowserError("标签页已关闭")
        try:
            resp = await self._cdp.send("Page.navigate", {"url": url}, timeout=min(10.0, timeout))
        except asyncio.TimeoutError:
            # 导航命令本身超时：不代表页面没打开，交给调用方轮询 location.href 判断。
            return
        err = str((resp.get("error") or {}).get("message") or "")
        err_text = str((resp.get("result") or {}).get("errorText") or "")
        text = err or err_text
        if not text or "ERR_ABORTED" in text:
            return
        raise BrowserError(f"页面打不开：{text}")

    async def eval(self, expression: str, *, timeout: float = 15.0) -> Any:
        if self._closed:
            raise BrowserError("标签页已关闭")
        return await self._cdp.js(expression, timeout=timeout)

    def listen(self, method: str, handler: Any) -> Callable[[], None]:
        """订阅本标签页的 CDP 事件，返回取消订阅的函数。

        lens 靠 ``Network.requestWillBeSent`` 里的 ``redirectResponse`` 直接拿到
        302 的目标地址 —— 比等浏览器把跨域 TLS 建好、location.href 更新快一个数量级。
        """
        self._cdp.add_listener(method, handler)

        def unsubscribe() -> None:
            self._cdp.remove_listener(method, handler)

        return unsubscribe

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owned:
            # 自己开的标签页要关掉，否则反复查询会攒出几十个标签，内存一路上涨。
            with contextlib.suppress(Exception):
                await self._cdp.send("Page.close", timeout=3)
        await self._cdp.close()


# ── 浏览器主体 ──────────────────────────────────────────────────────────────


class Browser:
    """一个运行中的无头浏览器进程（主页面 + 按需新建的标签页）。"""

    def __init__(
        self,
        *,
        proc: asyncio.subprocess.Process | None,
        port: int,
        cdp: _CDP,
        exe: str,
        proxy: str,
        profile_dir: Path,
        temp_profile: bool,
        host: str = "127.0.0.1",
        external: bool = False,
        insecure: bool = False,
    ) -> None:
        # proc 为 None = 外部浏览器（另一个容器/宿主机上跑着 Chrome），我们只连 CDP。
        self._proc = proc
        self._host = host or "127.0.0.1"
        self.external = bool(external)
        # 外部浏览器如果挂在 https 反代后面（常见是自签证书），aiohttp 默认会校验证书
        # 直接失败，这里按配置决定是否放行。
        self._insecure = bool(insecure)
        self._port = port
        self._cdp = cdp
        self._exe = exe
        self._proxy = proxy
        self.profile_dir = profile_dir
        self._temp_profile = temp_profile
        self._main_ws_url = ""
        self._closed = False
        # 主页面操作要串行：一次查询会连着做「导航 → 注入表单 → 选文件 → 提交」，
        # 两个查询同时跑会互相把页面导航走，结果两边都拿不到卡片。
        self.page_lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None

    # -- 基本属性 ----------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def _base_url(self) -> str:
        """浏览器调试端口的 HTTP 基址。

        本地是 127.0.0.1；接外部浏览器时是另一个容器名/IP（如 http://chrome:9222），
        所以不能到处硬编码 127.0.0.1。
        """
        return f"http://{self._host}:{self._port}"

    @property
    def exe(self) -> str:
        return self._exe

    @property
    def proxy(self) -> str:
        return self._proxy

    @property
    def alive(self) -> bool:
        # 不能用 proc.returncode 判断死活：Windows 上 Edge 启动后会「交棒」给真正的
        # 浏览器进程，被 CreateProcess 起来的那个 msedge.exe 会立刻以 0 退出（实测），
        # 用退出码判断会把活得好好的浏览器当成死了。以 CDP 连接状态为准。
        if self._closed:
            return False
        return not self._cdp.closed

    # -- 主页面 ------------------------------------------------------------

    async def eval(
        self, expression: str, *, await_promise: bool = False, timeout: float = 25
    ) -> Any:
        return await self._cdp.js(expression, await_promise=await_promise, timeout=timeout)

    async def navigate(self, url: str, *, timeout: float = 25) -> None:
        """导航主页面并等文档加载完（轮询 readyState）。"""
        if not self.alive:
            raise BrowserError("浏览器进程已经不在了")
        resp = await self._cdp.send("Page.navigate", {"url": url}, timeout=min(20.0, timeout))
        err = str((resp.get("error") or {}).get("message") or "")
        err_text = str((resp.get("result") or {}).get("errorText") or "")
        text = err or err_text
        if text and "ERR_ABORTED" not in text:
            raise BrowserError(f"页面打不开：{text}")
        await self._wait_ready(timeout)

    async def _wait_ready(self, timeout: float) -> None:
        """轮询到 DOM 可用（readyState 是 ``interactive`` 或 ``complete``）。

        为什么不等 complete：``complete`` 要等图片/字体/统计脚本全部下完，走代理时
        实测能多等 15 秒以上，而我们只需要 document.body 存在（好注入表单、读卡片）。
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + max(1.0, timeout)
        while loop.time() < end:
            try:
                state = await self.eval("document.readyState", timeout=4)
            except BrowserError:
                state = ""
            if state in ("interactive", "complete"):
                return
            await asyncio.sleep(0.15)

    async def set_file_input(
        self, selector: str, files: list[str], *, timeout: float = 10.0
    ) -> None:
        """往 ``<input type=file>`` 里塞本地文件。

        这是给 lens 上传用的扩展接口：给 file input 赋值属于浏览器安全边界内的操作，
        页面 JS 改不了（把 files 设成任意路径被禁止），只能走 DOM.setFileInputFiles。
        注意文件路径是**浏览器进程自己去读**的，所以临时图片必须真的落在磁盘上。
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + max(1.0, timeout)
        node_id = 0
        while loop.time() < end:
            doc = await self._cdp.send("DOM.getDocument", {"depth": 1}, timeout=5)
            root = int(((doc.get("result") or {}).get("root") or {}).get("nodeId") or 0)
            if root:
                query = await self._cdp.send(
                    "DOM.querySelector", {"nodeId": root, "selector": selector}, timeout=5
                )
                node_id = int(((query.get("result") or {}).get("nodeId")) or 0)
            if node_id:
                break
            await asyncio.sleep(0.2)
        if not node_id:
            raise BrowserError(f"页面里等不到文件上传框（{selector}）")

        real = [str(p) for p in files if Path(str(p)).is_file()]
        if not real:
            raise BrowserError("待上传的临时图片不存在了")
        resp = await self._cdp.send(
            "DOM.setFileInputFiles", {"files": real, "nodeId": node_id}, timeout=10
        )
        if resp.get("error"):
            raise BrowserError(f"设置上传文件失败：{_short(resp['error'])}")

    # -- 标签页 ------------------------------------------------------------

    async def new_tab(self) -> Tab:
        """开一个新标签页，返回带独立 CDP 连接的 Tab。"""
        if not self.alive:
            raise BrowserError("浏览器进程已经不在了")
        ws_url, owned = await self._new_target_ws()
        cdp = await _CDP.connect(ws_url, timeout=10)
        try:
            await cdp.send("Page.enable", timeout=8)
            # Network 域只为抓 302 目标存在（见 Tab.listen），失败不影响主流程。
            with contextlib.suppress(BrowserError):
                await cdp.send("Network.enable", timeout=8)
            # 每个新标签页都要重新伪装：UA 覆盖和脚本注入都不跨 target。
            await apply_stealth(cdp, reason="新标签页")
        except Exception:
            await cdp.close()
            raise
        return Tab(cdp, self, owned=owned)

    async def _new_target_ws(self) -> tuple[str, bool]:
        """拿到新标签页的 ws 地址。

        首选 PUT /json/new?about:blank（PUT 是必须的，GET 在部分版本上返回 405）。
        拿不到就退回 /json/list 里最后一个 page target —— 这种情况会复用已有标签页，
        只能「借用」，关闭时不能真关掉（owned=False）。
        """
        session = await self._http_session()
        try:
            async with session.request(
                "PUT",
                f"{self._base_url}/json/new?about:blank",
                proxy=None,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status < 300:
                    data = await resp.json(content_type=None)
                    url = str((data or {}).get("webSocketDebuggerUrl") or "")
                    if url:
                        return url, True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.debug("PUT /json/new 失败，准备退回 /json/list：%s", exc)

        pages = await self._page_targets()
        for item in reversed(pages):
            url = str(item.get("webSocketDebuggerUrl") or "")
            if url and url != self._main_ws_url:
                return url, False
        raise BrowserError("开不出新标签页（调试端口不认 /json/new）")

    async def _page_targets(self) -> list[dict[str, Any]]:
        session = await self._http_session()
        try:
            async with session.get(
                f"{self._base_url}/json/list",
                proxy=None,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise BrowserError(f"读不到浏览器标签页列表：{exc}") from exc
        items = data if isinstance(data, list) else (data or {}).get("items") or []
        return [t for t in items if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]

    async def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            # trust_env=False：本地回环 / 内网调试端口绝不能走宿主的 http_proxy。
            connector = aiohttp.TCPConnector(ssl=False) if self._insecure else None
            self._http = aiohttp.ClientSession(trust_env=False, connector=connector)
        return self._http

    async def set_cookies(self, raw: str, *, domain: str = ".google.com") -> int:
        """把一串 `name=value; name2=value2` 注入浏览器会话。

        用途：用户在自己的浏览器里过过一次谷歌验证后，把 google.com 的 cookie 抄过来，
        插件带着这套 cookie 查询就不会被当成陌生访客，验证码概率显著下降（登录态更明显）。

        限制要说清楚：谷歌的反滥用 cookie 是和 IP 绑定的，所以必须在**同一个出口 IP**
        下导出/使用；换了代理，这些 cookie 基本失效。
        """
        text = (raw or "").strip()
        if not text:
            return 0
        ok = 0
        for chunk in text.split(";"):
            if "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            try:
                resp = await self._cdp.send(
                    "Network.setCookie",
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                    },
                    timeout=5,
                )
            except BrowserError:
                continue
            if (resp.get("result") or {}).get("success"):
                ok += 1
        logger.info("已注入 %d 个 cookie（%s）", ok, domain)
        return ok

    async def cookie_header(self, domain_suffix: str) -> str:
        """把 profile 里属于某个域后缀的 cookie 拼成 Cookie 头。

        用途：lens 解析 /goto 时改走 aiohttp（比开标签页快一个数量级），但那次请求
        必须带浏览器同一套 cookie，否则谷歌会当成陌生会话直接给验证页。
        """
        resp = await self._cdp.send("Storage.getCookies", timeout=8)
        cookies = ((resp.get("result") or {}).get("cookies")) or []
        parts: list[str] = []
        seen: set[str] = set()
        for item in cookies:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").lstrip(".").lower()
            name = str(item.get("name") or "")
            if not name or name in seen:
                continue
            if not (domain == domain_suffix or domain.endswith("." + domain_suffix)):
                continue
            seen.add(name)
            parts.append(f"{name}={item.get('value') or ''}")
        return "; ".join(parts)

    # -- 关闭 --------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self.external:
            # 外部浏览器不归我们管：只关掉自己开的那张标签页，然后断开连接。
            # 绝对不能发 Browser.close —— 那会把别人（或另一个容器）正在用的浏览器关掉。
            with contextlib.suppress(Exception):
                await self._cdp.send("Page.close", timeout=3)
            await self._cdp.close()
            if self._http is not None:
                with contextlib.suppress(Exception):
                    await self._http.close()
                self._http = None
            logger.debug("已断开外部浏览器（%s）", self._base_url)
            return

        # 先让浏览器体面退出。必须走 CDP 的 Browser.close：Windows 上 Edge 启动后会
        # 「交棒」给真正的浏览器进程，我们手里的 proc 只是启动器，kill 它关不掉浏览器。
        with contextlib.suppress(Exception):
            await self._cdp.send("Browser.close", timeout=5)
        await self._cdp.close()
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.close()
            self._http = None

        proc = self._proc
        with contextlib.suppress(Exception):
            if proc.returncode is None:
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)

        # 兜底：Browser.close 没生效（比如 CDP 已经先断）时按端口找出真凶杀掉，
        # 否则每重启一次就漏一个无头浏览器在后台吃内存。
        await asyncio.sleep(0.4)
        if await self._debug_port_alive():
            logger.debug("Browser.close 没关干净，按端口强杀（port=%s）", self._port)
            await self._kill_port_owner()

        # 顺手清锁文件：下次启动不会被「profile 已被占用」挡住。
        _clean_profile_locks(self.profile_dir)
        if self._temp_profile:
            shutil.rmtree(self.profile_dir, ignore_errors=True)
        logger.debug("浏览器已关闭（port=%s）", self._port)

    async def _debug_port_alive(self) -> bool:
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.get(
                    f"{self._base_url}/json/version",
                    proxy=None,
                    timeout=aiohttp.ClientTimeout(total=1.5),
                ) as resp:
                    return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def _kill_port_owner(self) -> None:
        """按调试端口找到监听进程并强杀（仅 Windows；Linux 上 proc.kill 就是真进程）。"""
        if os.name != "nt":
            return
        try:
            netstat = await asyncio.create_subprocess_exec(
                "netstat",
                "-ano",
                "-p",
                "TCP",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            raw, _ = await asyncio.wait_for(netstat.communicate(), 5)
        except (OSError, asyncio.TimeoutError):
            return
        needle = f"127.0.0.1:{self._port}"
        pid = ""
        for line in raw.decode("utf-8", "replace").splitlines():
            if needle in line and "LISTENING" in line.upper():
                pid = line.split()[-1]
                break
        if not pid.isdigit():
            return
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/PID",
                pid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), 5)


def _clean_profile_locks(profile_dir: Path) -> None:
    for name in _PROFILE_STALE:
        target = profile_dir / name
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists() or target.is_symlink():
                target.unlink()
        except OSError:
            # Windows 上刚 kill 的进程可能还捏着文件句柄，删不掉不影响下次启动。
            continue


# ── 启动 ────────────────────────────────────────────────────────────────────


def _is_headless_shell(exe: str) -> bool:
    """Playwright 的 `chromium_headless_shell` 是精简版：天生无头，且不认 --headless=new。"""
    name = Path(str(exe)).name.lower()
    return "headless_shell" in name or "headless-shell" in name


def _build_args(exe: str, proxy: str, profile_dir: Path, headless: bool) -> list[str]:
    args = [exe]
    if proxy:
        # 代理只能通过启动参数给，运行中改不了 → get_browser 换代理时必须重启浏览器。
        args.append(f"--proxy-server={proxy}")
    if headless:
        # headless_shell 本来就是无头的，多传一个 --headless=new 在部分版本上会直接报错退出
        if not _is_headless_shell(exe):
            args.append("--headless=new")
    else:
        # 有头模式把窗口挪到屏幕外：用户自己在服务器桌面上过验证码 / Cookie 同意页时用。
        args += ["--window-position=-2400,-2400"]
    args += [
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1440,1400",
        f"--user-agent={USER_AGENT}",
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        "about:blank",
    ]
    if os.name != "nt":
        # Docker 里通常以 root 跑，Chromium 的沙箱在 root 下会直接退出码 1；
        # /dev/shm 小也是容器里的经典坑。
        args[-1:-1] = ["--no-sandbox", "--disable-dev-shm-usage"]
    return args


async def _wait_port(proc: asyncio.subprocess.Process, profile_dir: Path) -> int:
    """等 ``DevToolsActivePort`` 出现，返回真实调试端口。

    注意 Windows 的特例：被 CreateProcess 起来的 ``msedge.exe`` 会**立刻以 0 退出**并
    把活交给真正的浏览器进程（实测：端口文件比进程退出晚约 0.6 秒才写出来）。
    所以进程退出**不能**当成启动失败，只能当「再看看」的信号 —— 真失败的情况是
    端口文件一直不出现。
    """
    port_file = profile_dir / "DevToolsActivePort"
    exited_at = -1.0
    for tick in range(150):  # 150 × 150ms ≈ 22 秒，冷启动实测 1~2 秒，留足余量
        try:
            if port_file.exists():
                lines = port_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                if lines and lines[0].strip().isdigit():
                    return int(lines[0].strip())
        except OSError:
            pass
        if proc.returncode is not None and exited_at < 0:
            exited_at = time.monotonic()
        # 进程已经退了、端口文件又迟迟不出现：多半是真失败了，但也可能是机器很忙时
        # Edge 交棒出去的进程迟迟没起来，所以给 12 秒宽限再判死刑。
        if exited_at > 0 and time.monotonic() - exited_at > 12:
            hint = ""
            if os.name != "nt":
                hint = "（容器里常见原因：缺 --no-sandbox、/dev/shm 太小、没装字体库）"
            raise BrowserError(
                f"浏览器启动即退出（退出码 {proc.returncode}，profile={profile_dir}）{hint}"
            )
        await asyncio.sleep(0.15)
    raise BrowserError("浏览器 22 秒内没打开调试端口（可能被安全软件拦了，或 profile 被占用）")


async def _launch(exe: str, proxy: str, profile_dir: str, headless: bool) -> Browser:
    temp_profile = not (profile_dir or "").strip()
    prof = (
        Path(tempfile.mkdtemp(prefix="fishbrowser_"))
        if temp_profile
        else Path(profile_dir).expanduser()
    )
    try:
        prof.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrowserError(f"profile 目录建不出来（{prof}）：{exc}") from exc
    _clean_profile_locks(prof)

    args = _build_args(exe, proxy, prof, headless)
    logger.debug("启动浏览器：%s", " ".join(args))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise BrowserError(f"浏览器可执行文件不存在：{exe}") from exc
    except OSError as exc:
        raise BrowserError(f"浏览器启动失败（{exe}）：{exc}") from exc

    try:
        port = await _wait_port(proc, prof)
        ws_url = ""
        for _ in range(80):  # 80 × 200ms = 16 秒，等第一个 page target 出现
            try:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    async with session.get(
                        f"http://127.0.0.1:{port}/json/list",
                        proxy=None,
                        timeout=aiohttp.ClientTimeout(total=4),
                    ) as resp:
                        data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                data = None
            items = data if isinstance(data, list) else (data or {}).get("items") or []
            for item in items:
                if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                    ws_url = str(item["webSocketDebuggerUrl"])
                    break
            if ws_url:
                break
            await asyncio.sleep(0.2)
        if not ws_url:
            raise BrowserError("浏览器起来了但没有任何页面 target")

        cdp = await _CDP.connect(ws_url, timeout=15)
        try:
            await cdp.send("Page.enable", timeout=10)
            await cdp.send("DOM.enable", timeout=10)
            await apply_stealth(cdp, reason="本地浏览器")
        except Exception:
            await cdp.close()
            raise
    except BaseException:
        # 任何一步失败都要把半死的进程收掉，否则会留下一个占着端口的孤儿 Edge。
        with contextlib.suppress(Exception):
            if proc.returncode is None:
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        if temp_profile:
            shutil.rmtree(prof, ignore_errors=True)
        raise

    browser = Browser(
        proc=proc,
        port=port,
        cdp=cdp,
        exe=exe,
        proxy=proxy,
        profile_dir=prof,
        temp_profile=temp_profile,
    )
    browser._main_ws_url = ws_url
    logger.info("无头浏览器就绪（pid=%s port=%s）", proc.pid, port)
    return browser


# ── 外部浏览器（独立 Chrome 容器 / 宿主机上的 Chrome） ─────────────────────

_DEFAULT_CDP_PORT = 9222


def normalize_cdp_base(cdp_url: str) -> tuple[str, str, int]:
    """把用户填的 CDP 地址收敛成 (base_url, host, port)。

    允许这些写法（都会归一成 `http://host:9222`）：
        chrome:9222
        http://chrome:9222
        http://chrome:9222/
        http://chrome:9222/json/version
        ws://chrome:9222/devtools/browser/<id>   ← 有人会从 /json/version 里直接复制这条
    没写端口就按 Chrome 的默认调试端口 9222 处理。
    """
    raw = (cdp_url or "").strip().strip('"').strip("'")
    if not raw:
        raise BrowserError("外部浏览器地址是空的")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https", "ws", "wss"):
        raise BrowserError(f"外部浏览器地址只支持 http/https/ws/wss：{cdp_url}")
    host = parts.hostname or ""
    if not host:
        raise BrowserError(f"外部浏览器地址里没有主机名：{cdp_url}")
    port = parts.port or _DEFAULT_CDP_PORT
    scheme = "https" if parts.scheme in ("https", "wss") else "http"
    return f"{scheme}://{host}:{port}", host, port


async def _connect_remote(
    cdp_url: str, *, headless: bool = True, insecure: bool = False
) -> Browser:
    """连一个已经在跑的浏览器（不启动进程、不碰它的 profile）。

    典型场景：AstrBot 跑在一个容器里，Chrome 跑在另一个容器里，两边通过调试端口通信。
    这种模式下：

    * 代理由**外部浏览器自己**决定（插件给的 `--proxy-server` 用不上）；
    * 插件不会关掉浏览器，只在结束时关掉自己开的那张标签页（见 Browser.close）；
    * 浏览器容器必须以 `--remote-debugging-address=0.0.0.0` 启动，否则只监听它自己的
      127.0.0.1，端口映射也连不进去（这是最常见的坑）。
    """
    base, host, port = normalize_cdp_base(cdp_url)
    # 只有 https 才涉及证书；http 的 ssl 参数没有意义
    ssl_arg = False if (insecure and base.startswith("https://")) else None
    logger.info("连接外部浏览器 CDP：%s%s", base, "（忽略证书校验）" if ssl_arg is False else "")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10), trust_env=False
    ) as session:
        # 先确认端口通、而且真的是 Chrome 调试端口（而不是随便一个 HTTP 服务）
        try:
            async with session.get(f"{base}/json/version", proxy=None, ssl=ssl_arg) as resp:
                if resp.status != 200:
                    raise BrowserError(
                        f"外部浏览器返回 HTTP {resp.status}（{base}/json/version），"
                        "确认它是以 --remote-debugging-port 启动的"
                    )
                version = await resp.json(content_type=None)
        except BrowserError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise BrowserError(
                f"连不上外部浏览器 {base}：{exc}。请确认①浏览器已启动并带 "
                "--remote-debugging-port=9222 ②带 --remote-debugging-address=0.0.0.0 "
                "③AstrBot 容器能访问这个地址（同一 docker 网络 / host 网络）"
            ) from exc

        # 自己开一张独占标签页：不借用别人的页面，也就不会把别人的页面导航走。
        ws_url = ""
        try:
            async with session.request(
                "PUT", f"{base}/json/new?about:blank", proxy=None, ssl=ssl_arg
            ) as resp:
                if resp.status < 300:
                    data = await resp.json(content_type=None)
                    ws_url = str((data or {}).get("webSocketDebuggerUrl") or "")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.debug("PUT /json/new 失败，退回已有标签页：%s", exc)

        owned = bool(ws_url)
        if not ws_url:
            try:
                async with session.get(f"{base}/json/list", proxy=None, ssl=ssl_arg) as resp:
                    items = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                raise BrowserError(f"外部浏览器读不到标签页列表：{exc}") from exc
            pages = [
                t
                for t in (items if isinstance(items, list) else [])
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            ]
            if not pages:
                raise BrowserError("外部浏览器里没有任何标签页，也开不出新的（检查是否 --headless）")
            ws_url = str(pages[0]["webSocketDebuggerUrl"])
            logger.debug("复用外部浏览器已有标签页（不是新建的）")

    cdp = await _CDP.connect(ws_url, timeout=15, insecure=insecure)
    try:
        await cdp.send("Page.enable", timeout=10)
        await cdp.send("DOM.enable", timeout=10)
        with contextlib.suppress(BrowserError):
            await cdp.send("Network.enable", timeout=8)
        # Playwright / 无头 Chromium 的默认 UA 带 "HeadlessChrome"，谷歌会直接给验证码。
        # 这两步（UA 覆盖 + 指纹补丁）是外部浏览器能不能用的关键。
        await apply_stealth(cdp, reason="外部浏览器")
    except Exception:
        await cdp.close()
        raise

    browser = Browser(
        proc=None,
        port=port,
        cdp=cdp,
        exe="",
        proxy="",
        profile_dir=Path(""),
        temp_profile=False,
        host=host,
        external=True,
        insecure=insecure,
    )
    browser._main_ws_url = ws_url
    browser.external_target_owned = owned
    logger.info(
        "外部浏览器就绪（%s，%s）", base, (version or {}).get("Browser") or "版本未知"
    )
    return browser


# ── 进程内单例 ──────────────────────────────────────────────────────────────

_instance: Browser | None = None
_instance_key: tuple[str, str, bool] = ("", "", True)
# asyncio.Lock 在 3.10+ 是「首次使用时绑定事件循环」，插件被重载后循环会换一个，
# 老 Lock 再 await 就会抛 "attached to a different loop"。按循环分开存最省事。
_locks: dict[int, asyncio.Lock] = {}


def _lock_for_current_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = id(loop)
    lock = _locks.get(key)
    if lock is None:
        if len(_locks) > 4:  # 循环换得太频繁说明有别的 bug，别无限攒
            _locks.clear()
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def get_browser(
    *,
    proxy: str = "",
    exe: str = "",
    profile_dir: str = "",
    headless: bool = True,
    cdp_url: str = "",
    cdp_insecure: bool = False,
) -> Browser:
    """拿浏览器单例；proxy / exe / headless / cdp_url 变了就重连（或重启）。

    两种模式：
    * `cdp_url` 为空 → 本地起一个无头浏览器（自己管进程、自己管 profile）；
    * `cdp_url` 非空 → 连一个已经在跑的浏览器（独立 Chrome 容器 / 宿主机 Chrome），
      插件不启动也不关闭它，只借用它的调试端口。

    为什么要单例：一次查询的冷启动是 1~2 秒，而且 Edge 会派生十几个子进程，
    每次查询都起一个的话，连续搜图很容易把机器拖死。
    """
    global _instance, _instance_key

    cdp_url = (cdp_url or "").strip()
    if cdp_url:
        base, _host, _port = normalize_cdp_base(cdp_url)
        key = ("cdp", base, bool(headless), bool(cdp_insecure))
        async with _lock_for_current_loop():
            if _instance is not None:
                if _instance.alive and _instance_key == key:
                    return _instance
                logger.debug("重连外部浏览器（配置变了或连接已断）")
                await _instance.close()
                _instance = None
            _instance = await _connect_remote(
                cdp_url, headless=headless, insecure=cdp_insecure
            )
            _instance_key = key
            return _instance

    exe = (exe or "").strip().strip('"') or find_browser_exe("")
    if exe and not headless and _is_headless_shell(exe):
        raise BrowserError(
            "browser_headless 关掉了，但选中的是 Playwright 的 headless_shell（精简版，天生无头）。"
            "要么把 browser_headless 打开，要么装完整版 Chromium 并把 browser_exe 指过去"
            "（ms-playwright 目录下的 chromium-*/chrome-linux/chrome）"
        )
    if not exe:
        raise BrowserError(
            "没找到可用的浏览器：请装 Edge 或 Chrome，"
            "或在插件配置里把 browser_exe 指向浏览器可执行文件的绝对路径"
        )
    proxy = (proxy or "").strip()
    headless = bool(headless)
    key = (proxy, exe, headless)

    async with _lock_for_current_loop():
        if _instance is not None:
            if _instance.alive and _instance_key == key:
                return _instance
            reason = "已退出" if not _instance.alive else "代理/浏览器路径/有头模式变了"
            logger.debug("重启浏览器（%s）", reason)
            await _instance.close()
            _instance = None
        _instance = await _launch(exe, proxy, profile_dir, headless)
        _instance_key = key
        return _instance


async def close_browser() -> None:
    """插件 terminate() 调用；可重复调用。"""
    global _instance, _instance_key
    async with _lock_for_current_loop():
        if _instance is not None:
            await _instance.close()
            _instance = None
            _instance_key = ("", "", True)
