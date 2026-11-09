"""各引擎适配 + 反查编排。

引擎分工（这一点决定了插件好不好用，别随手调换）：

* **lens**（谷歌 Lens）—— 主力。真人照片只有它能反查出原帖，进而拿到 X/微博 账号。
  必须开代理、必须有无头浏览器（它要在浏览器里上传图片并渲染结果页）。
* **saucenao** —— 动漫/同人图，纯 HTTP，无水印限制，能直接给出 pixiv 作者行。
* **iqdb** —— 动漫图库（danbooru / konachan / yande.re），常排队。
* **tracemoe** —— 动画截图溯源，给的是番剧和集数，不是社媒账号。

编排策略：**lens 先发车，网络引擎同时跑**。lens 冷启动 + 渲染要十几秒，网络引擎只要
两三秒；串行跑等于白白多等十几秒。浏览器是单例，多个 lens 查询在内部排队。
"""

from __future__ import annotations

import asyncio
import json
import re

from .config import ENGINE_IDS, ENGINE_LABELS, Settings
from .net import HttpError, post_multipart
from .textutil import abs_url, host_of, strip_tags, trim_text


class EngineError(RuntimeError):
    """单个引擎失败。code 用于回话里区分「限流 / 排队 / 不可用」。"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def engine_labels(engines) -> list[str]:
    return [ENGINE_LABELS.get(str(e), str(e)) for e in (engines or []) if e]


def _upload_name(mime_type: str) -> str:
    return "image.jpg" if (mime_type or "").lower() == "image/jpeg" else "image.png"


# ── SauceNAO ──────────────────────────────────────────────────────────────
async def saucenao_search(
    content: bytes, mime_type: str, *, timeout: float, proxy: str = ""
) -> list[dict]:
    """SauceNAO 不需要 key，但连发会被 403 限流（大约 30 秒一次）。"""
    result = await post_multipart(
        "https://saucenao.com/search.php",
        file_field="file",
        filename=_upload_name(mime_type),
        content=content,
        content_type=mime_type or "image/png",
        fields=[("db", "999"), ("output_type", "0")],
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
        proxy=proxy,
    )
    if result.status == 403:
        raise EngineError("SauceNAO 限流（403），至少等 60 秒再试", code="RATE_LIMITED")
    if result.status != 200:
        raise EngineError(f"SauceNAO HTTP {result.status}")
    return parse_saucenao(result.text)


def parse_saucenao(html) -> list[dict]:
    """解析 SauceNAO 结果页。

    页面结构：每个结果是一个 `<div class="result">` 块，块里有相似度、标题、
    所有外链（第一条通常是图源）、以及 `Member: <a>作者名</a>` 形式的作者行。
    作者名比作品标题更像「这个人叫什么」，后面抽账号时优先用它。
    """
    text = str(html if html is not None else "")
    blocks = _RESULT_SPLIT.split(text)[1:]
    if not blocks:
        # 兜底：结构变了也不至于完全瞎，至少把相似度捞出来
        return [{"engine": "saucenao", "similarity": float(m) / 100} for m in _SIM_INFO.findall(text)]

    candidates = []
    for block in blocks:
        sim = _SIM_INFO.search(block) or _SIM_ALT.search(block) or _ANY_PERCENT.search(block)
        similarity = float(sim.group(1)) / 100 if sim else None

        title_match = _TITLE_STRONG.search(block) or _TITLE_PLAIN.search(block)
        title = strip_tags(title_match.group(1)) if title_match else ""

        links: list[str] = []
        for href in _HREF.findall(block):
            url = abs_url(href, "https://saucenao.com/")
            if not url:
                continue
            host = host_of(url)
            if not host or host.endswith("saucenao.com"):
                continue
            if url not in links:
                links.append(url)

        author = _AUTHOR_WITH_TEXT.search(block) or _AUTHOR_HREF.search(block)
        author_url = abs_url(author.group(1), "https://saucenao.com/") if author else ""
        author_name = (
            trim_text(strip_tags(author.group(2)), 40)
            if author and author.lastindex and author.lastindex >= 2
            else ""
        )

        if not links and not title:
            continue
        candidates.append(
            {
                "engine": "saucenao",
                "similarity": similarity,
                "title": title,
                "sourceUrl": links[0] if links else "",
                "authorUrl": author_url,
                "authorName": author_name,
                "host": host_of(links[0] if links else author_url),
                "snippet": trim_text(strip_tags(block), 200),
                "links": links,
            }
        )
    return candidates


# ── IQDB ──────────────────────────────────────────────────────────────────
async def iqdb_search(content: bytes, mime_type: str, *, timeout: float, proxy: str = "") -> list[dict]:
    result = await post_multipart(
        "https://iqdb.org/",
        file_field="file",
        filename=_upload_name(mime_type),
        content=content,
        content_type=mime_type or "image/png",
        fields=[("service[]", str(n)) for n in range(1, 7)],
        headers={"Accept": "text/html"},
        # IQDB 排队很常见，给的超时比别的引擎宽一点
        timeout=max(timeout, 35.0),
        proxy=proxy,
    )
    if result.status != 200:
        raise EngineError(f"IQDB HTTP {result.status}")
    return parse_iqdb(result.text)


def parse_iqdb(html) -> list[dict]:
    text = str(html if html is not None else "")
    if _IQDB_QUEUE.search(text):
        raise EngineError("IQDB 在排队（站点高负载），过一会儿再试", code="QUEUED")
    if _IQDB_EMPTY.search(text):
        return []

    links = []
    for m in _ANCHOR.finditer(text):
        raw = m.group(1)
        if raw.startswith("//"):
            raw = "https://" + raw[2:]
        url = abs_url(raw, "https://iqdb.org/")
        if not url:
            continue
        host = host_of(url)
        if not host or host.endswith("iqdb.org"):
            continue
        if _ASSET_RE.search(url):
            continue
        if any(item["url"] == url for item in links):
            continue
        links.append({"url": url, "host": host, "title": trim_text(strip_tags(m.group(2)), 100)})

    sim = _IQDB_SIM.search(text)
    similarity = float(sim.group(1)) / 100 if sim else None
    return [
        {
            "engine": "iqdb",
            "similarity": similarity,
            "title": item["title"],
            "sourceUrl": item["url"],
            "authorUrl": "",
            "host": item["host"],
            "snippet": "",
            "links": [item["url"]],
        }
        for item in links[:12]
    ]


# ── trace.moe ─────────────────────────────────────────────────────────────
async def tracemoe_search(
    content: bytes, mime_type: str, *, timeout: float, proxy: str = ""
) -> list[dict]:
    result = await post_multipart(
        "https://api.trace.moe/search?anilistInfo",
        file_field="image",
        filename=_upload_name(mime_type),
        content=content,
        content_type=mime_type or "image/png",
        headers={"Accept": "application/json"},
        timeout=timeout,
        proxy=proxy,
    )
    if result.status != 200:
        raise EngineError(f"trace.moe HTTP {result.status}")
    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise EngineError(f"trace.moe 返回的不是 JSON：{exc}") from exc
    return parse_tracemoe(payload)


def parse_tracemoe(payload) -> list[dict]:
    items = payload.get("result") if isinstance(payload, dict) else None
    items = items if isinstance(items, list) else []
    out = []
    seen = set()
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        # 0.87 以下基本是「随便一个番的某帧」，真命中一般 0.9 以上
        similarity = item.get("similarity")
        similarity = float(similarity) if isinstance(similarity, (int, float)) else 0.0
        if similarity < 0.87:
            continue
        anilist = item.get("anilist") if isinstance(item.get("anilist"), dict) else {}
        anilist_id = anilist.get("id") or anilist.get("anime_id")
        titles = anilist.get("title") if isinstance(anilist.get("title"), dict) else {}
        title = (
            titles.get("native")
            or titles.get("romaji")
            or titles.get("english")
            or item.get("filename")
            or ""
        )
        source_url = f"https://anilist.co/anime/{anilist_id}" if anilist_id else ""
        if not source_url or source_url in seen:
            continue
        seen.add(source_url)
        episode = item.get("episode")
        suffix = f" 第{episode}集" if isinstance(episode, (int, float)) else ""
        out.append(
            {
                "engine": "tracemoe",
                "similarity": similarity,
                "title": trim_text(f"{title}{suffix}", 120),
                "sourceUrl": source_url,
                "authorUrl": "",
                "host": "anilist.co",
                "snippet": trim_text(str(item.get("filename") or ""), 120),
                "links": [source_url],
            }
        )
        if len(out) >= 3:
            break
    return out


_NETWORK_RUNNERS = {
    "saucenao": saucenao_search,
    "iqdb": iqdb_search,
    "tracemoe": tracemoe_search,
}


# ── 编排 ──────────────────────────────────────────────────────────────────
async def reverse_search(
    content: bytes,
    mime_type: str,
    *,
    settings: Settings,
    on_log=None,
) -> dict:
    """跑一遍所有启用的引擎，返回 {candidates, failures, enginesUsed}。

    单个引擎失败不影响其它引擎 —— 反查本来就是个「多买几张彩票」的活。
    """
    wanted = [e for e in settings.engines if e in ENGINE_IDS] or ["lens"]
    candidates: list[dict] = []
    failures: list[dict] = []
    engines_used: list[str] = []
    timeout = float(settings.timeout_sec)

    def log(line: str) -> None:
        if on_log:
            try:
                on_log(line)
            except Exception:  # noqa: BLE001 - 日志出错不能影响结果
                pass

    async def run_lens() -> None:
        started = asyncio.get_running_loop().time()

        # 配了 SerpApi（开关 + key）就优先走它：不用浏览器、不会撞人机验证。
        # 没配就老老实实走本地浏览器 —— 这是用户明确要求的行为，不静默切换。
        if settings.serpapi_ready:
            try:
                from .serpapi import SerpApiError, serpapi_lens_search
            except Exception as exc:  # noqa: BLE001
                failures.append({"engine": "lens", "reason": f"SerpApi 模块不可用：{exc}"})
                return
            try:
                found = await asyncio.wait_for(
                    serpapi_lens_search(
                        content,
                        mime_type,
                        api_key=settings.serpapi_api_key,
                        proxy=settings.proxy,
                        timeout=max(timeout, 45.0),
                        on_log=log,
                    ),
                    timeout=max(timeout, 45.0) + 10,
                )
            except SerpApiError as exc:
                failures.append({"engine": "lens", "reason": str(exc)})
                log(f"[鱼鱼找鸡] lens（SerpApi）失败：{exc}")
                return
            except asyncio.TimeoutError:
                failures.append({"engine": "lens", "reason": "SerpApi 超时"})
                log("[鱼鱼找鸡] lens（SerpApi）超时")
                return
            except Exception as exc:  # noqa: BLE001
                failures.append({"engine": "lens", "reason": str(exc) or exc.__class__.__name__})
                return
            elapsed = (asyncio.get_running_loop().time() - started) * 1000
            engines_used.append("lens")
            candidates.extend(found)
            log(f"[鱼鱼找鸡] lens（SerpApi）完成 {elapsed:.0f}ms，结果 {len(found)} 条")
            return

        try:
            from .lens import LensError, browser_available, lens_search
        except Exception as exc:  # noqa: BLE001 - 缺文件/循环导入时给一条明确的失败
            failures.append({"engine": "lens", "reason": f"Lens 模块不可用：{exc}"})
            return
        if not browser_available(settings.browser_exe, settings.browser_cdp):
            failures.append(
                {"engine": "lens", "reason": "没找到 Chrome/Edge，Lens 需要它来渲染结果页"}
            )
            return
        try:
            found = await asyncio.wait_for(
                lens_search(
                    content,
                    mime_type,
                    proxy=settings.proxy,
                    browser_exe=settings.browser_exe,
                    timeout=max(timeout, 45.0),
                    max_resolve=settings.lens_max_resolve,
                    headless=settings.browser_headless,
                    region=settings.browser_region,
                    profile_dir=settings.browser_profile,
                    cdp_url=settings.browser_cdp,
                    cdp_insecure=settings.browser_cdp_insecure,
                    google_cookie=settings.google_cookie,
                    on_log=log,
                ),
                timeout=max(timeout, 45.0) + 20,
            )
        except LensError as exc:
            failures.append({"engine": "lens", "reason": str(exc)})
            log(f"[鱼鱼找鸡] lens 失败：{exc}")
            return
        except asyncio.TimeoutError:
            failures.append({"engine": "lens", "reason": "超时"})
            log("[鱼鱼找鸡] lens 超时")
            return
        except Exception as exc:  # noqa: BLE001
            failures.append({"engine": "lens", "reason": str(exc) or exc.__class__.__name__})
            log(f"[鱼鱼找鸡] lens 失败：{exc}")
            return
        elapsed = (asyncio.get_running_loop().time() - started) * 1000
        engines_used.append("lens")
        if found:
            candidates.extend(found)
        else:
            failures.append({"engine": "lens", "reason": "空结果"})
        log(f"[鱼鱼找鸡] lens 完成 {elapsed:.0f}ms，结果 {len(found or [])} 条")

    async def run_network(engine: str) -> None:
        started = asyncio.get_running_loop().time()
        runner = _NETWORK_RUNNERS.get(engine)
        if runner is None:
            failures.append({"engine": engine, "reason": "未实现的引擎"})
            return
        try:
            found = await asyncio.wait_for(
                runner(content, mime_type, timeout=timeout, proxy=settings.proxy),
                timeout=timeout + 10,
            )
        except asyncio.TimeoutError:
            failures.append({"engine": engine, "reason": "超时"})
            log(f"[鱼鱼找鸡] {engine} 超时")
            return
        except (EngineError, HttpError) as exc:
            failures.append({"engine": engine, "reason": str(exc)})
            log(f"[鱼鱼找鸡] {engine} 失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            failures.append({"engine": engine, "reason": str(exc) or exc.__class__.__name__})
            log(f"[鱼鱼找鸡] {engine} 失败：{exc}")
            return
        elapsed = (asyncio.get_running_loop().time() - started) * 1000
        engines_used.append(engine)
        if found:
            candidates.extend(found)
        else:
            failures.append({"engine": engine, "reason": "空结果"})
        log(f"[鱼鱼找鸡] {engine} 完成 {elapsed:.0f}ms，结果 {len(found or [])} 条")

    tasks = []
    if "lens" in wanted:
        tasks.append(asyncio.create_task(run_lens()))
    for engine in wanted:
        if engine == "lens":
            continue
        tasks.append(asyncio.create_task(run_network(engine)))
    if tasks:
        await asyncio.gather(*tasks)

    # 保持用户配置的引擎顺序，回话里显示顺序才稳定
    engines_used.sort(key=lambda e: wanted.index(e) if e in wanted else 99)
    return {"candidates": candidates, "failures": failures, "enginesUsed": engines_used}


# ── 正则 ──────────────────────────────────────────────────────────────────
_RESULT_SPLIT = re.compile(r'<div class="result\b')
_SIM_INFO = re.compile(r"resultsimilarityinfo[^>]*>\s*([\d.]+)%")
_SIM_ALT = re.compile(r"similarity-info[^>]*>[\s\S]{0,120}?([\d.]+)%")
_ANY_PERCENT = re.compile(r"([\d.]+)%")
_TITLE_STRONG = re.compile(r'class="resulttitle"[\s\S]{0,400}?<strong[^>]*>([\s\S]*?)</strong>', re.I)
_TITLE_PLAIN = re.compile(r'class="resulttitle"[\s\S]{0,200}?>\s*([^<]{2,200})<', re.I)
_HREF = re.compile(r'href="([^"]+)"')
_AUTHOR_WITH_TEXT = re.compile(
    r'(?:Member|Author)[\s\S]{0,240}?<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.I
)
_AUTHOR_HREF = re.compile(r'author[^>]*>[\s\S]{0,200}?href="([^"]+)"', re.I)
_IQDB_QUEUE = re.compile(r"Place in queue|under high load", re.I)
_IQDB_EMPTY = re.compile(r"No relevant matches")
_ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.I)
_ASSET_RE = re.compile(r"\.(?:css|js|ico|png|jpe?g|gif|svg)(?:\?|$)", re.I)
_IQDB_SIM = re.compile(r"(\d{1,3})%\s*similarity", re.I)
