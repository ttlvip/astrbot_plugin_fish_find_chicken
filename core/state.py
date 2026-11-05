"""状态持久化：冷却、每日配额、结果缓存。

为什么要有配额：Lens 是免费服务，但 IP 被刷狠了会直接给人机验证，一次就把所有人挡住；
SauceNAO 更是 403 限流。所以默认「同一人 30 秒一次、单人每天 20 次、全站每天 100 次」。
缓存按图片内容哈希存 30 天 —— 群里同一张图被反复转发是常态，命中缓存不扣配额。

状态文件放 AstrBot 的 data 目录（插件目录会在更新/重装时被覆盖），写入用「临时文件 + 替换」
保证不会因为进程被杀留下半个 JSON。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BEIJING = timezone(timedelta(hours=8))


def day_key(now: float | None = None) -> str:
    """按北京时间分天 —— 用户在国内，按 UTC 分天会出现「晚上 8 点就重置」。"""
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts, _BEIJING).strftime("%Y-%m-%d")


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {"last": {}, "day": {"user": {}, "global": {}}, "cache": {}}
        self._lock = asyncio.Lock()

    # ── 读写 ────────────────────────────────────────────────────────────
    async def load(self) -> None:
        async with self._lock:
            self._data = self._read()

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("last", {})
        raw.setdefault("cache", {})
        day = raw.get("day")
        if not isinstance(day, dict):
            day = {}
        day.setdefault("user", {})
        day.setdefault("global", {})
        raw["day"] = day
        return raw

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except Exception:  # noqa: BLE001 - 写失败不该让查询失败
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ── 配额 ────────────────────────────────────────────────────────────
    def quota_check(
        self,
        *,
        key: str,
        user_id: str,
        is_admin: bool = False,
        cooldown_sec: int = 30,
        per_user_day: int = 20,
        global_day: int = 100,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """能不能查。第二个返回值直接能拿来回话。"""
        if is_admin:
            return True, ""
        ts = time.time() if now is None else now

        if cooldown_sec > 0:
            last = float(self._data.get("last", {}).get(key) or 0)
            if last and ts - last < cooldown_sec:
                return False, f"慢点，{max(1, int(cooldown_sec - (ts - last)))} 秒后再发"

        today = day_key(ts)
        day = self._data["day"]
        if per_user_day > 0:
            entry = day["user"].get(str(user_id)) or {}
            if entry.get("day") == today and int(entry.get("count") or 0) >= per_user_day:
                return False, f"今天的 {per_user_day} 次用完了，明天再来"
        if global_day > 0:
            entry = day["global"] or {}
            if entry.get("day") == today and int(entry.get("count") or 0) >= global_day:
                return False, f"今天全站 {global_day} 次用完了，明天再来"
        return True, ""

    async def charge(
        self, *, key: str, user_id: str, is_admin: bool = False, now: float | None = None
    ) -> None:
        """扣一次配额。管理员不扣 —— 免得自己调试把当天的额度用光。"""
        if is_admin:
            return
        ts = time.time() if now is None else now
        today = day_key(ts)
        async with self._lock:
            self._data.setdefault("last", {})[key] = ts
            day = self._data.setdefault("day", {"user": {}, "global": {}})
            users = day.setdefault("user", {})
            uid = str(user_id)
            prev = users.get(uid) or {}
            users[uid] = {
                "day": today,
                "count": (int(prev.get("count") or 0) if prev.get("day") == today else 0) + 1,
            }
            glob = day.get("global") or {}
            day["global"] = {
                "day": today,
                "count": (int(glob.get("count") or 0) if glob.get("day") == today else 0) + 1,
            }
            self._prune(ts, today)
            self._write()

    def _prune(self, now: float, today: str) -> None:
        """顺手清理过期条目，避免状态文件无限膨胀。"""
        last = self._data.get("last", {})
        if len(last) > 2000:
            cutoff = now - 86400
            self._data["last"] = {k: v for k, v in last.items() if float(v or 0) >= cutoff}
        users = self._data.get("day", {}).get("user", {})
        if len(users) > 5000:
            self._data["day"]["user"] = {
                k: v for k, v in users.items() if (v or {}).get("day") == today
            }

    # ── 缓存 ────────────────────────────────────────────────────────────
    def cache_get(self, cache_key: str, *, cache_days: int = 30, now: float | None = None):
        if cache_days <= 0:
            return None
        entry = self._data.get("cache", {}).get(cache_key)
        if not isinstance(entry, dict):
            return None
        ts = time.time() if now is None else now
        age_days = (ts - float(entry.get("at") or 0)) / 86400
        if age_days < 0 or age_days > cache_days:
            return None
        result = entry.get("result")
        return result if isinstance(result, dict) else None

    async def cache_put(self, cache_key: str, result: dict, *, cache_max: int = 500) -> None:
        async with self._lock:
            cache = self._data.setdefault("cache", {})
            cache[cache_key] = {"at": time.time(), "result": result}
            if len(cache) > max(1, cache_max):
                ordered = sorted(cache.items(), key=lambda kv: float(kv[1].get("at") or 0))
                for stale_key, _ in ordered[: len(cache) - cache_max]:
                    cache.pop(stale_key, None)
            self._write()
