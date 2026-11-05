"""谷歌 Lens 以图搜图：图片 → 找到原帖 / 作者主页（最终目标是拿到 X 账号）。

这里是纯 Python 实现，只依赖 aiohttp + 标准库，靠 core/browser.py 的极简 CDP 客户端
驱动无头 Edge/Chrome。所有「看起来绕」的地方都是踩出来的，改之前请先看注释。

两个最容易踩死的坑（实测结论，务必保留）：

1. **上传必须在浏览器进程内完成。**
   用 aiohttp 先 POST https://lens.google.com/v3/upload 拿 303 的 Location、再让浏览器
   打开那个 URL 是**错的** —— 结果页会显示「没有与您的搜索相匹配的内容」，因为上传会话
   是跟浏览器 cookie 绑的，aiohttp 那边传出来的 vsrid 在浏览器里不认。
   正确姿势：浏览器打开 google.com，注入一个隐藏 multipart form 指向 v3/upload，
   用 CDP 的 DOM.setFileInputFiles 给 file input 塞本地文件，再 form.submit()。

2. **结果卡片的地址被包了一层 /goto?url=<opaque>。**
   href="/goto?url=…" 里的 token 是 protobuf base64，解不出来，只能真去访问
   https://www.google.com/goto?url=<token> 让它自己 302 到真实地址；
   单条 1.3~4.4 秒，所以要开 2~3 个标签页并发解析，否则一次查询光解析就几十秒。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import aiohttp

from .browser import (
    USER_AGENT,
    Browser,
    BrowserError,
    find_browser_exe,
    get_browser,
)

logger = logging.getLogger(__name__)

__all__ = ["LensError", "browser_available", "lens_search"]

GOOGLE_HOME = "https://www.google.com/"
LENS_UPLOAD = "https://lens.google.com/v3/upload"

# 社交平台域名：命中的卡片排最前面，因为我们的目标就是「这个人是谁、号在哪」。
SOCIAL_HOSTS = (
    "x.com",
    "twitter.com",
    "instagram.com",
    "weibo.com",
    "weibo.cn",
    "bilibili.com",
    "xiaohongshu.com",
    "tiktok.com",
    "facebook.com",
    "threads.net",
)

# title 里的社交平台线索。单/双字符的 "X"、"ins" 用边界匹配，
# 否则 "Xiaohongshu"、"inside" 之类会全被误判成社交卡片。
#
# 这里**故意不含 facebook**：实测 Lens 结果页上大量转载视频的标题都以 "| Facebook" 结尾，
# 一旦把 facebook 算作文本信号，13 张卡片会被判成社交而挤掉维基百科/百科/美术馆这些真正
# 有用的来源。facebook 只在**解析出 host 之后**参与排序（见 _build_results），那时才准。
SOCIAL_TEXT_RE = re.compile(
    r"(?:twitter|x\.com|instagram|threads\.net|tiktok|"
    r"微博|推特|哔哩哔哩|小红书|抖音|bilibili)",
    re.I,
)
_SOCIAL_WORD_RE = re.compile(r"(?<![0-9A-Za-z_])(?:ins|X|b站)(?![0-9A-Za-z_])", re.I)

# 谷歌自己的 5xx 页面（实测 502：「The server encountered a temporary error」）。
# 命中它说明是上游故障，不是「这张图搜不到」，两者要给用户不同的结论。
_UPSTREAM_ERROR_RE = re.compile(
    r"The server encountered a temporary error|That.s an error\.|"
    r"\b50[234]\b.*(?:error|unavailable)|服务暂时不可用",
    re.I,
)

CAPTCHA_TEXT_RE = re.compile(
    r"unusual traffic|我们的系统检测到|automated queries|not a robot", re.I
)
NET_ERROR_RE = re.compile(
    r"net::ERR_|ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|ERR_TUNNEL|ERR_PROXY|ERR_TIMED_OUT",
    re.I,
)

_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

# 卡片文本形如 "維基百科File:Mona Lisa, by …"、"X阿薰kaOriบน X: …" ——
# 站点名和标题是直接拼在一起的，靠已知站名前缀兜底切。
_KNOWN_SITES = (
    "維基百科",
    "维基百科",
    "Wikipedia",
    "X",
    "Twitter",
    "Instagram",
    "Pinterest",
    "Facebook",
    "YouTube",
    "Reddit",
    "TikTok",
    "抖音",
    "微博",
    "哔哩哔哩",
    "Bilibili",
    "小红书",
    "Pixiv",
    "Danbooru",
    "Gelbooru",
    "SauceNAO",
    "Imgur",
    "Tumblr",
    "DeviantArt",
    "ArtStation",
    "Threads",
)

# ── 页面脚本 ────────────────────────────────────────────────────────────────

# 注入上传表单。为什么不用谷歌页面自己的上传按钮：那是 <input> 隐藏 + 一堆 JS，
# 点击路径极不稳定；自己造一个 form 直接 POST 到 v3/upload 最省事，域名和 cookie 都对。
PREPARE_FORM_JS = """(function(){
  if (document.getElementById('fishLensForm')) return 'exists';
  var f = document.createElement('form');
  f.id = 'fishLensForm';
  f.method = 'POST';
  f.enctype = 'multipart/form-data';
  f.action = 'https://lens.google.com/v3/upload';
  f.target = '_self';
  f.style.display = 'none';
  var i = document.createElement('input');
  i.type = 'file';
  i.name = 'encoded_image';
  i.id = 'fishLensFile';
  f.appendChild(i);
  document.body.appendChild(f);
  return 'ok';
})()"""

# 抽卡片。只认 a[href*="/goto?url="]：结果页里真正的「来源站点」卡片都是这个形状，
# 图片本身的 data: / 谷歌自家链接都不会带 /goto，天然过滤掉了。
COLLECT_CARDS_JS = r"""(function(){
  var out = [];
  var nodes = document.querySelectorAll('a[href*="/goto?url="]');
  for (var i = 0; i < nodes.length; i++) {
    var a = nodes[i];
    var href = a.getAttribute('href') || '';
    var m = href.match(/[?&]url=([^&]+)/);
    if (!m) continue;
    var box = a.closest('div[data-ved]') || a.parentElement;
    var raw = box ? (box.innerText || '') : '';
    var lines = raw.split('\n')
      .map(function(s) { return s.trim(); })
      .filter(function(s) { return s.length > 0; });
    out.push({
      token: m[1],
      label: (a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim(),
      title: (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200),
      site: lines.length > 1 ? lines[0].slice(0, 40) : '',
      text: raw.replace(/\s+/g, ' ').trim().slice(0, 240)
    });
  }
  return JSON.stringify(out);
})()"""

# 页面状态：URL（判验证码/是否真到了结果页）、正文摘要（诊断用）、
# 以及「完全匹配(48) / 外观匹配(44)」两个备用标签页链接。
PAGE_INFO_JS = r"""(function(){
  var tabs = [];
  var nodes = document.querySelectorAll('a[href*="udm="]');
  for (var i = 0; i < nodes.length; i++) {
    var h = nodes[i].getAttribute('href') || '';
    var m = h.match(/[?&]udm=(4[48])(?:&|$)/);
    if (m) {
      tabs.push({
        udm: m[1],
        href: h,
        text: (nodes[i].textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24)
      });
    }
  }
  return JSON.stringify({
    url: location.href,
    body: document.body ? document.body.innerText.slice(0, 1500) : '',
    tabs: tabs
  });
})()"""

# 解析 token 时每拍读一次：当前真实 URL + 页面有没有 meta refresh 的自跳地址。
#
# 为什么需要这个：/goto 请求太密集时，谷歌不返回 302，而是回一个「正在验证您的请求」
# 的节流页 —— 里面只有一条 <meta http-equiv="refresh" content="5;url=/goto?url=<新token>">，
# 等 5 秒再带新 token 重试。实测这是 token 解析「卡死 7 秒然后失败」的真正原因。
RESOLVE_PROBE_JS = r"""(function(){
  var refresh = '';
  var meta = document.querySelector('meta[http-equiv="refresh" i]');
  if (meta) {
    var content = meta.getAttribute('content') || '';
    var at = content.toLowerCase().indexOf('url=');
    if (at >= 0) refresh = content.slice(at + 4).trim();
  }
  return JSON.stringify({ href: location.href, refresh: refresh });
})()"""


class LensError(RuntimeError):
    """Lens 查询失败。``code`` 供上层分支出不同的用户提示。

    code 取值：CAPTCHA（要验证）/ UNREACHABLE（连不上谷歌）/ NO_BROWSER（没浏览器）/
    BROWSER（浏览器其它故障）/ UPLOAD（上传环节失败）/ TIMEOUT（整体超时）。
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# ── 对外入口 ────────────────────────────────────────────────────────────────


def browser_available(exe: str = "", cdp_url: str = "") -> bool:
    """宿主机上有没有可用的 Chrome/Edge。

    插件层在开跑之前先问一句，能省掉一次「启动失败」的无效等待，
    也能把「没装浏览器」和「Lens 被谷歌拦了」这两种失败区分开。
    """
    if (cdp_url or "").strip():
        # 接外部浏览器时本地不需要任何可执行文件，能不能用由连接结果说了算。
        return True
    return bool(find_browser_exe((exe or "").strip().strip('"')))


async def lens_search(
    content: bytes,
    mime_type: str,
    *,
    proxy: str = "",
    browser_exe: str = "",
    cdp_url: str = "",
    cdp_insecure: bool = False,
    google_cookie: str = "",
    profile_dir: str = "",
    timeout: float = 45.0,
    max_resolve: int = 10,
    headless: bool = True,
    on_log: Callable[[str], None] | None = None,
) -> list[dict]:
    """拿一张图去谷歌 Lens 搜，返回候选来源列表（已按「社交优先」排序、按 URL 去重）。

    返回每条的字段与项目其它引擎完全一致：
    engine / similarity / title / sourceUrl / authorUrl / host / snippet / links / siteName。
    解析不出真实地址的卡片会被丢掉 —— 只给出 /goto?url=<opaque> 对调用方没有任何用处。
    """
    log = _make_logger(on_log)
    if not content:
        raise LensError("图片内容是空的，没法拿去搜", code="UPLOAD")

    timeout = max(10.0, float(timeout))
    max_resolve = max(0, int(max_resolve))
    started = time.monotonic()
    budget_end = started + timeout

    tmpdir = Path(tempfile.mkdtemp(prefix="fishlens_"))
    image_path = tmpdir / ("upload" + _ext_for(mime_type))
    try:
        # 必须落盘：DOM.setFileInputFiles 给的是路径，浏览器进程自己去读这个文件。
        image_path.write_bytes(content)

        t0 = time.monotonic()
        try:
            browser = await asyncio.wait_for(
                get_browser(
                    proxy=proxy,
                    exe=browser_exe,
                    profile_dir=profile_dir,
                    headless=headless,
                    cdp_url=cdp_url,
                    cdp_insecure=cdp_insecure,
                ),
                min(35.0, timeout + 5),
            )
        except asyncio.TimeoutError as exc:
            raise LensError("浏览器启动超时", code="NO_BROWSER") from exc
        except BrowserError as exc:
            raise _as_lens_error(exc) from exc
        log(f"浏览器就绪（{time.monotonic() - t0:.1f}s）")
        if (google_cookie or "").strip():
            # 每次查询前都注入一次：浏览器可能被重启，cookie 也就没了。
            injected = await browser.set_cookies(google_cookie)
            log(f"已注入 {injected} 个 google cookie")

        try:
            cards = await asyncio.wait_for(
                _search_in_page(browser, image_path, budget_end, log),
                max(6.0, budget_end - time.monotonic()),
            )
        except asyncio.TimeoutError as exc:
            raise LensError(f"谷歌 Lens 查询超时（超过 {timeout:.0f} 秒）", code="TIMEOUT") from exc
        except LensError:
            raise
        except BrowserError as exc:
            raise _as_lens_error(exc) from exc
        except Exception as exc:  # 兜底：任何意外都要变成带中文说明的 LensError
            raise LensError(f"谷歌 Lens 查询失败：{exc}", code="UPLOAD") from exc

        log(f"拿到 {len(cards)} 张卡片（{time.monotonic() - started:.1f}s）")
        picked = _pick_cards(cards, max_resolve)
        if not picked:
            return []

        t1 = time.monotonic()
        resolved = await _resolve_via_http(browser, picked, proxy, budget_end, log)
        used = "HTTP"
        if not resolved and budget_end - time.monotonic() > 8:
            # HTTP 路径没解全（cookie 缺失、被限流吃光预算等），剩下的用标签页兜底。
            log(f"HTTP 只解析出 {len(resolved)} 条，剩余用标签页兜底")
            rest_pairs = [(i, c) for i, c in enumerate(picked) if i not in resolved]
            extra = await _resolve_cards(browser, [c for _, c in rest_pairs], budget_end, log)
            for offset, url in extra.items():
                resolved[rest_pairs[offset][0]] = url
            used = "HTTP+标签页"
        log(f"解析出 {len(resolved)}/{len(picked)} 个真实地址（{used}，"
            f"{time.monotonic() - t1:.1f}s）")
        results = _build_results(picked, resolved)
        log(f"总耗时 {time.monotonic() - started:.1f}s，返回 {len(results)} 条")
        return results
    finally:
        # 临时图片用完必须删：一次查询一张图，不删的话 /tmp 会被图片塞满。
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── 上传 + 抽卡片 ───────────────────────────────────────────────────────────


async def _search_in_page(
    browser: Browser, image_path: Path, budget_end: float, log: Callable[[str], None]
) -> list[dict]:
    """在浏览器主页面里完成「打开谷歌 → 上传 → 等结果页卡片」。

    整段拿 browser.page_lock 串行化：上传过程会连续导航主页面，
    两个查询并发跑必然互相把页面踢走。
    """
    async with browser.page_lock:
        home_budget = max(8.0, min(25.0, budget_end - time.monotonic()))
        await browser.navigate(GOOGLE_HOME, timeout=home_budget)
        info = await _page_info(browser)
        _guard_captcha(info)

        await browser.eval(PREPARE_FORM_JS, timeout=10)
        await browser.set_file_input("#fishLensFile", [str(image_path)], timeout=10)

        try:
            await browser.eval(
                "document.getElementById('fishLensForm').submit()", timeout=8
            )
        except BrowserError as exc:
            # 提交会立刻触发导航，执行上下文被销毁时这条命令可能收不到响应，属正常现象。
            log(f"提交图片时连接被导航打断（正常）：{exc}")
        log("已提交图片，等结果页…")

        t0 = time.monotonic()
        cards, info = await _wait_cards(browser, budget_end, log, min_cards=3, first_wait=14.0)
        log(f"结果页出卡片耗时 {time.monotonic() - t0:.1f}s（{len(cards)} 张）")

        if cards:
            return cards

        # 落地页（udm=26）空了才去点「完全匹配 48 / 外观匹配 44」兜底标签页。
        log(f"落地页 0 卡片，URL={str(info.get('url') or '')[:140]}")
        body = re.sub(r"\s+", " ", str(info.get("body") or ""))[:220]
        if body:
            log(f"页面文本：{body}")
        if info.get("error"):
            # 页面根本读不到，兜底标签页也没意义 —— 直接报错，比返回空列表让人以为
            # 「这张图网上没有」要诚实得多。
            raise LensError(f"结果页打不开：{info['error']}", code="UPLOAD")
        if _UPSTREAM_ERROR_RE.search(body):
            # 谷歌自己挂了（实测见过 502 「The server encountered a temporary error」）。
            # 这时候卡片是 0 张，但真相不是「图搜不到」—— 必须分开说，否则用户会一直
            # 换图重试，白折腾。
            raise LensError(
                "谷歌服务暂时出错（502/临时错误），和这张图无关，过 30 秒再试", code="UPSTREAM"
            )
        for udm in ("48", "44"):
            href = _tab_href(info.get("tabs") or [], udm)
            if not href:
                continue
            if budget_end - time.monotonic() < 8:
                log("预算不够，跳过兜底标签页")
                break
            log(f"尝试 udm={udm} 兜底：{href[:100]}")
            try:
                await browser.navigate(_absolute_google(href), timeout=12)
            except BrowserError as exc:
                log(f"切标签页失败：{exc}")
                continue
            cards, info = await _wait_cards(
                browser, budget_end, log, min_cards=1, first_wait=7.0
            )
            if cards:
                log(f"udm={udm} 拿到 {len(cards)} 张卡片")
                return cards
    return []


async def _wait_cards(
    browser: Browser,
    budget_end: float,
    log: Callable[[str], None],
    *,
    min_cards: int,
    first_wait: float,
) -> tuple[list[dict], dict]:
    """轮询到有卡片，再等 1 秒让懒加载稳定后抽最终结果。

    实测：上传后约 3 秒出卡片（matlab 图 3.0s 时 86 张），但数量会继续增长，
    所以拿到卡片后要再等一拍，否则会漏掉后面才渲染出来的来源。
    """
    deadline = min(budget_end - 2.0, time.monotonic() + first_wait)
    info: dict = {}
    cards: list[dict] = []
    last_n = -1
    stable = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.7)
        info = await _page_info(browser)
        _guard_captcha(info)
        cards = await _collect_cards(browser)
        if len(cards) >= min_cards:
            break
        if cards and len(cards) == last_n:
            stable += 1
            if stable >= 2:  # 数量两拍没变，就当它已经稳定，别白等
                break
        else:
            stable = 0
        last_n = len(cards)

    if not cards:
        return [], info

    # 卡片稳定期：1 秒足够（实测数量增长发生在最初 1~2 秒内）。
    await asyncio.sleep(1.0)
    info = await _page_info(browser)
    _guard_captcha(info)
    settled = await _collect_cards(browser)
    return (settled or cards), info


async def _collect_cards(target: Any) -> list[dict]:
    try:
        raw = await target.eval(COLLECT_CARDS_JS, timeout=10)
    except BrowserError:
        return []  # 页面正在导航，这一拍没抽到不算失败
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict) and c.get("token")]


async def _page_info(target: Any) -> dict:
    """读页面状态。失败时把原因塞进 ``error`` 字段带出去 —— 静默吞掉的话，
    线上只能看到「0 张卡片」，根本不知道页面是崩了、被拦了、还是还在加载。"""
    try:
        raw = await target.eval(PAGE_INFO_JS, timeout=8)
    except BrowserError as exc:
        return {"error": str(exc)}
    if not raw:
        return {"error": "页面没有返回内容"}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"error": "页面内容不是合法 JSON"}
    return data if isinstance(data, dict) else {"error": "页面内容结构异常"}


def _guard_captcha(info: dict) -> None:
    """一发现验证码/同意页就立刻抛，不要傻等到超时（用户等 45 秒才被告知要验证很糟）。"""
    url = str(info.get("url") or "")
    body = str(info.get("body") or "")
    if "/sorry/" in url or CAPTCHA_TEXT_RE.search(body):
        # 这是谷歌的多因子判定（出口 IP 信誉 + 浏览器指纹 + 查询频率），不是插件能"破"的。
        # 能做的只有降低判定概率：换干净出口、带上已有 cookie、放慢频率。
        raise LensError(
            "谷歌要求人机验证（换出口 IP 或配置 google_cookie 可缓解），稍后再试",
            code="CAPTCHA",
        )
    if "consent.google.com" in url:
        raise LensError(
            "谷歌弹出了 Cookie 同意页：先在服务器上用同一个 browser_profile 手动打开一次"
            "浏览器，把同意点掉再试",
            code="CAPTCHA",
        )


def _tab_href(tabs: list[dict], udm: str) -> str:
    for tab in tabs:
        if isinstance(tab, dict) and str(tab.get("udm")) == udm:
            href = str(tab.get("href") or "")
            if href:
                return href
    return ""


def _absolute_google(href: str) -> str:
    if href.startswith("http"):
        return href
    return GOOGLE_HOME.rstrip("/") + (href if href.startswith("/") else "/" + href)


# ── token 解析 ──────────────────────────────────────────────────────────────

# 谷歌对 /goto 的限流页里只有这一条 meta refresh：5 秒后带**新 token** 重试。
# 立刻重试会直接 400（实测），必须等它要求的秒数。
_REFRESH_RE = re.compile(r'content="(\d+)\s*;\s*url=([^"]+)"', re.I)


async def _resolve_via_http(
    browser: Browser,
    cards: list[dict],
    proxy: str,
    deadline: float,
    log: Callable[[str], None],
) -> dict[int, str]:
    """用 aiohttp + 浏览器 cookie 直接发 /goto 请求，从 302 的 Location 里拿真实地址。

    为什么比开标签页好：
    - 浏览器里导航要建 TLS、要渲染目标页，一条 1.3~4.4 秒；HTTP 只要 0.2~0.6 秒。
    - 限流页那 5 秒等待是**按 token 各自计算**的，HTTP 并发时等待可以重叠；
      标签页方案只能一个个干等，10 条里 4 条中招就是 20 秒起步（实测）。
    - 不开标签页就不用管渲染进程抢占、Page.navigate 卡死这些问题。

    但 cookie 必须是浏览器那一套：不带 cookie 的请求谷歌直接给验证页（这也是
    「先用 aiohttp 上传、再让浏览器打开」那个老方案失败的原因）。
    """
    results: dict[int, str] = {}
    if not cards:
        return results
    try:
        cookie = await browser.cookie_header("google.com")
    except BrowserError as exc:
        log(f"取浏览器 cookie 失败，解析改走标签页：{exc}")
        return results
    if not cookie:
        log("浏览器里没有谷歌 cookie，解析改走标签页")
        return results

    headers = {
        "user-agent": USER_AGENT,
        "referer": GOOGLE_HOME,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cookie": cookie,
    }
    # 并发 3：再多谷歌限流会更凶（实测并发一高，连重试都会被判 400），
    # 再少就压不住时延。
    gate = asyncio.Semaphore(3)
    timeout = aiohttp.ClientTimeout(total=18, connect=10)
    # 被限流的（index, 新 token 的绝对地址, 可以重试的时刻）
    pending: list[tuple[int, str, float]] = []

    async with aiohttp.ClientSession(timeout=timeout) as session:

        async def probe(index: int, token: str) -> None:
            """第一轮：并发试一遍，能直接 302 的立刻拿到，被限流的排进待重试队列。"""
            async with gate:
                url = f"{GOOGLE_HOME.rstrip('/')}/goto?url={token}"
                try:
                    async with session.get(
                        url, headers=headers, proxy=proxy or None, allow_redirects=False
                    ) as resp:
                        if resp.status in (301, 302, 303, 307, 308):
                            location = resp.headers.get("Location", "")
                            if _is_real_url(location):
                                results[index] = location
                            return
                        if resp.status != 200:
                            return
                        text = await resp.text()
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log(f"解析第 {index} 条请求失败：{exc}")
                    return
                match = _REFRESH_RE.search(text)
                if not match or not match.group(2).startswith("/goto?url="):
                    return
                wait = min(float(match.group(1)), 6.0)
                target = GOOGLE_HOME.rstrip("/") + match.group(2)
                pending.append((index, target, time.monotonic() + wait))

        await asyncio.gather(*(probe(i, str(c["token"])) for i, c in enumerate(cards)))

        # 第二轮：限流页要求的等待是**按条各算**的，所以第一轮已经让它们重叠起来了；
        # 真正的重试必须**串行**并留间隔 —— 实测三五条重试同时打回去会被判 400。
        for index, url, ready_at in sorted(pending, key=lambda item: item[2]):
            for attempt in range(2):
                now = time.monotonic()
                if deadline - now < 1.2:
                    log("解析预算用完，剩余卡片跳过")
                    return results
                pause = max(0.0, ready_at - now) + random.uniform(0.1, 0.5)
                if pause:
                    await asyncio.sleep(min(pause, max(0.0, deadline - now - 0.8)))
                try:
                    async with session.get(
                        url, headers=headers, proxy=proxy or None, allow_redirects=False
                    ) as resp:
                        if resp.status in (301, 302, 303, 307, 308):
                            location = resp.headers.get("Location", "")
                            if _is_real_url(location):
                                results[index] = location
                            break
                        if resp.status != 200:
                            text = ""
                        else:
                            text = await resp.text()
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log(f"重试第 {index} 条失败：{exc}")
                    break
                match = _REFRESH_RE.search(text)
                if not match or not match.group(2).startswith("/goto?url="):
                    break
                url = GOOGLE_HOME.rstrip("/") + match.group(2)
                ready_at = time.monotonic() + min(float(match.group(1)), 6.0)
            await asyncio.sleep(0.4)  # 串行间隔，别把重试连成一串

    if pending:
        log(f"限流后重试完成，累计解析出 {len(results)}/{len(cards)} 条")
    return results


async def _resolve_cards(
    browser: Browser,
    cards: list[dict],
    budget_end: float,
    log: Callable[[str], None],
) -> dict[int, str]:
    """并发 2~3 个标签页把 /goto?url=<token> 解析成真实地址。

    单条 1.3~4.4 秒，10 条串行就是 30 秒上下；开 3 个标签页刚好压在 6 秒内。
    结果写进共享 dict，即使外层超时被取消，已解析到的也留得下来。
    """
    results: dict[int, str] = {}
    if not cards:
        return results
    # 卡片已经拿到了，解析这一步值一点宽限：多花 6 秒换回可用的 sourceUrl 很划算。
    deadline = max(budget_end, time.monotonic() + 6.0)
    workers = max(1, min(3, len(cards)))
    buckets: list[list[tuple[int, str]]] = [[] for _ in range(workers)]
    for idx, card in enumerate(cards):
        buckets[idx % workers].append((idx, str(card["token"])))

    # 谷歌的限流是「越敲越紧」：连着几条都撞到节流页，继续解析只会让这一批更慢、
    # 也更可能升级成真验证码。撞到 2 条就收手，用已有的结果回话。
    stats = {"throttled": 0}

    async def worker(jobs: list[tuple[int, str]]) -> None:
        try:
            tab = await browser.new_tab()
        except BrowserError as exc:
            log(f"解析标签页开不出来：{exc}")
            return
        try:
            for idx, token in jobs:
                if stats["throttled"] >= 2:
                    log("连续遇到谷歌限流，剩余卡片本次不再解析")
                    break
                left = deadline - time.monotonic()
                if left < 1.5:
                    log("解析预算用完，剩余卡片跳过")
                    break
                # 单条最多 11 秒：够走完一次「限流页等 2 秒 + 重试 + 302」，
                # 又不会因为某一条卡住把整次查询的预算吃光。
                url = await _resolve_one(tab, token, min(11.0, left), log, stats)
                if url:
                    results[idx] = url
        except Exception as exc:  # 单个 worker 出错不影响其它 worker
            log(f"解析 worker 异常退出：{exc}")
        finally:
            await tab.close()

    await asyncio.gather(*(worker(job) for job in buckets), return_exceptions=True)
    return results


async def _resolve_one(
    tab: Any,
    token: str,
    budget: float,
    log: Callable[[str], None] | None = None,
    stats: dict | None = None,
) -> str:
    """让 /goto?url=<token> 自己跳走，拿到真实地址。

    token 是 protobuf base64，解不出来；只能真访问一次借谷歌的 302。

    关键提速手段：订阅 ``Network.requestWillBeSent``，看 ``redirectResponse`` 就能在
    **302 刚发出、目标站点的 TLS 还没握手**的时候拿到目标 URL。
    只靠轮询 location.href 的话，跨域导航要等 TLS + 首字节落地才更新，
    冷 cache 下每条 2~4 秒、10 条就要 20 秒以上（实测），用事件只要 0.2~0.6 秒。
    """
    target = f"{GOOGLE_HOME.rstrip('/')}/goto?url={token}"
    hops: list[str] = []

    def on_request(params: dict) -> None:
        # 不能只看 redirectResponse：谷歌的 /goto 有时是服务端 302（会带 redirectResponse），
        # 有时是一张带 meta refresh / JS 跳转的中转页（直接发起新请求，没有 redirectResponse）。
        # 两种情况都会产生「指向目标站点的请求」，所以任何一跳的请求地址都收下来，由
        # _is_real_url 判断是不是已经走出谷歌了 —— 只要一走出去就立刻拿到答案。
        request = params.get("request") or {}
        url = str(request.get("url") or "")
        if url:
            hops.append(url)

    unsubscribe = tab.listen("Network.requestWillBeSent", on_request)
    found = ""
    started = time.monotonic()
    source = ""
    try:
        await tab.navigate(target, timeout=min(6.0, budget))
    except BrowserError:
        pass  # 单条失败直接跳过，不能因为一张卡片炸掉整次查询
    else:
        end = time.monotonic() + budget
        tick = 0
        follows = 0
        while time.monotonic() < end:
            await asyncio.sleep(0.1)
            tick += 1
            for url in hops:
                if _is_real_url(url):
                    found = url
                    source = "event"
                    break
            if found:
                # 拿到就够了：立刻掐掉加载，不然 x.com/instagram 的首页会接着下载
                # 一大堆静态资源，把下一条解析堵在这条连接上。
                with contextlib.suppress(BrowserError):
                    await tab._cdp.send("Page.stopLoading", timeout=2.0)  # noqa: SLF001
                break
            # 事件偶尔会漏（比如导航被 Service Worker 接管），每 4 拍用页面探测兜一次；
            # 顺便在这一步处理谷歌的限流页（原因见 RESOLVE_PROBE_JS 上面的注释）。
            if tick % 4:
                continue
            try:
                raw = await tab.eval(RESOLVE_PROBE_JS, timeout=2.0)
                probe = json.loads(raw) if raw else {}
            except (BrowserError, ValueError, TypeError):
                continue
            current = str((probe or {}).get("href") or "")
            if _is_real_url(current):
                found = current
                source = "poll"
                break
            refresh = str((probe or {}).get("refresh") or "")
            if refresh:
                next_url = _absolute_google(refresh)
                if "/goto?url=" in next_url:
                    # 限流页：整页只有一条「5 秒后带新 token 重试」的 meta refresh。
                    # 真人在这儿点一下「继续」就行，所以等 2 秒替它跳；最多替两次 ——
                    # 再多就是在硬刚谷歌的限流，容易被升级成真验证码。
                    if follows < 2 and time.monotonic() - started > 2.0:
                        follows += 1
                        if stats is not None:
                            stats["throttled"] = int(stats.get("throttled") or 0) + 1
                        if log:
                            log(f"命中谷歌限流页，替它重试第 {follows} 次")
                        with contextlib.suppress(BrowserError):
                            await tab.navigate(next_url, timeout=min(4.0, budget))
                        continue
                else:
                    # 普通跳转页：meta refresh 直接指向真实目的地，立刻跟着跳，
                    # 不要当成限流去等 2 秒（否则每条都白等，10 条就是 20 秒）。
                    if _is_real_url(next_url):
                        found = next_url
                        source = "refresh"
                        break
                    with contextlib.suppress(BrowserError):
                        await tab.navigate(next_url, timeout=min(3.0, budget))
                    continue
            if "/sorry/" in current:
                return ""  # 已经被要求人机验证，这条放弃，别拖死整个查询
    finally:
        unsubscribe()

    # 每条解析完都复位到 about:blank。不复位的话，下一条要在一个还在加载
    # facebook/instagram 的渲染进程上执行 Page.navigate，实测会被卡住 4~7 秒
    # （日志里表现为「浏览器没响应 Page.navigate」），而且那个 token 就废了。
    with contextlib.suppress(BrowserError):
        await tab.eval("location.href='about:blank'", timeout=2.0)
    return found


def _is_real_url(url: str) -> bool:
    """这个地址算不算「已经走出谷歌、拿到真实来源」。

    谷歌的中转有好几层：`/goto?url=` → `google.com/url?q=` → `/search?...`，
    任何一层都不算答案；用户要的是最后那个站点的地址。
    """
    if not url.startswith("http"):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host == "google.com" or host.endswith(".google.com") or ".google." in host:
        return False
    if host in ("gstatic.com", "googleusercontent.com") or host.endswith(".gstatic.com"):
        return False
    if "google.com/goto" in url:
        return False
    return True


# ── 排序与结果组装 ──────────────────────────────────────────────────────────


def _pick_cards(cards: list[dict], max_resolve: int) -> list[dict]:
    """挑出要解析的卡片：社交平台先占名额，其余按「像不像一条正经来源」排序。

    这里必须处理一个实测现象：**同一个 token 会在同一个卡片里出现多次**
    （缩略图是 a、标题是另一个 a，href 完全一样）。如果只取第一条，拿到的是
    视频缩略图那种 ``title='0:26'`` 的垃圾记录；所以按 token 归并，取信息最全的那条。
    """
    if max_resolve <= 0:
        return []

    order: list[str] = []
    best: dict[str, dict] = {}
    for card in cards:
        token = str(card.get("token") or "")
        if not token:
            continue
        previous = best.get(token)
        if previous is None:
            best[token] = card
            order.append(token)
        elif _card_score(card) > _card_score(previous):
            best[token] = card

    merged = [best[token] for token in order]
    social: list[dict] = []
    normal: list[tuple[int, int, dict]] = []
    for index, card in enumerate(merged):
        if _looks_social(card):
            social.append(card)
        else:
            normal.append((-_card_score(card), index, card))
    normal.sort(key=lambda item: (item[0], item[1]))

    picked = social[:max_resolve]
    need = max_resolve - len(picked)
    if need > 0:
        picked += [card for _, _, card in normal[:need]]
    return picked


_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def _card_score(card: dict) -> int:
    """给卡片打个「信息含量」分。

    Lens 结果页的 DOM 顺序是「视频轮播 → 图片墙 → 来源列表」，纯按顺序取前 N 张
    会拿到一堆 title 只有 "0:26" 的视频缩略图（实测），真正有用的来源卡片在后面。
    """
    title = str(card.get("title") or "").strip()
    site = str(card.get("site") or "").strip()
    score = 0
    if title:
        score += 2
        if not _DURATION_RE.match(title):
            score += 2
        if len(title) >= 12:
            score += 1
    if site and site != title:
        score += 1
    if _site_name(card, title):
        score += 2
    return score


_REPLY_CARD_RE = re.compile(r"^(?:X|Twitter)?\s*Repl(?:y|ies)\s+by\b", re.I)


def _looks_social(card: dict) -> bool:
    text = " ".join(str(card.get(k) or "") for k in ("site", "label", "title"))
    return bool(SOCIAL_TEXT_RE.search(text) or _SOCIAL_WORD_RE.search(text))


def _build_results(picked: list[dict], resolved: dict[int, str]) -> list[dict]:
    items: list[tuple[dict, bool]] = []
    for idx, card in enumerate(picked):
        url = resolved.get(idx) or ""
        if not url:
            continue  # 解析失败的卡片不进结果：只有 token 没法给人看
        host = (urlparse(url).hostname or "").lower()
        title = re.sub(r"\s+", " ", str(card.get("title") or "")).strip()[:200]
        item = {
            "engine": "lens",
            "similarity": None,
            "title": title,
            "sourceUrl": url,
            "authorUrl": "",
            "host": host,
            "snippet": "",
            "links": [url],
            "siteName": _site_name(card, title),
        }
        # 「Replies by 某人」这种卡片是**别人在这张图下面回帖**，不是原帖作者。
        # 直接丢掉太可惜（有时原帖已删、只剩转发），所以标成弱证据：
        # 排序时排在 direct 之后，回话里也会标「未核实」。
        if _REPLY_CARD_RE.match(title):
            item["weak"] = True
            item["snippet"] = "别人的回帖，不一定是原帖作者"
        items.append((item, host in SOCIAL_HOSTS or _looks_social(card)))

    # 稳定排序：社交卡片提到前面，同组内保持结果页原有顺序（相关性由谷歌给）。
    items.sort(key=lambda pair: 0 if pair[1] else 1)

    results: list[dict] = []
    seen_urls: set[str] = set()
    for item, _ in items:
        if item["sourceUrl"] in seen_urls:
            continue
        seen_urls.add(item["sourceUrl"])
        results.append(item)
    return results


def _site_name(card: dict, title: str) -> str:
    """推站点名：aria-label 最可靠 → 卡片首行（站点名单独一行时）→ 已知站名前缀。"""
    label = str(card.get("label") or "").strip()
    if label:
        return label[:60]

    site = str(card.get("site") or "").strip()
    if site and len(site) <= 30 and site != title and (not title or title.startswith(site)):
        return site

    lowered = title.lower()
    for name in _KNOWN_SITES:
        if not lowered.startswith(name.lower()):
            continue
        tail = title[len(name) : len(name) + 1]
        # 单/双字符站名（X）必须右边是边界：中文紧邻是正常的（"X阿薰…"），
        # 但 "Xiaohongshu" 不能被认成 "X"。
        if len(name) <= 2 and tail and tail.isascii() and (tail.isalnum() or tail == "_"):
            continue
        return name
    return ""


# ── 零碎工具 ────────────────────────────────────────────────────────────────


def _ext_for(mime_type: str) -> str:
    """临时文件的扩展名要跟真实格式一致。

    浏览器的 file input 按扩展名推 MIME，扩展名给错了 Lens 会当成未知文件拒收。
    """
    mime = (mime_type or "").split(";")[0].strip().lower()
    ext = _MIME_EXT.get(mime)
    if ext:
        return ext
    guessed = mimetypes.guess_extension(mime) if mime else ""
    return guessed or ".jpg"


def _as_lens_error(exc: BrowserError) -> LensError:
    message = str(exc)
    if "没找到可用的浏览器" in message or "可执行文件不存在" in message:
        return LensError(message, code="NO_BROWSER")
    if NET_ERROR_RE.search(message):
        return LensError(f"谷歌连不上（代理没开或代理不通）：{message}", code="UNREACHABLE")
    return LensError(f"浏览器出了点问题：{message}", code="BROWSER")


def _make_logger(on_log: Callable[[str], None] | None) -> Callable[[str], None]:
    def log(line: str) -> None:
        logger.debug("lens: %s", line)
        if on_log is None:
            return
        try:
            on_log(str(line))
        except Exception:  # 回调是外部的，出错不能影响查询
            pass

    return log
