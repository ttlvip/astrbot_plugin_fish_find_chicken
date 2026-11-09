"""网络层：aiohttp 请求 + 代理 + 图片下载。

代理是这套插件能不能用的关键：谷歌、YouTube 这些在国内直连不通，用户通常会在
AstrBot 的全局配置里填一个 http_proxy。这里统一把代理收敛成**一个可选的字符串**，
所有出站请求都显式传 `proxy=`：

* 传了代理 → 只走代理（不信环境变量，避免 NO_PROXY / 系统代理互相打架）
* 没传代理 → 显式 `proxy=None`，走直连（同样不看环境变量，行为可预测）

另一个约束是**别把用户图片发到奇怪的地方**：用户给的图片网址要过 SSRF 检查，
内网/环回/字面量私有 IP 一律拒绝，防止有人拿「图片网址」当探测器扫内网。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import uuid
from urllib.parse import urlsplit

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 45.0


class HttpError(RuntimeError):
    """网络层错误（统一中文 message，可直接进回话）。"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class HttpResult:
    __slots__ = ("status", "body", "content_type", "url", "headers")

    def __init__(self, status: int, body: bytes, content_type: str, url: str = "", headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.url = url
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


# ── 代理 ──────────────────────────────────────────────────────────────────
def normalize_proxy(proxy: str | None) -> str:
    """把各种写法收敛成 aiohttp 认的形式；空串表示直连。

    用户可能填 `127.0.0.1:7890`（漏了 scheme）、`http://user:pass@host:port`、
    或者 socks5 地址。socks 需要 aiohttp-socks，这里明确拒绝而不是静默失败。
    """
    text = str(proxy or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    scheme = urlsplit(text).scheme.lower()
    if scheme not in ("http", "https"):
        raise HttpError(f"暂不支持 {scheme} 代理，请用 http 代理（如 http://127.0.0.1:7890）")
    return text


def proxy_host(proxy: str) -> str:
    """日志里只显示主机端口，不打印账号密码。"""
    if not proxy:
        return ""
    try:
        parts = urlsplit(normalize_proxy(proxy))
        if not parts.hostname:
            return "已配置"
        port = f":{parts.port}" if parts.port else ""
        auth = "***@" if parts.username else ""
        return f"{parts.scheme}://{auth}{parts.hostname}{port}"
    except (ValueError, HttpError):
        return "已配置"


def _timeout(seconds: float):
    import aiohttp

    return aiohttp.ClientTimeout(total=seconds)


def _session(*, headers: dict, timeout: float):
    import aiohttp

    return aiohttp.ClientSession(
        timeout=_timeout(timeout),
        headers={"User-Agent": DEFAULT_UA, **headers},
        # 不跟随环境变量里的代理设置：走不走代理由调用方明确指定
        trust_env=False,
    )


async def request(
    method: str,
    url: str,
    *,
    proxy: str = "",
    headers: dict[str, str] | None = None,
    data=None,
    params: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_redirects: bool = True,
    insecure: bool = False,
) -> HttpResult:
    """发一个请求并把整个响应读回内存（反查结果的页面都不大）。"""
    px = normalize_proxy(proxy)
    try:
        async with _session(headers=headers or {}, timeout=timeout) as session:
            async with session.request(
                method,
                url,
                data=data,
                params=params,
                proxy=px or None,
                allow_redirects=allow_redirects,
                # 内网自签证书的 https 端点（如反代后的 CDP 端口）要显式放行，
                # 否则报 SSLCertVerificationError，看起来像"连不上"。
                ssl=False if (insecure and url.lower().startswith("https://")) else None,
            ) as resp:
                body = await resp.read()
                return HttpResult(
                    resp.status,
                    body,
                    (resp.headers.get("Content-Type") or "").split(";")[0].strip(),
                    str(resp.url),
                    dict(resp.headers),
                )
    except asyncio.TimeoutError as exc:
        raise HttpError(f"请求超时（{int(timeout)} 秒）：{_brief(url)}", code="TIMEOUT") from exc
    except HttpError:
        raise
    except Exception as exc:  # noqa: BLE001 - aiohttp 的异常类型很多，统一转成中文
        raise HttpError(_describe_net_error(exc, url), code="NET") from exc


async def post_multipart(
    url: str,
    *,
    file_field: str,
    filename: str,
    content: bytes,
    content_type: str,
    fields: list[tuple[str, str]] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    proxy: str = "",
) -> HttpResult:
    """POST 一个 multipart 表单（图片 + 若干普通字段）。"""
    import aiohttp

    form = aiohttp.FormData()
    for name, value in fields or []:
        form.add_field(name, str(value))
    form.add_field(file_field, content, filename=filename, content_type=content_type)
    try:
        return await request("POST", url, proxy=proxy, headers=headers, data=form, timeout=timeout)
    except HttpError as exc:
        # aiohttp 各版本的 FormData 序列化行为不一致（实测 3.14 上偶发
        # `Can not serialize value type: <class 'bytes'>`，把图片当成普通字段写），
        # 这类错误跟网络无关，退回手搓 multipart 再发一次，别让整条引擎挂掉。
        if "serialize" not in str(exc).lower():
            raise
    return await asyncio.to_thread(
        _post_multipart_urllib,
        url,
        file_field,
        filename,
        content,
        content_type,
        fields or [],
        {"User-Agent": DEFAULT_UA, **(headers or {})},
        timeout,
        proxy,
    )


def _post_multipart_urllib(url, file_field, filename, content, content_type, fields, headers,
                           timeout, proxy) -> HttpResult:
    """手搓 multipart 的兜底实现（不依赖 aiohttp 的 FormData）。"""
    import urllib.request

    boundary = f"----fishfind{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    chunks.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode()
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(chunks)

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return HttpResult(resp.status, resp.read(),
                              (resp.headers.get("Content-Type") or "").split(";")[0].strip())
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, exc.read() or b"",
                          (exc.headers.get("Content-Type") or "").split(";")[0].strip()
                          if exc.headers else "")


def _brief(url: str, limit: int = 60) -> str:
    text = str(url or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe_net_error(exc: Exception, url: str) -> str:
    """把 aiohttp / ssl 的异常翻译成用户能看懂的一句话。"""
    name = exc.__class__.__name__
    # 有的异常会把整个请求体（图片字节）repr 进 message 里，直接塞进日志会刷屏几 MB。
    detail = (str(exc) or name)[:300]
    host = ""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        pass
    lowered = detail.lower()
    if "certificate" in lowered or isinstance(exc, ssl.SSLError):
        return f"访问 {host or '目标站点'} 时证书校验失败：{detail}"
    if "cannot connect" in lowered or "connection" in lowered and "refused" in lowered:
        return f"连不上 {host or '目标站点'}（网络或代理不通）"
    if "name" in lowered and "resolve" in lowered:
        return f"域名解析失败：{host or _brief(url)}"
    if "proxy" in lowered:
        return f"代理不可用（{host or _brief(url)}）：{detail}"
    return f"请求失败（{host or _brief(url)}）：{detail}"


# ── 图片下载 ──────────────────────────────────────────────────────────────
_MAX_REDIRECTS = 5
_CHUNK_SIZE = 64 * 1024


async def download_image(
    url: str,
    *,
    proxy: str = "",
    max_bytes: int = 20 * 1024 * 1024,
    timeout: float = 30.0,
    allow_private: bool = False,
) -> tuple[bytes, str]:
    """下载一张图，返回 (bytes, mime)。

    `allow_private=False` 时会拒绝内网地址 —— 图片网址来自群里的陌生人，不能当扫描器用。
    重定向必须手动跟随并每跳重检：aiohttp 默认 allow_redirects 会绕过首跳 SSRF 检查。
    响应按块读，超过 max_bytes 立刻断开，避免对方先吐几百 MB 再报「图太大」。
    """
    target = str(url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        raise HttpError("图片地址必须是 http/https 网址")

    px = normalize_proxy(proxy)
    headers = {"Accept": "image/*,*/*;q=0.8", "Referer": "", "User-Agent": DEFAULT_UA}
    seen: set[str] = set()

    try:
        async with _session(headers=headers, timeout=timeout) as session:
            for _ in range(_MAX_REDIRECTS + 1):
                if target in seen:
                    raise HttpError("图片下载重定向形成循环")
                seen.add(target)
                if not allow_private:
                    await ensure_public_host(target)

                async with session.request(
                    "GET",
                    target,
                    proxy=px or None,
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location") or ""
                        if not location:
                            raise HttpError(f"下载图片失败 HTTP {resp.status}（无 Location）")
                        target = _absolute_url(str(resp.url), location)
                        if not target.lower().startswith(("http://", "https://")):
                            raise HttpError("图片重定向后不是 http/https 网址")
                        continue

                    if resp.status != 200:
                        raise HttpError(f"下载图片失败 HTTP {resp.status}")
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith("image/"):
                        raise HttpError(f"那个地址不是图片（{ctype}）")

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise HttpError(
                                f"图太大了（超过 {max_bytes // 1048576}MB）"
                            )
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        raise HttpError("图片是空的")
                    return data, sniff_mime(data, ctype or "image/jpeg")

            raise HttpError(f"图片下载重定向超过 {_MAX_REDIRECTS} 次")
    except HttpError:
        raise
    except asyncio.TimeoutError as exc:
        raise HttpError(f"请求超时（{int(timeout)} 秒）：{_brief(url)}", code="TIMEOUT") from exc
    except Exception as exc:  # noqa: BLE001
        raise HttpError(_describe_net_error(exc, url), code="NET") from exc


def _absolute_url(base: str, location: str) -> str:
    """把相对 Location 收成绝对 URL。"""
    from urllib.parse import urljoin

    return urljoin(base, location.strip())


def sniff_mime(data: bytes, fallback: str = "image/jpeg") -> str:
    """按文件头认格式，不信扩展名和 Content-Type —— 上传接口按这个判类型。"""
    head = data[:16]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:3] == b"GIF":
        return "image/gif"
    if head[:2] == b"BM":
        return "image/bmp"
    return fallback


def suffix_for(mime: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/bmp": "bmp",
    }.get((mime or "").lower(), "jpg")


async def ensure_public_host(url: str) -> None:
    """拒绝指向内网/环回/保留地址的图片网址（DNS 解析后再判一次，防 DNS 重绑定太复杂，够用）。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError as exc:
        raise HttpError("图片地址解析失败") from exc
    if not host:
        raise HttpError("图片地址里没有域名")
    if host.lower() in ("localhost", "localhost.localdomain"):
        raise HttpError("不接受本机地址作为图片网址")

    # 字面量 IP 直接判，省一次 DNS
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not _ip_is_public(ip):
            raise HttpError("不接受内网地址作为图片网址")
        return

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise HttpError(f"域名解析失败：{host}") from exc
    for info in infos:
        addr = str(info[4][0]).split("%")[0]
        try:
            if not _ip_is_public(ipaddress.ip_address(addr)):
                raise HttpError("不接受内网地址作为图片网址")
        except ValueError:
            continue


def _ip_is_public(ip) -> bool:
    """只放行公网全局地址。

    ``is_private`` 放不过 CGNAT（100.64.0.0/10），``is_global`` 会拦；
    同时继续显式拒绝环回 / 链路本地 / 组播，避免个别 Python 版本对边界地址判断不一致。
    """
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or ip.is_private
    ):
        return False
    return bool(getattr(ip, "is_global", True))
