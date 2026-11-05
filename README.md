# 鱼鱼找鸡 · AstrBot 插件

给一张图，反查这人是哪个 coser、账号在哪。

> **一句话原理**：把图交给谷歌 Lens 反查原帖，从结果里抽社交媒体账号（X / 微博 / B站 / Instagram / 小红书），
> 再用 SauceNAO / trace.moe / IQDB 补充动漫图和动画截图的场景。不调用任何大模型，零 token。

---

## 一、它到底怎么工作的

```
群里有人引用一张图 → /鱼鱼找鸡
        │
        ├─ 1. 取图（被引用的消息 / 本条消息 / 图片网址）→ 落地成临时文件
        │
        ├─ 2. 反查（并发）
        │     ├─ 谷歌 Lens ★主力：必须在浏览器里上传图片 → 渲染结果页 → 逐条解析出真实域名
        │     ├─ SauceNAO   ：动漫/同人图，能直接给出 pixiv 作者行
        │     ├─ trace.moe  ：动画截图 → 番剧与集数
        │     └─ IQDB       ：动漫图库（danbooru / konachan / yande.re）
        │
        ├─ 3. 抽账号：只认 URL 证据，逐条收敛到「主页」，同一条原帖只算一个人，
        │            再用「名字共识」挡掉别人的号（宁可少报，不报错人）
        │
        └─ 4. 回话：🎯 / 🌐 平台 / 👤 名字 / 🆔 账号 · 主页链接
```

**Google Lens 是这套插件的命门**：真人照片只有它能从「一张图」走到「某个人的 X 主页」。
所以：

* 必须能访问谷歌 → 用 AstrBot 全局 `http_proxy`（或插件自己的 `proxy`）；
* 必须本机有 Chrome / Edge → 它在浏览器里上传图片（**实测：用 HTTP 直接传图会让结果页返回「没有与您搜索相匹配的内容」**，因为上传会话与浏览器 cookie 不绑定）；
* 结果页的卡片链接是谷歌的 opaque token（`/goto?url=CAES...`），解码不出来，只能用浏览器里同一套 cookie 去 `GET` 一次、读 302 的 `Location`（0.2~0.6 秒/条）；这条路走不通时退回「开标签页真跳一次」（1~4 秒/条）。插件按「社交平台优先」解析若干条，默认 10 条。

没有 Chrome/Edge 或没有代理时，Lens 会跳过并在回话里说明原因，其余三个引擎照常工作（只是查不到真人）。

## 二、装之前

| 需要 | 说明 |
|---|---|
| AstrBot ≥ 4.5 | — |
| Chrome / Chromium / Edge | 只为了 Lens。Windows/macOS 装个浏览器就有；Docker 里 `apt install chromium`；**已经装了 playwright 的话，它自带的 chromium 会被自动识别，不用另装** |
| 能访问谷歌的代理 | 填在 AstrBot 的 `http_proxy`（设置 → 其他）里最省事，插件会复用 |

## 三、安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/ttlvip/astrbot_plugin_fish_find_chicken.git
```

然后在 WebUI「插件」页重载插件。日志里应当看到：

```
[鱼鱼找鸡] 引擎：lens,saucenao,tracemoe；代理：http://***@127.0.0.1:7890（来自 AstrBot 全局 http_proxy）；超时：45s
[鱼鱼找鸡] Lens 可用，浏览器：C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
```

## 四、指令

| 指令 | 说明 |
|---|---|
| `/鱼鱼找鸡` | 反查：引用一张带图的消息，或图跟指令一起发 |
| `/鱼鱼找鸡 <图片网址>` | 直接给图片链接 |
| `/鱼鱼找鸡 帮助` | 用法 |
| `/找鸡自检` | 管理员：看浏览器、代理连通性、引擎、配额 |
| `/找鸡重置浏览器` | 管理员：清掉浏览器 profile（被谷歌标记、或换代理后建议清一次） |

别名：`/找鸡`、`/coser`、`/反查`。

回话长这样：

```
🎯 鱼鱼找鸡结果

🌐 平台：Twitter
👤 名字：阿薰kaOri
🆔 账号：@izayoill · https://x.com/izayoill

⏱ 引擎 谷歌 Lens / SauceNAO
```

查不到时会区分三种情况（这三种用户的下一步动作完全不同）：

* `❌ 这次没查成，一个引擎都没跑通（lens:谷歌连不上…）` → 环境问题，去看代理
* `❌ 没查到账号。…翻到 12 条相似图，但没有能确认的账号` → 引擎干活了，但对不上人
* `❌ 没查到账号。…都空` → 私图 / 新图 / 裁过的图

没查到账号时，回话下面会跟一份**候选图源**（5 条），按「最可能是原主」排序：
社交平台优先 → 相似度高的优先 → 其余按搜索引擎给的相关性顺序；同一个站点最多占 2 条，
避免 5 条全是 pixiv 这种没信息量的列表。每条都是可点开的链接，自己核对比任何统计都有用。

## 五、配置（WebUI 里改）

| 配置 | 默认 | 说明 |
|---|---|---|
| `engines` | `lens,saucenao,tracemoe` | 想关掉某个就删掉；`iqdb` 可加 |
| `serpapi_enabled` | 关 | 开 = 用 SerpApi 查 Lens，不启动浏览器、不会撞人机验证 |
| `serpapi_api_key` | 空 | SerpApi 的 key。**开关开着但 key 为空 = 自动回退本地浏览器** |
| `proxy` | 空 | 留空 = 用 AstrBot 全局 `http_proxy` |
| `use_astrbot_proxy` | 开 | 关掉就只用插件自己的 `proxy` 或环境变量 |
| `browser_exe` | 空 | 留空自动探测：PATH 里的 chrome/chromium/edge → **playwright 的 `ms-playwright` 缓存** → 常见安装路径。Docker 里一般不用填 |
| `browser_headless` | 开 | 谷歌一直不给结果时可以关掉试试（窗口移到屏幕外） |
| `browser_profile` | 空 | 留空 = `data/plugin_data/astrbot_plugin_fish_find_chicken/browser_profile`。**建议留着**：谷歌把「已通过验证」记在 cookie 里，固定 profile 能明显降低人机验证概率、冷启动也更快 |
| `browser_cdp` | 空 | 填 `http://chrome:9222` 就连独立跑的 Chrome，不本地启动（Docker 场景见第六节） |
| `browser_cdp_insecure` | 关 | CDP 地址是 `https://` 且自签证书时打开（内网反代常见）；`http://` 不需要 |
| `lens_max_resolve` | 10 | Lens 解析多少条结果；社交平台优先，每条约 1~3 秒 |
| `strict_consensus` | 开 | 名字对不上主轴的号一律不报（宁缺毋滥） |
| `timeout_sec` | 45 | 单引擎超时 |
| `max_accounts` | 3 | 最多报几个号 |
| `cooldown_sec` / `per_user_day` / `global_day` | 30 / 20 / 100 | 冷却与每日配额（管理员不受限） |
| `cache_days` | 30 | 同一张图再查直接给缓存，不扣次数 |
| `found_image` | 空 | 命中时附带的那张「已找到」图（路径或网址） |
| `group_whitelist` / `group_blacklist` | 空 | 按群开关 |

**调优建议**：Lens 慢主要慢在「解析链接」。群里只想要个大概就把 `lens_max_resolve` 调到 6；
想让「查全」就调到 14。冷启动那 1~2 秒只发生一次，浏览器是进程内单例，后续查询复用。

**实测耗时**（Windows + Edge 141 + 香港出口，真人 coser 图）：

| 阶段 | 耗时 |
|---|---|
| 浏览器冷启动 | 0.8 ~ 2.3 秒（只发生一次，后续查询复用） |
| 上传图片 → 结果页出卡片 | 5 ~ 8 秒 |
| 解析 10 条结果链接（HTTP 直读 302，失败退回标签页） | 干净 IP + 热 profile：6 秒内 10/10；冷启动或被限流时 8 ~ 14 秒、6 ~ 8 条 |
| 一次查询总计 | **9 ~ 27 秒** |

如果连续搜索把谷歌惹毛了（返回「正在验证您的请求」的节流页），插件会等 2 秒替它重试一次；
**撞到第 2 条节流页就停止解析剩余的，用手上已有的结果回话** —— 越敲越紧，硬刚只会升级成人机验证。

## 六、Docker 部署

Docker 里能不能用，只取决于三件事（和代码无关）：

| 条件 | 不满足的后果 | 怎么补 |
|---|---|---|
| 容器能拿到 Chrome | **「图片 → X 账号」直接不可用**（只剩 SauceNAO 这类兜底） | 装进 AstrBot 镜像，**或**另起一个 Chrome 容器（见下两节） |
| 容器能访问谷歌 | Lens 报「谷歌连不上」，同上 | 给容器 / 给那个 Chrome 配 `http_proxy`（别写容器自己的 `127.0.0.1`） |
| `/AstrBot/data` 可写 | 配额/缓存/浏览器 profile 全失败 | 别用 `:ro` 挂载 |

官方镜像 `soulter/astrbot:latest` = `python:3.12-slim`，**没有装任何浏览器**。
你有两条路：装进 AstrBot 容器（简单），或单独跑一个 Chrome 容器（干净、可复用）。

### 先自检，再排查

```bash
docker exec -it astrbot bash /AstrBot/data/plugins/astrbot_plugin_fish_find_chicken/tools/docker_check.sh
```

它会依次检查：插件文件与版本 → aiohttp → 浏览器（本地启动会真启动一次并让 CDP 读 UA；
配了外部 CDP 则直接探 `/json/version`）→ 代理与谷歌连通性 → data 目录可写，最后给 PASS/FAIL 汇总。

### 方案 A：把 chromium 装进 AstrBot 镜像

临时 `docker exec` 装的东西在容器重建后会丢，所以建议落成镜像：

```dockerfile
FROM soulter/astrbot:latest
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
```

compose 里把 `image:` 换成构建出来的镜像（约 +300MB）。插件会自动加 `--no-sandbox`
和 `--disable-dev-shm-usage`（root + 小 `/dev/shm` 是容器里的经典坑），不需要你额外配参数。

### 方案 B：单独跑一个 Chrome 容器，插件连它的 CDP

不想动 AstrBot 镜像（或者想让多个服务共用一个浏览器）时用这个。插件通过 DevTools 协议
连过去，**不启动也不关闭它**，只借用调试端口。

```yaml
services:
  chrome:
    image: zenika/alpine-chrome:latest
    container_name: chrome
    restart: unless-stopped
    command: >
      --headless --no-sandbox --disable-gpu --disable-dev-shm-usage
      --remote-debugging-address=0.0.0.0 --remote-debugging-port=9222
      --proxy-server=http://192.168.1.10:7890
    shm_size: 512m
    networks: [botnet]

  astrbot:
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: unless-stopped
    ports: ["6185:6185"]
    volumes: ["./data:/AstrBot/data"]
    environment: ["TZ=Asia/Shanghai"]
    networks: [botnet]

networks:
  botnet:
```

然后在插件配置里填：

```
browser_cdp = http://chrome:9222
```

四个必须注意的点：

1. **`--remote-debugging-address=0.0.0.0` 不能省**。Chrome 默认只监听自己的 `127.0.0.1`，
   只映射端口 9222 也连不进去 —— 这是最常见的坑。
2. **代理由这个 Chrome 自己带**（上例的 `--proxy-server`）。插件的 `proxy` 配置对浏览器无效，
   但对 SauceNAO / trace.moe / IQDB 那些 HTTP 请求仍然生效。
3. **`browser_headless` / `browser_profile` 在这种模式下无效**：头模式和 profile 都由那个
   Chrome 容器决定（想保持登录态就把它的 profile 目录挂出来）。
4. 两个容器必须在**同一个 docker 网络**里，`http://chrome:9222` 才能解析；跨机器就写 IP。

验证（在 astrbot 容器里）：

```bash
docker exec -it astrbot curl -sS --max-time 8 http://chrome:9222/json/version
# 返回一段 JSON、里面有 "webSocketDebuggerUrl" 就说明通了
```

两种方案怎么选：只有这一个插件用浏览器 → 方案 A 省事；已经有一个浏览器服务、或想让
AstrBot 镜像保持官方镜像（便于随时升级）→ 方案 B。

> **如果你已经跑着一个带网页界面的浏览器镜像**（Selkies、linuxserver/chrome、noVNC 之类），
> 注意那个网页地址（常见 `https://host:4001/`、`http://host:3000/`）**不是 CDP 端点**：
> 它是给人看画面的远程桌面，`/json/version` 会返回 404。插件要的是 DevTools 调试端口，
> 必须在浏览器启动参数里加
> `--remote-debugging-port=9222 --remote-debugging-address=0.0.0.0`，并把 9222 暴露出来
> （或让两个容器同网络）。判断标准只有一条：`curl http://host:9222/json/version` 能返回 JSON。

> 无论哪种方案，都**不要**在容器里装 playwright/selenium：体积更大，而且会和插件自带的
> CDP 驱动抢浏览器和 profile。

### 代理：容器里的 `127.0.0.1` 不是宿主机

| 拓扑 | 代理怎么写 | 注意 |
|---|---|---|
| `network_mode: host` | `http://127.0.0.1:7890` | 最简单；端口不再隔离 |
| 桥接 + 宿主机局域网 IP | `http://192.168.1.10:7890` | Clash 需开「允许局域网连接」(allow-lan)，否则只监听回环 |
| 桥接 + `host.docker.internal` | `http://host.docker.internal:7890` | Linux 要加 `extra_hosts: ["host.docker.internal:host-gateway"]` |

写完在**容器内**验证（验证的是容器的网络，不是宿主机的）：

```bash
docker exec -it astrbot curl -sS -o /dev/null -w "%{http_code}\n" --max-time 12 \
  -x http://192.168.1.10:7890 https://www.google.com/
# 200 / 302 才算通；000 或超时就是不通
```

不需要为浏览器调试端口配 `no_proxy`：插件对 CDP 的探测是显式直连的（`trust_env=False`）。
另外 Chrome 的 `--proxy-server` 不支持带账号密码的代理 URL，需要认证的代理要在代理端放开。

## 七、目录结构

```
astrbot_plugin_fish_find_chicken/
├── main.py                 # AstrBot 侧：收图、配额、回话（不含引擎逻辑）
├── core/
│   ├── __init__.py         # search_image()：一次完整反查的入口
│   ├── config.py           # 配置 → Settings（含代理优先级）
│   ├── net.py              # aiohttp 请求 / 代理 / 图片下载（SSRF 防护）
│   ├── browser.py          # 极简 CDP 客户端 + 浏览器单例（纯 aiohttp，不装 selenium）
│   ├── lens.py             # 谷歌 Lens：浏览器内上传 + 结果卡片抽取 + token 解析
│   ├── engines.py          # SauceNAO / IQDB / trace.moe + 并发编排
│   ├── accounts.py         # 候选 → 账号（平台规则 / 同源去重 / 名字共识）
│   ├── reply.py            # 回话排版
│   ├── state.py            # 冷却 / 每日配额 / 结果缓存（原子写）
│   └── textutil.py
├── tools/docker_check.sh   # 容器内一键自检（浏览器/代理/目录）
├── tests/                  # 离线单测（不联网、不需要 AstrBot）
├── _conf_schema.json
├── metadata.yaml
└── requirements.txt
```

状态文件在 `data/plugin_data/astrbot_plugin_fish_find_chicken/state.json`（不放在插件目录，避免更新时被覆盖）。

## 八、用 SerpApi 代替本地浏览器（可选，能彻底避开人机验证）

插件支持接 [SerpApi 的 Google Lens API](https://serpapi.com/google-lens-api)：由 SerpApi 用它自己的池子去查 Lens，
插件只发一个 HTTPS 请求就能拿到结果。**不需要浏览器、不会遇到验证码**，代价是按次计费。

### 怎么开

1. 去 serpapi.com 注册，Dashboard 里复制 API key
2. 插件配置：
   ```
   serpapi_enabled = true
   serpapi_api_key = 你的 key
   ```
3. 重载插件 → `/找鸡自检` 会显示 `Lens 通道：SerpApi（key …abcd，不启动本地浏览器）`

### 回退规则（严格按配置走，不会偷偷切换）

| 配置状态 | 实际用的通道 |
|---|---|
| `serpapi_enabled = true` 且 key 非空 | **SerpApi**（不启动任何浏览器） |
| `serpapi_enabled = true` 但 key 为空 | 本地浏览器，并在日志里警告「开了 SerpApi 但没填 key，已回退」 |
| `serpapi_enabled = false` | 本地浏览器 / 外部 CDP，和以前完全一样 |

SerpApi 调用**失败**时（key 无效、额度用尽、网络问题）不会自动降级到本地浏览器 —— 那样会掩盖
「key 配错了」这类问题。失败原因会直接写进回话的「没出结果」里，比如「额度用完了（429）」。

### 两个必须知道的点

1. **图片会被上传到 SerpApi**。这和本地模式把图传给谷歌是一回事，但毕竟是第三方，群里用的话建议先说明。
2. **SerpApi 限制图片 ≤ 500KB**（jpg/png/webp）。插件会自动压缩：按长边 1600→640、质量 88→68
   逐级降，够小就停（实测 2.4MB 的 PNG 压到 170KB、972×1600，Lens 照样能认）。压缩需要 Pillow，
   AstrBot 镜像默认已带。

### 顺带的好处

本地模式要逐条把 `/goto?url=<token>` 解析成真实地址（这一步既慢又容易触发限流）；SerpApi 直接返回
`visual_matches[].link` 就是来源页地址，所以**一次请求就拿到全部结果**，通常 2~5 秒，比本地模式快一截。

## 九、谷歌要求验证（CAPTCHA）怎么办

先明确一件事：**这是谷歌的多因子判定，不是插件故障，也没有可靠的「破解」办法**。
判定依据大致是「出口 IP 信誉 + 浏览器指纹 + 查询频率」三者叠加。能做的只有降低被判定为
机器人的概率，以及复用你已经通过验证的会话。

按有效性排序：

| 手段 | 有效性 | 怎么做 |
|---|---|---|
| **换出口 IP** | 最高 | 机房 IP / 公共 VPN 的 IP 池几乎必然被拦；换家宽或住宅代理后基本不再出现。换 IP 后原来的 cookie 也失效，要一起换 |
| **修正浏览器指纹** | 高（已内置） | 插件会自动做：覆盖 UA、抹掉 `navigator.webdriver`、补 `window.chrome` / `plugins` / `languages`、伪装 WebGL vendor。**Playwright 自带 Chromium 的默认 UA 里带 `HeadlessChrome`，这一步是必须的** |
| **复用已验证会话** | 高 | 配 `google_cookie`：在你自己浏览器里过完验证后，从开发者工具复制 google.com 的 Cookie 头整串粘进来。注意反滥用 cookie 与出口 IP 绑定，换 IP 就无效 |
| **降低频率** | 中 | `cooldown_sec` 调到 60、`per_user_day` 5、`global_day` 30。信誉是累积的，越刷越紧；撞到验证后插件会主动收手 |
| **固定 profile + 人工过验证一次** | 中（仅本地浏览器模式） | 用 `browser_headless: false` 让窗口出现在屏幕外，人工点一次验证，cookie 就留在 `browser_profile` 里；外部浏览器模式则由那个浏览器的 profile 决定 |

不建议做的事：接打码平台去解 reCAPTCHA。它对 Lens 这种「结果页本身也被判定」的场景基本没用
（解完验证码，结果页照样可能是空壳），而且不稳定、有合规风险。

判断是不是验证码：日志/回话里出现「谷歌要求人机验证（换出口 IP 或配置 google_cookie 可缓解）」。
如果只是偶发，等 10~30 分钟通常自己恢复；如果每次必现，那就是出口 IP 已被标记，换出口是唯一解。

**按顺序排查（本地浏览器模式）**：

1. 先确认浏览器是完整版而不是 headless_shell：`/找鸡自检` 里显示的路径应该形如
   `/ms-playwright/chromium-1091/chrome-linux/chrome`。看到 `chromium_headless_shell` 说明只有精简版，
   指纹更假、也更容易被拦 —— 跑一次 `playwright install chromium` 补上完整版。
2. 还验证 → `/找鸡重置浏览器`。旧 profile 里可能存着「被标记」的 cookie，换指纹也没用，
   清掉重新建立一个干净身份再试。
3. 还验证 → 就是出口 IP 的问题。换家宽/住宅代理；换完记得再重置一次 profile。
4. 仍不行 → 配 `google_cookie`（和当前出口同 IP 导出的），并把 `lens_max_resolve` 降到 6、冷却调到 60 秒。

> 想更进一步降低无头特征，可以让容器跑「有头 + Xvfb」：`apt install xvfb`，启动
> `Xvfb :99 -screen 0 1440x1400x24 &`，设 `DISPLAY=:99`，再把 `browser_headless` 关掉。
> 注意 `chromium_headless_shell` 天生无头，用不了这条路。收益不确定，属于最后一招。

## 十、已知限制

* **Lens 会被人机验证挡**（这是谷歌的判定，不是插件故障，处理办法见第八节）。插件会自动重试一次、
  然后收手，并把配额默认卡在「单人 20 次/天、全站 100 次/天」。自建机器人实测连续十几次查询就会触发。
* **解析条数受配额影响**。`lens_max_resolve` 是上限，被限流时可能只解析出几条 —— 结果里会如实少报，不会编。
* **只能找到「网上公开过」的图**。私聊原图、刚发的新图、被裁过/加过贴纸的图基本查不到，这是原理决定的。
* **账号是「最可能」而不是「确定」**。名字共识能挡掉大部分误报，但最终请点开链接核对。
* Pixiv 是画作平台，插件会把相似度 < 0.8 的 pixiv 结果直接丢掉 —— 画师的作品页不是 coser 的号。
* 搜索结果受出口 IP 地域影响（代理在香港/日本/美国，结果会略有差异）。

## 十一、致谢与许可

* 业务规则与文案来自原项目 **鱼鱼找鸡 v1.0.0**（Node 实现），本插件是它的纯 Python 重写：
  「账号抽取只看 URL 证据」「名字共识」「三种没查到的区分」等都是原项目在真机上踩出来的经验。
* 架构参考了 [`astrbot_plugin_coser_image_search_rimielabot`](https://github.com/RimieLab/astrbot_plugin_coser_image_search_rimielabot)
  的「用 CDP 驱动本机 Chrome/Edge」思路（本实现自带极简 CDP 客户端，仅依赖 aiohttp）。
* 许可证见 [LICENSE](LICENSE)。
