"""文本小工具：去标签、截断、补全链接、取域名。"""

from __future__ import annotations

import html as _html
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]*>")


def strip_tags(value) -> str:
    """去标签 + 反转义 + 压空白（JS 的 stripTags）。"""
    text = _TAGS.sub(" ", str(value if value is not None else ""))
    text = _html.unescape(text)
    return _WS.sub(" ", text).strip()


def trim_text(value, max_len: int) -> str:
    """超长截断成 …结尾（JS 的 trimText）。"""
    text = _WS.sub(" ", str(value if value is not None else "")).strip()
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


# 源站/搜索摘要把长链接「显示截断」时留下的痕迹。推特 status id、小红书 token
# 少一位就 404，所以这种链接宁可不报，也不能原样发给用户。
# 覆盖 …(U+2026)、⋯(U+22EF)、三个及以上 ASCII 点、···(中文间隔号连用)。
_ELLIPSIS_RE = re.compile(r"…|⋯|\.{3,}|·{3,}")


def looks_truncated_url(url) -> bool:
    """URL 里带省略号 —— 它是被显示截断过的半截链接，点开必然打不开。

    注意只判「省略号」，不碰普通中文标点：URL 里合法的 `?`/`&`/`%` 等一概不动。
    """
    return bool(_ELLIPSIS_RE.search(str(url if url is not None else "")))


def abs_url(href, base: str) -> str:
    """补全相对链接；补不全返回空串（JS 的 absUrl）。"""
    try:
        return urljoin(base, _html.unescape(str(href)))
    except (ValueError, AttributeError):
        return ""


def host_of(url) -> str:
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


# 只清掉「分享/埋点」参数，别的一概不动：小红书的 xsec_token 少了就打不开笔记。
_TRACKING_PARAMS = frozenset(
    {"s", "t", "ref_src", "ref_url", "share_source", "share_medium", "spm", "from_source"}
)


def strip_tracking(url) -> str:
    """去掉链接上的分享/埋点参数（`?s=20&t=abc`），让人一眼能看出这是哪一条。

    只碰已知的埋点参数；不认识的 query 一律保留 —— 有些站点的笔记/作品 id 就藏在里面。
    """
    text = str(url if url is not None else "").strip()
    if "?" not in text:
        return text
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment)
    )
