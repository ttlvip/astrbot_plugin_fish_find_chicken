"""鱼鱼找鸡的核心：给一张图，反查出这是谁、账号在哪。

对外只暴露一个入口 `search_image`，插件层（main.py）不需要知道引擎细节：

    result = await search_image(data, "image/jpeg", settings=settings, store=store)
    result["text"]      # 直接能发群里的文案
    result["accounts"]  # 结构化账号（见下）

每条账号的字段：

    platform / handle / authorName / confidence   # 是谁、在哪、可信度
    url / profileUrl                              # 主页链接（看全部作品、认人）
    matchUrl / matchTitle / matchHost             # 与图片内容最相关的那条具体页面
                                                  # （推文 / 微博正文 / 作品页 / 笔记）
    matchUrls                                     # 同上，按相关性排的前 3 条，备查
    evidence                                      # 引擎 / 相似度 / 命中的原始 sourceUrl

`matchUrl` 为空 = 引擎只翻到主页，没有比主页更具体的内容页；这时候不要拿主页凑数。

设计上的几条底线：

1. **Lens 是主力**。真人照片只有谷歌 Lens 认得出来，其它引擎是补充（动漫图、动画截图）。
2. **绝不编造账号**。只有 URL 证据才会被报出来；认不出就是「没查到」，这比报错人好。
3. **不把用户图片发给白名单外的域名**。上传目标只有谷歌 Lens / SauceNAO / IQDB / trace.moe。
"""

from __future__ import annotations

import hashlib

from .accounts import (
    ARTWORK_PLATFORMS,
    account_name_of,
    dominant_person_name,
    display_handle,
    drop_weak_artwork,
    extract_accounts,
    match_platform,
    normalize_person_name,
    platform_label,
)
from .config import DEFAULT_ENGINES, ENGINE_IDS, ENGINE_LABELS, Settings
from .engines import EngineError, engine_labels, reverse_search
from .net import HttpError, download_image, sniff_mime
from .reply import format_reply
from .textutil import trim_text
from .state import StateStore, day_key

# 缓存版本号：改了引擎选择/解析逻辑就加一，让老缓存自动失效，
# 否则同一张图会一直吃旧结果，看起来「改了跟没改一样」。
# v3：账号新增 matchUrl（最相关原帖链接），v2 的缓存里没有这个字段。
# v4：缓存键纳入引擎/严格筛选等配置；失败结果不再写入。
CACHE_VERSION = 4

# 这类失败说明这次查询不可靠，写进缓存会挡住后续重试（尤其是 Lens 验证码/超时）。
_CACHE_BLOCKING_FAILURES = ("超时", "验证码", "人机", "限流", "429", "captcha", "timeout", "throttle")

__all__ = [
    "ARTWORK_PLATFORMS",
    "CACHE_VERSION",
    "DEFAULT_ENGINES",
    "ENGINE_IDS",
    "ENGINE_LABELS",
    "EngineError",
    "HttpError",
    "Settings",
    "StateStore",
    "account_name_of",
    "cache_key_for",
    "day_key",
    "display_handle",
    "dominant_person_name",
    "drop_weak_artwork",
    "download_image",
    "engine_labels",
    "extract_accounts",
    "format_reply",
    "match_platform",
    "normalize_person_name",
    "platform_label",
    "reverse_search",
    "search_image",
    "sniff_mime",
]


def cache_key_for(content: bytes, settings: Settings | None = None) -> str:
    """按图片内容 + 会影响结果的配置生成缓存键。

    `/鱼鱼找鸡 全部` 会把 strict_consensus 设成 False，必须和严格筛选分开存，
    否则命中旧缓存时「全部」等于没开。
    """
    digest = hashlib.sha256(content).hexdigest()
    if settings is None:
        return f"{digest}:{CACHE_VERSION}"
    engines = ",".join(settings.engines)
    channel = "serpapi" if settings.serpapi_ready else "browser"
    conf = (
        f"e={engines}|s={int(bool(settings.strict_consensus))}|"
        f"m={settings.max_accounts}|c={channel}|r={settings.lens_max_resolve}"
    )
    return f"{digest}:{CACHE_VERSION}:{hashlib.sha256(conf.encode()).hexdigest()[:12]}"


def _should_cache(result: dict) -> bool:
    """只有可靠的成功结果才写缓存，避免把超时/验证码锁进 30 天。"""
    engines_used = result.get("enginesUsed") or []
    if not engines_used:
        return False
    failures = result.get("failures") or []
    for item in failures:
        reason = str(item.get("reason") or "").lower()
        if any(token in reason for token in _CACHE_BLOCKING_FAILURES):
            return False
    candidates = result.get("candidates") or []
    # 候选全空且有任意失败：多半是半残结果（比如补充引擎空跑、Lens 挂了），别缓存。
    if not candidates and failures:
        return False
    return True


async def search_image(
    content: bytes,
    mime_type: str,
    *,
    settings: Settings,
    store: StateStore | None = None,
    on_log=None,
) -> dict:
    """反查一张图，返回 candidates / accounts / text / 引擎与失败信息。"""
    key = cache_key_for(content, settings)
    if store is not None:
        cached = store.cache_get(key, cache_days=settings.cache_days)
        if cached and cached.get("text"):
            cached = dict(cached)
            cached["cached"] = True
            if on_log:
                on_log("[鱼鱼找鸡] 命中缓存，直接用上次结果")
            return cached

    result = await reverse_search(content, mime_type, settings=settings, on_log=on_log)
    candidates = result["candidates"]
    # 名字主轴只从 Lens 的候选里算：它才是「从这张图认出这个人」的引擎，
    # SauceNAO / IQDB 的标题是画作信息，混进来会把主轴带偏（实测会把所有账号丢光）。
    lens_candidates = [c for c in candidates if c.get("engine") == "lens"]
    # 被「名字对不上主轴」挡掉的号要收集起来：这是回话里区分
    # 「压根没翻到号」和「翻到了但不是这个人」的唯一依据。
    dropped: list[dict] = []

    def _on_drop(account, _consensus) -> None:
        dropped.append(account)

    accounts = extract_accounts(
        candidates,
        max_accounts=settings.max_accounts,
        strict_names=settings.strict_consensus,
        consensus_candidates=lens_candidates or candidates,
        on_drop=_on_drop,
    )
    # pixiv 这类画作平台的「相似画作」相似度不够就不能当成图源 —— 否则真人照片会被
    # SauceNAO 的画风近似结果带偏，报出一堆画师号。
    accounts, artwork_dropped = drop_weak_artwork(accounts, 0.8)
    # 没认出「这张图主要在讲谁」却报了好几个号时，得说清楚这是按相关性排的候选，
    # 否则用户会以为三个都是同一个人的号。
    consensus = dominant_person_name(lens_candidates or candidates)
    note = ""
    if any(a.get("fallback") for a in accounts):
        # 严格过滤全灭后回退的情况：必须说清楚「这些号名字都没对上」，别让用户误以为是确定答案
        subject = f"「{trim_text(str(consensus.get('name') or ''), 20)}」" if consensus.get("name") else "主轴"
        note = f"这些号的名字都没对上{subject}，但都列出来了（未核实），点开核对一下"
    elif len(accounts) > 1 and not consensus.get("prefix"):
        note = "没找到统一的作者名，以上按相关性排序，点开核对一下"
    elif any(a.get("confidence") == "weak" for a in accounts):
        note = "标「相似图」的是别人的帖子，不一定是原帖作者"
    text = format_reply(
        accounts=accounts,
        candidates=candidates,
        engines_used=result["enginesUsed"],
        failures=result["failures"],
        artwork_dropped=artwork_dropped,
        consensus_name=str(consensus.get("name") or ""),
        consensus_hits=int(consensus.get("hits") or 0),
        name_dropped=len(dropped),
        dropped_handles=[
            (a.get("handle") or a.get("url") or "") for a in dropped[:5]
        ],
        note=note,
    )
    payload = {
        "accounts": accounts,
        "candidates": candidates,
        "text": text,
        "enginesUsed": result["enginesUsed"],
        "failures": result["failures"],
        "cached": False,
    }
    # 只缓存可靠的成功结果：半残/超时/验证码写进去会挡住用户重试。
    if store is not None and _should_cache(payload):
        await store.cache_put(key, payload)
    return payload
