"""插件配置：把 AstrBot 的配置字典收敛成一个不可变的小对象。

配置有两个来源，优先级从高到低：

1. 插件自己的配置（WebUI 里填的 `proxy`、`engines` 等）
2. AstrBot 全局配置的 `http_proxy`（用户为了跑大模型通常已经填好，插件直接复用）
3. 环境变量（最后兜底，Docker 里常这么传）

**代理是这套插件的命门**：谷歌 Lens 在国内直连不通，只有拿到代理才能实现
「发图 → 认出这是哪个 X 用户」。所以 `use_astrbot_proxy` 默认开着。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .net import normalize_proxy

# 能用的引擎。lens 是主力（真人照片只有它认得出人），另外三个是补充：
# 动漫图走 SauceNAO / IQDB，动画截图走 trace.moe。
ENGINE_IDS: tuple[str, ...] = ("lens", "saucenao", "tracemoe", "iqdb")

ENGINE_LABELS = {
    "lens": "谷歌 Lens",
    "saucenao": "SauceNAO",
    "tracemoe": "动漫截图溯源",
    "iqdb": "IQDB",
}

DEFAULT_ENGINES = ["lens", "saucenao", "tracemoe"]

_ENV_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    if value is None:
        return default
    return bool(value)


def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass
class Settings:
    """一次反查需要的全部参数。"""

    enabled: bool = True
    engines: list[str] = field(default_factory=lambda: list(DEFAULT_ENGINES))
    timeout_sec: int = 45
    max_accounts: int = 3
    strict_consensus: bool = True

    proxy: str = ""
    proxy_source: str = "无"
    use_astrbot_proxy: bool = True

    browser_exe: str = ""
    browser_headless: bool = True
    browser_profile: str = ""
    browser_cdp: str = ""
    browser_cdp_insecure: bool = False
    google_cookie: str = ""
    serpapi_enabled: bool = False
    serpapi_api_key: str = ""
    lens_max_resolve: int = 10

    cooldown_sec: int = 30
    per_user_day: int = 20
    global_day: int = 100
    cache_days: int = 30

    notify_start: bool = True
    send_on_miss: bool = True
    show_engine_detail: bool = True
    found_image: str = ""

    max_image_mb: int = 20
    max_concurrent: int = 1
    allow_url_query: bool = True
    allow_local_path: bool = False

    group_whitelist: list[str] = field(default_factory=list)
    group_blacklist: list[str] = field(default_factory=list)

    # ── 构造 ────────────────────────────────────────────────────────────
    @classmethod
    def from_config(cls, cfg: dict | None, global_cfg: dict | None = None) -> "Settings":
        cfg = cfg if isinstance(cfg, dict) else {}
        global_cfg = global_cfg if isinstance(global_cfg, dict) else {}

        engines = [e.lower() for e in _as_list(cfg.get("engines")) if e.lower() in ENGINE_IDS]
        if not engines:
            engines = list(DEFAULT_ENGINES)

        explicit = str(cfg.get("proxy") or "").strip()
        use_global = _as_bool(cfg.get("use_astrbot_proxy"), True)
        proxy, source = "", "无"
        if explicit:
            proxy, source = explicit, "插件配置"
        elif use_global:
            inherited = str(global_cfg.get("http_proxy") or "").strip()
            if inherited:
                proxy, source = inherited, "AstrBot 全局 http_proxy"
        if not proxy:
            env = _env_proxy()
            if env:
                proxy, source = env, "环境变量"
        try:
            proxy = normalize_proxy(proxy)
        except Exception:  # noqa: BLE001 - 配错了不该让插件加载失败，退回直连并保留原文
            source = f"{source}（格式不认识，已按直连处理）"
            proxy = ""

        return cls(
            enabled=_as_bool(cfg.get("enabled"), True),
            engines=engines,
            timeout_sec=max(5, _as_int(cfg.get("timeout_sec"), 45)),
            max_accounts=max(1, _as_int(cfg.get("max_accounts"), 3)),
            strict_consensus=_as_bool(cfg.get("strict_consensus"), True),
            proxy=proxy,
            proxy_source=source,
            use_astrbot_proxy=use_global,
            browser_exe=str(cfg.get("browser_exe") or "").strip(),
            browser_headless=_as_bool(cfg.get("browser_headless"), True),
            browser_profile=str(cfg.get("browser_profile") or "").strip(),
            browser_cdp=str(cfg.get("browser_cdp") or "").strip(),
            browser_cdp_insecure=_as_bool(cfg.get("browser_cdp_insecure"), False),
            google_cookie=str(cfg.get("google_cookie") or "").strip(),
            serpapi_enabled=_as_bool(cfg.get("serpapi_enabled"), False),
            serpapi_api_key=str(cfg.get("serpapi_api_key") or "").strip(),
            lens_max_resolve=max(1, min(24, _as_int(cfg.get("lens_max_resolve"), 10))),
            cooldown_sec=max(0, _as_int(cfg.get("cooldown_sec"), 30)),
            per_user_day=max(0, _as_int(cfg.get("per_user_day"), 20)),
            global_day=max(0, _as_int(cfg.get("global_day"), 100)),
            cache_days=max(0, _as_int(cfg.get("cache_days"), 30)),
            notify_start=_as_bool(cfg.get("notify_start"), True),
            send_on_miss=_as_bool(cfg.get("send_on_miss"), True),
            show_engine_detail=_as_bool(cfg.get("show_engine_detail"), True),
            found_image=str(cfg.get("found_image") or "").strip(),
            max_image_mb=max(1, _as_int(cfg.get("max_image_mb"), 20)),
            max_concurrent=max(1, _as_int(cfg.get("max_concurrent"), 1)),
            allow_url_query=_as_bool(cfg.get("allow_url_query"), True),
            allow_local_path=_as_bool(cfg.get("allow_local_path"), False),
            group_whitelist=_as_list(cfg.get("group_whitelist")),
            group_blacklist=_as_list(cfg.get("group_blacklist")),
        )

    # ── 便捷属性 ────────────────────────────────────────────────────────
    @property
    def max_image_bytes(self) -> int:
        return self.max_image_mb * 1024 * 1024

    @property
    def lens_enabled(self) -> bool:
        return "lens" in self.engines

    @property
    def serpapi_ready(self) -> bool:
        """开关打开**且**填了 key 才算能用；否则一律回退本地浏览器。"""
        return bool(self.serpapi_enabled and self.serpapi_api_key)

    def engine_labels(self, engines=None) -> list[str]:
        return [ENGINE_LABELS.get(str(e), str(e)) for e in (engines if engines is not None else self.engines)]


def _env_proxy() -> str:
    for key in _ENV_PROXY_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return ""
