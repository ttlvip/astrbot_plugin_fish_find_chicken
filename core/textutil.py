"""文本小工具：去标签、截断、补全链接、取域名。"""

from __future__ import annotations

import html as _html
import re
from urllib.parse import urljoin, urlsplit

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
