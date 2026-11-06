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
