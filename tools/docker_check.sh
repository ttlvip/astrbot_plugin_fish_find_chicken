#!/usr/bin/env bash
# 鱼鱼找鸡 · 容器内自检
#
# 用法（在宿主机执行，把 astrbot 换成你的容器名）：
#   docker exec -it astrbot bash /AstrBot/data/plugins/astrbot_plugin_fish_find_chicken/tools/docker_check.sh
#
# 它只做检查、不改任何东西。核心能力（图片 → X 账号）依赖三件事：
#   1. 有一个能用的 Chrome —— 本地启动，或配置了 browser_cdp 连一个独立浏览器容器
#   2. 容器能访问谷歌
#   3. data 目录可写
# 三项全绿才算可用；缺哪项，脚本会直接告诉你该补什么。

set -uo pipefail

PLUGIN_DIR="${PLUGIN_DIR:-/AstrBot/data/plugins/astrbot_plugin_fish_find_chicken}"
DATA_DIR="${DATA_DIR:-/AstrBot/data/plugin_data/astrbot_plugin_fish_find_chicken}"
CONFIG_FILE="${CONFIG_FILE:-/AstrBot/data/cmd_config.json}"
PLUGIN_CFG="${PLUGIN_CFG:-/AstrBot/data/config/astrbot_plugin_fish_find_chicken_config.json}"
PROXY="${PROXY:-}"
CDP="${CDP:-}"

pass=0
fail=0
warn=0

ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; fail=$((fail + 1)); }
meh()  { printf '  \033[33m[WARN]\033[0m %s\n' "$1"; warn=$((warn + 1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# 读一个 JSON 文件里的字符串字段（文件可能带 BOM）
json_field() {
    python - "$1" "$2" <<'PYJSON' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8-sig") as fh:
        data = json.load(fh)
    print(str(data.get(sys.argv[2]) or "").strip())
except Exception:
    print("")
PYJSON
}

# 把 CDP 地址归一成 http://host:port
cdp_base() {
    python - "$1" <<'PYJSON' 2>/dev/null
import sys
from urllib.parse import urlsplit
raw = sys.argv[1]
if "://" not in raw:
    raw = "http://" + raw
u = urlsplit(raw)
scheme = "https" if u.scheme in ("https", "wss") else "http"
print(f"{scheme}://{u.hostname}:{u.port or 9222}")
PYJSON
}

# ── 1. 插件本体 ────────────────────────────────────────────────────────────
head_ "1/5 插件文件"
if [ -f "$PLUGIN_DIR/main.py" ] && [ -f "$PLUGIN_DIR/core/lens.py" ]; then
    ver=$(grep -E '^version:' "$PLUGIN_DIR/metadata.yaml" 2>/dev/null | head -1 | awk '{print $2}')
    ok "插件在 $PLUGIN_DIR（version=${ver:-未知}）"
    if [ -d "$PLUGIN_DIR/core" ] && [ -d "$PLUGIN_DIR/pycore" ]; then
        meh "同时存在 core/ 和 pycore/ —— 这是旧 Node 双后端版本的残留，确认部署的是新代码"
    fi
else
    bad "插件不完整：$PLUGIN_DIR 下没有 main.py 或 core/lens.py（检查挂载路径）"
fi

if python -c "import aiohttp" >/dev/null 2>&1; then
    ok "aiohttp $(python -c 'import aiohttp;print(aiohttp.__version__)' 2>/dev/null)"
else
    bad "缺 aiohttp（AstrBot 自带，说明 Python 环境不对）"
fi

# ── 2. 浏览器 ──────────────────────────────────────────────────────────────
head_ "2/5 浏览器（Lens 必需）"

# 插件有两种拿浏览器的路径：本地可执行文件，或连独立跑的 Chrome（browser_cdp）。
if [ -z "$CDP" ] && [ -f "$PLUGIN_CFG" ]; then
    CDP=$(json_field "$PLUGIN_CFG" "browser_cdp")
fi

if [ -n "$CDP" ]; then
    base=$(cdp_base "$CDP")
    ver=$(curl -sS --max-time 8 "$base/json/version" 2>/dev/null || true)
    if printf '%s' "$ver" | grep -q '"webSocketDebuggerUrl"'; then
        ver_name=$(printf '%s' "$ver" | python -c 'import json,sys;print(json.load(sys.stdin).get("Browser",""))' 2>/dev/null || echo "")
        ok "外部浏览器可用：$base（${ver_name:-已连通}）"
    else
        bad "连不上外部浏览器 $base —— 插件会报「连不上外部浏览器」"
        printf '         检查：①浏览器容器带 --remote-debugging-port=9222\n'
        printf '              ②必须同时带 --remote-debugging-address=0.0.0.0（否则只监听它自己的 127.0.0.1）\n'
        printf '              ③两个容器在同一 docker 网络，容器名能被解析\n'
    fi
    printf '         注意：这个浏览器必须自己配好代理（插件无法远程给它设 --proxy-server）\n'
else
    browser=""
    for name in google-chrome google-chrome-stable chromium chromium-browser \
                microsoft-edge microsoft-edge-stable chrome headless_shell chrome-headless-shell; do
        if command -v "$name" >/dev/null 2>&1; then browser=$(command -v "$name"); break; fi
    done
    if [ -n "$browser" ]; then
        ok "找到 $browser"
    else
        bad "容器里没有任何 Chrome/Chromium —— 图片反查这个核心功能会不可用"
        printf '         办法一：apt-get update && apt-get install -y --no-install-recommends chromium\n'
        printf '         办法二：另起一个 Chrome 容器，插件配置里填 browser_cdp = http://chrome:9222\n'
    fi

    if [ -n "$browser" ]; then
        # which 通过不代表能跑：容器里缺库或沙箱没关都会秒退。这里用插件自己的
        # CDP 客户端真连一次，和线上路径完全一致。
        probed=$(PLUGIN_DIR="$PLUGIN_DIR" python - "$browser" <<'PYCDP' 2>&1
import asyncio, os, sys
sys.path.insert(0, os.environ["PLUGIN_DIR"])
try:
    from core.browser import get_browser, close_browser
except Exception as exc:
    print(f"IMPORT_ERROR {exc}")
    raise SystemExit(0)


async def main():
    try:
        b = await get_browser(exe=sys.argv[1], headless=True)
    except Exception as exc:
        print(f"START_FAIL {type(exc).__name__}: {exc}")
        return
    try:
        ua = await b.eval("navigator.userAgent")
        print("OK " + str(ua)[:80])
    except Exception as exc:
        print(f"EVAL_FAIL {type(exc).__name__}: {exc}")
    finally:
        await close_browser()


asyncio.run(main())
PYCDP
)
        case "$probed" in
            OK*)       ok "浏览器能启动并接受 CDP 控制：${probed#OK }" ;;
            IMPORT_ERROR*) bad "插件代码导入失败：${probed#IMPORT_ERROR }" ;;
            START_FAIL*) bad "浏览器起不来：${probed#START_FAIL }"
                         printf '         常见原因：/dev/shm 太小、缺系统库（ldd %s | grep "not found"）\n' "$browser" ;;
            *)         bad "浏览器启动了但页面脚本执行失败：${probed}" ;;
        esac
    fi
fi

# ── 3. 代理 ────────────────────────────────────────────────────────────────
head_ "3/5 代理与谷歌连通性"
if [ -z "$PROXY" ]; then
    PROXY="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}"
fi
proxy_source="环境变量"
if [ -z "$PROXY" ]; then
    proxy_source="AstrBot 全局配置"
    if [ -f "$CONFIG_FILE" ]; then
        PROXY=$(json_field "$CONFIG_FILE" "http_proxy")
    fi
fi

if [ -z "$PROXY" ]; then
    warn "没发现代理配置（环境变量与 AstrBot 全局 http_proxy 都是空）"
    printf '         国内直连谷歌不通 → Lens 会失败。给容器加 http_proxy，或在 WebUI 设置里填\n'
else
    masked=$(python - "$PROXY" <<'PYMASK' 2>/dev/null
import sys
from urllib.parse import urlsplit
p = sys.argv[1]
if "://" not in p:
    p = "http://" + p
try:
    u = urlsplit(p)
    print(f"{u.scheme}://{'***@' if u.username else ''}{u.hostname}:{u.port or ''}")
except Exception:
    print("已配置")
PYMASK
)
    ok "代理：$masked（来源：$proxy_source）"
    if printf '%s' "$PROXY" | grep -qE '127\.0\.0\.1|localhost'; then
        meh "代理地址是本机回环 —— 在容器里 127.0.0.1 指容器自己，不是宿主机"
        printf '         除非用了 network_mode: host，否则要写宿主机 LAN IP 或 host.docker.internal\n'
    fi
fi

if [ -n "$PROXY" ]; then
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 12 -x "$PROXY" https://www.google.com/ 2>/dev/null || true)
    case "$code" in
        200|301|302|303) ok "容器经代理能到谷歌（HTTP $code）" ;;
        000|"") bad "容器经代理到不了谷歌（超时/连接失败）—— 检查 allow-lan、防火墙、代理是否在跑" ;;
        *)     warn "谷歌返回 HTTP $code（可能被限流或需要验证，Lens 可能不稳定）" ;;
    esac
fi

# ── 4. 数据目录 ────────────────────────────────────────────────────────────
head_ "4/5 数据目录"
mkdir -p "$DATA_DIR" 2>/dev/null || true
if touch "$DATA_DIR/.write_test" 2>/dev/null; then
    rm -f "$DATA_DIR/.write_test"
    ok "$DATA_DIR 可写（状态文件与浏览器 profile 会落在这里）"
else
    bad "$DATA_DIR 不可写 —— data 卷可能是 :ro 或权限不对，配额/缓存/浏览器 profile 都会失败"
fi

# ── 5. 汇总 ────────────────────────────────────────────────────────────────
head_ "汇总"
printf '  PASS %d   FAIL %d   WARN %d\n' "$pass" "$fail" "$warn"
if [ "$fail" -eq 0 ]; then
    printf '\n  \033[32m核心能力可用\033[0m：在群里引用一张图发 /鱼鱼找鸡 就能试；再发 /找鸡自检 复核插件侧状态。\n'
    exit 0
fi
printf '\n  \033[31m有 %d 项没通过\033[0m：上面标 FAIL 的就是要补的。\n' "$fail"
printf '  只影响「图片→X 账号」的是浏览器与代理这两项；它们不通时，插件仍会用 SauceNAO/IQDB 兜底（只能查动漫图）。\n'
exit 1
