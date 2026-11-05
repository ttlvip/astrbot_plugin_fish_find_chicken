"""SerpApi 的 Google Lens 通道（可选，用来绕开本地浏览器的人机验证）。

为什么要有这条路：谷歌对「机房 IP + 无头浏览器 + 高频查询」会要求人机验证，自建环境很难稳定。
SerpApi 用自己的池子和账号去抓 Lens，插件只调一次 HTTP 接口就能拿到结果 —— 不需要浏览器，
不需要 token 解析，也不会被验证码挡住。代价是按次计费。

接口（已按官方文档核对）：

1. 上传图片：`POST https://serpapi.com/image`，multipart 字段 `image`，同时带 `api_key`。
   返回 `{"message": ..., "image_id": "..."}`，image_id 10 分钟内有效。
   **限制：jpg/png/webp，最大 500KB** —— 所以本地图片要先压缩（见 `shrink_to_limit`）。
2. 搜索：`GET https://serpapi.com/search?engine=google_lens&image_id=...&api_key=...`。
   返回里 `visual_matches[]` 就是「视觉匹配」，每条有 title / link / source，链路已是真实地址。

相比本地浏览器模式的两个好处：① 一次请求拿到全部结果，不用逐条解析 `/goto` token；
② 结果里的 `link` 直接是来源页地址，能直接被账号抽取用。
"""

from __future__ import annotations

import json
from io import BytesIO
from urllib.parse import parse_qs, unquote, urlparse

from .net import HttpError, HttpResult, post_multipart, request
from .textutil import host_of, trim_text

SERPAPI_IMAGE_URL = "https://serpapi.com/image"
SERPAPI_SEARCH_URL = "https://serpapi.com/search"

# SerpApi 的硬限制：上传图片不能超过 500KB
IMAGE_LIMIT_BYTES = 500 * 1024

# 压缩时依次尝试的长边与质量。从「看不出来被压过」开始，逐级往下降，够小就停。
_SHRINK_STEPS = (
    (1600, 88),
    (1280, 84),
    (1280, 78),
    (1024, 80),
    (1024, 72),
    (896, 74),
    (768, 72),
    (640, 68),
)


class SerpApiError(RuntimeError):
    """SerpApi 调用失败。code: NO_KEY / AUTH / QUOTA / UPLOAD / NET / TOO_LARGE / EMPTY。"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def shrink_to_limit(
    content: bytes, mime_type: str, *, limit: int = IMAGE_LIMIT_BYTES
) -> tuple[bytes, str]:
    """把图片压到 SerpApi 的 500KB 限制以内。

    只在超限时才动图；已经够小就原样返回，避免白白损失清晰度（Lens 对清晰度敏感）。
    没有 Pillow 时抛清楚的错误，而不是让用户对着「上传失败」猜。
    """
    if not content:
        raise SerpApiError("图片是空的", code="UPLOAD")
    if len(content) <= limit:
        return content, mime_type or "image/jpeg"

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - AstrBot 镜像自带 Pillow
        raise SerpApiError(
            f"图片 {len(content) // 1024}KB 超过 SerpApi 的 500KB 上限，"
            "但环境里没有 Pillow 无法压缩（pip install pillow）",
            code="TOO_LARGE",
        ) from exc

    try:
        image = Image.open(BytesIO(content))
        image.load()
    except Exception as exc:  # noqa: BLE001 - PIL 的异常类型很杂
        raise SerpApiError(f"图片读不出来，无法压缩：{exc}", code="UPLOAD") from exc

    # JPEG 不支持透明通道，统一转 RGB（PNG 截图也走这条路）
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    for edge, quality in _SHRINK_STEPS:
        candidate = image.copy()
        candidate.thumbnail((edge, edge))
        buffer = BytesIO()
        candidate.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
        data = buffer.getvalue()
        if len(data) <= limit:
            return data, "image/jpeg"

    raise SerpApiError(
        f"这张图压到 {limit // 1024}KB 以下会严重失真，SerpApi 可能也认不出来；"
        "建议换一张更小的原图",
        code="TOO_LARGE",
    )


def _unwrap_google_redirect(url: str) -> str:
    """SerpApi 偶尔会把 lens 的中转链接原样给出，能解就解开。"""
    text = str(url or "").strip()
    if not text:
        return ""
    host = host_of(text)
    if host and ("google." in host):
        query = parse_qs(urlparse(text).query)
        for key in ("url", "q", "imgrefurl"):
            values = query.get(key) or []
            if values and values[0].startswith("http"):
                return unquote(values[0])
    return text


def parse_search_payload(payload: dict) -> list[dict]:
    """把 SerpApi 的 JSON 转成插件内部的候选结构。

    主结果取 `visual_matches`（视觉匹配，谷歌按相关性排好序了）；
    真的一条都没有时再看 `organic_results`（自然搜索结果），
    它更偏「这图讲的是什么」，但总比空手强。
    """
    if not isinstance(payload, dict):
        return []

    candidates: list[dict] = []
    seen: set[str] = set()

    def push(url: str, title: str, source: str, *, weak: bool = False) -> None:
        link = _unwrap_google_redirect(url)
        if not link.startswith("http"):
            return
        key = link.split("#")[0]
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            {
                "engine": "lens",
                "similarity": None,
                "title": trim_text(title, 200),
                "sourceUrl": key,
                "authorUrl": "",
                "host": host_of(key),
                "snippet": trim_text(source, 60),
                "links": [key],
                "siteName": trim_text(source, 60),
                **({"weak": True} if weak else {}),
            }
        )

    for item in payload.get("visual_matches") or []:
        if not isinstance(item, dict):
            continue
        push(
            str(item.get("link") or ""),
            str(item.get("title") or ""),
            str(item.get("source") or ""),
        )

    if not candidates:
        for item in payload.get("organic_results") or []:
            if not isinstance(item, dict):
                continue
            # 自然搜索结果只能算弱证据：它说的是「这张图出现在哪些文章里」
            push(
                str(item.get("link") or item.get("redirect_link") or ""),
                str(item.get("title") or ""),
                str(item.get("source") or ""),
                weak=True,
            )

    return candidates


def _error_of(payload) -> str:
    if isinstance(payload, dict):
        return str(payload.get("error") or "")
    return ""


def _check_http(result: HttpResult, action: str) -> dict:
    """统一的 HTTP 状态 → 中文错误映射。"""
    if result.status == 401:
        raise SerpApiError("SerpApi 不接受这个 API key（401），检查 key 是否复制完整", code="AUTH")
    if result.status == 403:
        raise SerpApiError("SerpApi 拒绝访问（403），可能是 key 无权限或套餐受限", code="AUTH")
    if result.status == 429:
        raise SerpApiError("SerpApi 额度用完了（429），等下个月或升级套餐", code="QUOTA")
    if result.status >= 400:
        detail = _error_of(_safe_json(result)) or trim_text(result.text, 120)
        raise SerpApiError(f"SerpApi {action}失败（HTTP {result.status}）：{detail}", code="NET")

    payload = _safe_json(result)
    if not isinstance(payload, dict):
        raise SerpApiError(f"SerpApi {action}返回的数据看不懂（不是 JSON）", code="NET")
    error = _error_of(payload)
    if error:
        code = "QUOTA" if ("run out" in error.lower() or "credit" in error.lower()) else "NET"
        raise SerpApiError(f"SerpApi {action}失败：{trim_text(error, 120)}", code=code)
    return payload


def _safe_json(result: HttpResult) -> dict:
    try:
        data = json.loads(result.text or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def upload_image(
    content: bytes, mime_type: str, *, api_key: str, proxy: str = "", timeout: float = 40.0
) -> str:
    """上传图片换 image_id（10 分钟有效）。"""
    data, mime = shrink_to_limit(content, mime_type)
    suffix = "png" if mime == "image/png" else "jpg"
    try:
        result = await post_multipart(
            SERPAPI_IMAGE_URL,
            file_field="image",
            filename=f"image.{suffix}",
            content=data,
            content_type=mime,
            fields=[("api_key", api_key)],
            headers={"Accept": "application/json"},
            timeout=timeout,
            proxy=proxy,
        )
    except HttpError as exc:
        raise SerpApiError(f"上传图片到 SerpApi 失败：{exc}", code="NET") from exc

    payload = _check_http(result, "上传图片")
    image_id = str(payload.get("image_id") or "").strip()
    if not image_id:
        raise SerpApiError("SerpApi 没返回 image_id（上传没成功）", code="UPLOAD")
    return image_id


async def serpapi_lens_search(
    content: bytes,
    mime_type: str,
    *,
    api_key: str,
    proxy: str = "",
    timeout: float = 45.0,
    on_log=None,
) -> list[dict]:
    """走 SerpApi 查一次 Lens，返回与本地浏览器模式同构的候选列表。"""
    key = (api_key or "").strip()
    if not key:
        raise SerpApiError("没配置 SerpApi API key", code="NO_KEY")

    def log(line: str) -> None:
        if on_log:
            try:
                on_log(line)
            except Exception:  # noqa: BLE001 - 日志回调出错不能影响查询
                pass

    image_id = await upload_image(
        content, mime_type, api_key=key, proxy=proxy, timeout=min(40.0, timeout)
    )
    log(f"SerpApi 上传完成，image_id={image_id[:12]}…")

    try:
        result = await request(
            "GET",
            SERPAPI_SEARCH_URL,
            params={"engine": "google_lens", "image_id": image_id, "api_key": key},
            headers={"Accept": "application/json"},
            timeout=timeout,
            proxy=proxy,
        )
    except HttpError as exc:
        raise SerpApiError(f"SerpApi 查询失败：{exc}", code="NET") from exc

    payload = _check_http(result, "查询")
    candidates = parse_search_payload(payload)
    log(f"SerpApi 返回 {len(candidates)} 条视觉匹配")
    if not candidates:
        raise SerpApiError("SerpApi 没有匹配到任何来源（这张图网上可能真没有）", code="EMPTY")
    return candidates
