"""账号抽取：从引擎候选里认出「这个人是谁、号在哪」。

整套判断来自原项目（鱼鱼找鸡 v1.0.0）实测踩出来的经验，几条铁律不要随手改：

1. **只认 URL 证据**。没有链接的 handle 一律不写，宁可少报也不猜。
2. **收敛到主页，但不丢掉原帖**。命中的常是某条推文（/status/…），人问的是「账号」，
   所以 `url` 一律归一成主页；同时把「最接近原图的那条具体页面」记在 `matchUrl` 里
   —— 只有主页的话用户还得自己在时间线上翻，等于没给到原始内容。
3. **同一条原帖只算一个人**。weibo.com/n/昵称 与 weibo.com/u/数字uid 是同一个人。
4. **名字对不上主轴就不报**。同一角色常有好几个 coser 各拍一版，Lens 会把别人的原帖
   一起带回来；判断「谁是这张图的人」看某个名字是否在多条结果标题里反复出现。
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from .textutil import host_of, strip_tags, strip_tracking, trim_text

# ── 平台规则 ──────────────────────────────────────────────────────────────
# 命中的常常是某条推文（/status/…），但人问的是「账号」，所以一律收敛到主页地址。
PLATFORM_RULES = [
    {
        "platform": "twitter",
        "re": re.compile(
            r"^https?://(?:www\.|mobile\.|m\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{2,20})(?:/|$|\?)",
            re.I,
        ),
        "clean": lambda m: {
            "handle": "@" + m.group(1),
            "handleKey": m.group(1).lower(),
            "url": f"https://x.com/{m.group(1)}",
        },
    },
    {
        "platform": "weibo",
        # 三种形态：/u/<数字uid>、/n/<昵称>、/<昵称>；昵称在链接里常是 percent-encoding
        #（`weibo.com/n/%E9%98%BF%E8%96%B0`），所以要放行 %XX。
        "re": re.compile(
            r"^https?://(?:www\.|m\.)?weibo\.(?:com|cn)/(?:u/|n/)?"
            r"((?:\d{6,})|(?:[A-Za-z0-9_\-]|%[0-9A-Fa-f]{2}){3,60})(?:/|$|\?)",
            re.I,
        ),
        "clean": lambda m: {
            "handle": unquote(m.group(1)),
            "handleKey": unquote(m.group(1)).lower(),
        },
    },
    {
        "platform": "bilibili",
        "re": re.compile(
            r"^https?://(?:space\.bilibili\.com|www\.bilibili\.com/space)/(\d{3,})(?:/|$|\?)",
            re.I,
        ),
        "clean": lambda m: {"handle": m.group(1), "handleKey": m.group(1)},
    },
    {
        "platform": "instagram",
        "re": re.compile(
            r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:\?|$)",
            re.I,
        ),
        "clean": lambda m: {"handle": "@" + m.group(1), "handleKey": m.group(1).lower()},
    },
    {
        "platform": "pixiv",
        # 两种真实形态：新站 /users/<id>，老站 /member.php?id=<id>（SauceNAO 的 Member 行就是后者）
        "re": re.compile(
            r"^https?://(?:www\.)?pixiv\.net/(?:(?:en/)?users/(\d{2,})|member\.php\?(?:[^#]*&)?id=(\d{2,}))",
            re.I,
        ),
        "clean": lambda m: {
            "handle": m.group(1) or m.group(2),
            "handleKey": m.group(1) or m.group(2),
            "url": f"https://www.pixiv.net/users/{m.group(1) or m.group(2)}",
        },
    },
    {
        "platform": "xiaohongshu",
        # 小红书主页只有 /user/profile/<24位id> 这一种形态；explore/discovery 之类都是内容页
        "re": re.compile(
            r"^https?://(?:www\.)?xiaohongshu\.com/user/profile/([A-Za-z0-9]{16,32})",
            re.I,
        ),
        "clean": lambda m: {
            "handle": m.group(1),
            "handleKey": m.group(1).lower(),
            "url": f"https://www.xiaohongshu.com/user/profile/{m.group(1)}",
        },
    },
]

# bilibili 的图文/视频页里带作者 mid 时才算 direct
BILIBILI_AUTHOR_RE = re.compile(
    r"/(?:opus|video|read)/.*?(?:mid=|space\.bilibili\.com/)(\d{3,})", re.I
)

# 同分时的平台顺序：Twitter → 微博 → B站 → Instagram → 小红书 → Pixiv。
# 按「国内 coser 最可能留号的地方」排，回话里第 1 条就是最可能有用的那个。
PLATFORM_WEIGHT = {
    "twitter": 1,
    "weibo": 2,
    "bilibili": 3,
    "instagram": 4,
    "xiaohongshu": 5,
    "pixiv": 9,
}

# 社交平台的域名（含常见变体）。用来回答一个很常见的问题：
# 「翻到 59 条相似图，为什么一个账号都没有？」—— 因为那 59 条可能全是图站/转载站，
# 一条社交链接都没有。这个判断要能在回话里说清楚，否则用户会以为是插件坏了。
SOCIAL_HOSTS = frozenset(
    {
        "x.com",
        "twitter.com",
        "mobile.twitter.com",
        "instagram.com",
        "weibo.com",
        "weibo.cn",
        "m.weibo.cn",
        "bilibili.com",
        "space.bilibili.com",
        "t.bilibili.com",
        "xiaohongshu.com",
        "tiktok.com",
        "facebook.com",
        "threads.net",
        "threads.com",
    }
)


def is_social_host(host: str) -> bool:
    """这个域名是不是社交平台（含子域）。"""
    text = str(host or "").lower().lstrip(".")
    if not text:
        return False
    if text in SOCIAL_HOSTS:
        return True
    return any(text.endswith("." + base) for base in SOCIAL_HOSTS)


# 画作平台：pixiv 上的是画师的作品/会员号，不是 coser 的主页。
ARTWORK_PLATFORMS = {"pixiv"}

PLATFORM_LABELS = {
    "twitter": "Twitter",
    "weibo": "微博",
    "bilibili": "B站",
    "instagram": "Instagram",
    "xiaohongshu": "小红书",
    "pixiv": "Pixiv",
}

# 「看着像账号、其实是页面」的路径前缀 —— 百度识图/搜索结果里这类链接特别多，
# 不挡掉就会把 `weibo.com/ttarticle/p/show?id=…` 当成一个叫 ttarticle 的账号报出去。
PATH_DENY = {
    "weibo": [
        "ttarticle", "p", "show", "status", "detail", "photo", "video", "article",
        "fav", "like", "comment", "search", "hot", "topic", "tv", "page", "s",
        "weibo", "login", "signup", "reg",
    ],
    "twitter": [
        "i", "intent", "share", "home", "search", "hashtag", "status", "explore",
        "notifications", "messages", "settings", "login", "signup", "tos", "privacy",
        "account",
    ],
    "instagram": [
        "p", "reel", "reels", "explore", "accounts", "stories", "tv", "about",
        "legal", "directory",
    ],
    "bilibili": ["video", "read", "opus", "bangumi", "search", "account"],
    "pixiv": ["artworks", "i", "en", "tags", "search", "ranking", "novel", "ajax"],
}

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>）)]+")
_WEIBO_NICK = re.compile(r"weibo\.com/n/([^/?#]+)", re.I)


def match_platform(url) -> dict | None:
    """把一条链接认成「哪个平台的哪个号」；认不出返回 None。"""
    text = str(url if url is not None else "")
    for rule in PLATFORM_RULES:
        m = rule["re"].match(text)
        if not m:
            continue
        handle = str(m.group(1) or "")
        deny = PATH_DENY.get(rule["platform"], [])
        if handle.lower() in deny:
            continue
        if rule["platform"] == "weibo":
            # weibo.com/123456 这种纯数字才是 uid；weibo.com/abc 是昵称路径，也能用。
            # 注意这里判的是链接里那段原文（可能是 %XX），解出来的中文昵称本身当然不匹配 ASCII 类。
            if not handle.isdigit() and not re.fullmatch(r"[A-Za-z0-9_\-]{3,}", handle):
                if not re.fullmatch(r"(?:[A-Za-z0-9_\-]|%[0-9A-Fa-f]{2}){3,60}", handle):
                    continue
        if rule["platform"] == "instagram" and re.fullmatch(
            r"(p|reel|explore|accounts|stories)", handle, re.I
        ):
            continue
        if rule["platform"] == "twitter" and re.fullmatch(
            r"(i|intent|share|home|search)", handle, re.I
        ):
            continue
        cleaned = rule["clean"](m)
        # clean() 可以给出归一化后的 url（比如 pixiv 老站 member.php → /users/<id>）
        return {
            "platform": rule["platform"],
            **cleaned,
            "url": cleaned.get("url") or text,
        }
    return None


# ── 名字识别 ──────────────────────────────────────────────────────────────
def looks_like_person_name(value) -> bool:
    """整条标题当人名的兜底太脏（会得到「阿抽崽on X: "hi" / X」这种），先过滤掉不像名字的。"""
    text = str(value if value is not None else "").strip()
    if len(text) < 2 or len(text) > 24:
        return False
    if re.search(r"[\"':：/|]", text):
        return False
    if re.search(
        r"\b(on|via|by)\s+(x|twitter|instagram|ig|weibo|bilibili|tiktok|facebook|threads)\b",
        text,
        re.I,
    ):
        return False
    return True


_PERSON_NAME_PUNCT = re.compile(
    r"[\s\-_·・.,，。:：;；!！?？'\"“”‘’()（）\[\]【】{}<>《》|/\\~—–]+"
)


def normalize_person_name(value) -> str:
    """人名归一：小写、去空格与常见标点，用来做「这俩是不是同一串字」的粗比较。"""
    return _PERSON_NAME_PUNCT.sub("", str(value if value is not None else "").lower())


def author_name_from_title(title) -> str:
    text = trim_text(strip_tags(str(title if title is not None else "")), 120)
    if not text:
        return ""
    cleaned = re.sub(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    candidate = cleaned or text
    return candidate if 2 <= len(candidate) <= 24 else ""


_MENTION_PATTERNS = [
    # 注意不要写 \bCN：`FacebookCN : 阿薰kaOri` 里 k 和 C 之间没有词边界
    re.compile(r"CN\s*[:：]\s*([\w@.]{2,24})", re.I | re.U),
    re.compile(r"(?:coser|cosplayer|cos)\s*[:：\-_]\s*([\w@.]{2,24})", re.I | re.U),
    re.compile(r"(?:博主|coser|cosplayer)(?![吧网站群])([\u4e00-\u9fff][\w@.]{1,23})", re.I | re.U),
]


def name_from_title_mention(title) -> str:
    """从标题里挖 coser 名字（`CN : 阿薰kaOri`、`动漫博主阿薰kaOri NO.049`）。"""
    text = str(title if title is not None else "")
    if not text:
        return ""
    for pattern in _MENTION_PATTERNS:
        hit = pattern.search(text)
        if not hit:
            continue
        # 中文里常见「Coser吧…」「博主网…」，把开头的语气词/站点词削掉再当名字
        name = re.sub(r"^(?:吧|网|站|群|们|的)+", "", str(hit.group(1) or "")).strip()
        if name and looks_like_person_name(name):
            return name
    return ""


_SOCIAL_PREFIX = (
    r"X|Twitter|推特|Instagram|IG|Ins|TikTok|抖音|微博|Weibo|Bilibili|哔哩哔哩|Facebook|Threads"
)
_SOCIAL_ON = r"X|Twitter|Instagram|TikTok|Facebook|Threads|微博|Bilibili"
_SOCIAL_NAME_RE = re.compile(
    rf"^(?:{_SOCIAL_PREFIX})\s*(.{{1,32}}?)\s*on\s*(?:{_SOCIAL_ON})\b", re.I
)

# Lens 的卡片标题会被拼成「XReplies by 某个名字(@handle) / X」「XPosts by …」这种形态，
# 前面那截是谷歌的界面词，不是人名，得削掉，否则拿它去做名字共识会误判主轴。
_UI_NOISE_PREFIX = re.compile(
    r"^(?:Replies?|Posts?|Reposts?|Quotes?|Threads?)\s+(?:by|from)\s+", re.I
)


def _clean_display_name(name: str) -> str:
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    text = _UI_NOISE_PREFIX.sub("", text)
    return re.sub(r"[:：\"'“”]+$", "", text).strip()


def social_name_from_title(title) -> str:
    """`X吴二棒on X: "…"` 里那个「on <站点>」前面的名字。"""
    text = re.sub(r"\s+", " ", str(title if title is not None else "")).strip()
    if not text:
        return ""
    m = _SOCIAL_NAME_RE.match(text)
    if not m:
        return ""
    return _clean_display_name(m.group(1))[:40]


_PROFILE_NAME_RE = re.compile(
    rf"^(?:{_SOCIAL_PREFIX})\s*(.{{1,40}}?)\s*[（(]\s*@?([A-Za-z0-9_.\-]{{2,30}})\s*[)）]", re.I
)


def profile_title_name(title) -> dict:
    """主页卡片标题：`X阿薰kaOri防盗版(@izayoill) / X` → 名字 + handle。"""
    text = re.sub(r"\s+", " ", str(title if title is not None else "")).strip()
    if not text:
        return {"name": "", "handle": ""}
    m = _PROFILE_NAME_RE.match(text)
    if not m:
        return {"name": "", "handle": ""}
    return {"name": _clean_display_name(m.group(1)), "handle": m.group(2)}


def dominant_person_name(candidates, min_hits: int = 1, min_prefix: int = 4) -> dict:
    """找出「这一批结果主要在讲谁」。

    把候选里出现过的名字逐个拿去比对所有结果的标题，某个名字或它的前几个字命中最多条
    结果，就以它为主轴。只命中自己那一条的名字（hits 减掉自身后为 0）不算证据。
    """
    items = list(candidates or [])
    titles = [
        normalize_person_name(f"{c.get('title') or ''} {c.get('snippet') or ''}")
        for c in items
    ]
    titles = [t for t in titles if len(t) >= min_prefix]
    if not titles:
        return {"name": "", "prefix": "", "hits": 0}

    names: dict[str, str] = {}

    def add(raw):
        key = normalize_person_name(raw)
        if len(key) < min_prefix or key in names:
            return
        names[key] = str(raw if raw is not None else "").strip()

    for candidate in items:
        title = str(candidate.get("title") or "")
        add(candidate.get("authorName"))
        add(profile_title_name(title)["name"])
        add(social_name_from_title(title))
        # 标题里写的名字（`CN : 阿薰kaOri`）也是证据：同一批结果里它出现在好几条标题上，
        # 就说明这张图讲的是这个人 —— 哪怕账号卡片是别人发的。
        add(name_from_title_mention(title))

    best = {"name": "", "prefix": "", "hits": 0}
    # 短名先占位：同票时留「阿薰kaori」这种干净写法，而不是「阿薰kaOriCOSPlayerVN」。
    for key in sorted(names, key=len):
        # 从长到短试前缀：「阿薰kaori防盗版」整串只命中 1 条，砍到「阿薰kaori」就命中好几条
        for length in range(min(len(key), 12), min_prefix - 1, -1):
            head = key[:length]
            hits = sum(1 for text in titles if head in text) - 1
            if hits < min_hits:
                continue
            if hits > best["hits"]:
                best = {"name": names.get(key, key), "prefix": head, "hits": hits}
            break
    return best


# ── 账号抽取 ──────────────────────────────────────────────────────────────
def _canonical_score(account: dict) -> int:
    url = str(account.get("url") or "")
    if re.search(r"/u/\d{3,}", url):
        return 3
    if re.search(r"space\.bilibili\.com/\d+", url):
        return 3
    if re.search(r"members?\.php\?|pixiv\.net/users/\d+", url):
        return 3
    if re.search(r"/(?:n|profile)/", url):
        return 1  # weibo.com/n/<昵称>：会 302 到 uid
    return 2


def _rank(account: dict) -> int:
    confidence = account.get("confidence")
    if confidence == "direct":
        return 0
    if confidence == "weak":
        return 1
    return 2


def extract_accounts(candidates, max_accounts: int = 3, strict_names: bool = True,
                     on_drop=None, consensus_candidates=None) -> list[dict]:
    """从候选结果里抽社媒账号。只认 URL 证据 —— 没有证据的 handle 一律不写。

    `consensus_candidates` 用来指定「拿哪批候选算名字主轴」。默认用全部候选，
    但混了 SauceNAO / IQDB 的动漫结果时不能这样：它们的标题讲的是画作角色，
    拿去算共识会把主轴带偏，导致真正的人名对不上、所有账号被丢光（实测踩过）。
    所以插件层会把它锚定到「知道这个人是谁」的那批引擎（Lens）上。
    """
    out: list[dict] = []
    seen: set[str] = set()
    items = list(candidates or [])

    for candidate in items:
        author_hit = match_platform(candidate.get("authorUrl")) if candidate.get("authorUrl") else None
        author_key = f"{author_hit['platform']}|{author_hit['handleKey']}" if author_hit else ""

        urls: list[str] = []
        for value in (
            candidate.get("authorUrl"),
            candidate.get("sourceUrl"),
            candidate.get("url"),
            *(candidate.get("links") or []),
        ):
            if value:
                urls.append(str(value))
        # 标题/摘要里偶尔也带完整链接
        for text in (candidate.get("title"), candidate.get("snippet")):
            urls.extend(_URL_IN_TEXT.findall(str(text if text is not None else "")))

        for url in urls:
            hit = match_platform(url)
            if not hit:
                continue
            dedupe = f"{hit['platform']}|{hit['handleKey']}"
            profile_name = profile_title_name(candidate.get("title"))
            fallback_name = author_name_from_title(candidate.get("title"))
            own_name = (
                profile_name["name"]
                or social_name_from_title(candidate.get("title"))
                or str(candidate.get("authorName") or "")
                or (
                    ""
                    if dedupe == author_key or not looks_like_person_name(fallback_name)
                    else fallback_name
                )
            )
            if dedupe in seen:
                # 同一个号被多个引擎撞见时，谁先到并不代表谁的标题更完整
                #（扫描日期在前、作者在后很常见），所以后面再遇到带名字的就补上。
                flat = f"{hit['platform']}|" + str(hit.get("handle") or "").lstrip("@").lower()
                for existing in out:
                    key = f"{existing.get('platform')}|" + str(existing.get("handle") or "").lstrip("@").lower()
                    if key == flat and not existing.get("authorName") and own_name:
                        existing["authorName"] = own_name
                continue
            seen.add(dedupe)
            out.append(
                {
                    "platform": hit["platform"],
                    "handle": hit["handle"],
                    "url": hit["url"],
                    # 作者行带出来的名字（`Member: ... <a>名字</a>`）比作品标题更像「这个人叫什么」
                    "authorName": own_name,
                    "confidence": "weak" if candidate.get("weak") else "direct",
                    "evidence": {
                        "engine": candidate.get("engine") or "",
                        "similarity": candidate.get("similarity"),
                        "sourceUrl": candidate.get("sourceUrl") or "",
                    },
                }
            )

        # bilibili 图文页：从 sourceUrl 里挖 mid
        bili = BILIBILI_AUTHOR_RE.search(str(candidate.get("sourceUrl") or ""))
        if bili:
            dedupe = f"bilibili|{bili.group(1)}"
            if dedupe not in seen:
                seen.add(dedupe)
                out.append(
                    {
                        "platform": "bilibili",
                        "handle": bili.group(1),
                        "url": f"https://space.bilibili.com/{bili.group(1)}",
                        # JS 版这条记录里没有 authorName 字段，保持一致（别补空串，
                        # 否则 --json 的结构和 Node 后端对不上）
                        "confidence": "weak" if candidate.get("weak") else "direct",
                        "evidence": {
                            "engine": candidate.get("engine") or "",
                            "similarity": candidate.get("similarity"),
                            "sourceUrl": candidate.get("sourceUrl") or "",
                        },
                    }
                )

    # 同一张图/同一条原帖只能对应一个作者：SauceNAO 常同时给出
    # `weibo.com/n/昵称` 与 `weibo.com/u/数字uid` —— 那是一个人，不能当成两个账号报出去。
    by_source: dict[str, dict] = {}
    for account in out:
        group = f"{account['platform']}|{account['evidence'].get('sourceUrl') or account.get('url') or ''}"
        prev = by_source.get(group)
        if prev is None:
            by_source[group] = account
            continue
        if _rank(account) < _rank(prev):
            by_source[group] = account
            continue
        if _rank(account) == _rank(prev) and _canonical_score(account) > _canonical_score(prev):
            by_source[group] = account
    deduped = list(by_source.values())

    # 同一角色常有好几个 coser 各拍一版，判断「谁是这张图的人」看出现次数。
    consensus_pool = (
        list(consensus_candidates) if consensus_candidates is not None else items
    )
    consensus = dominant_person_name(consensus_pool)
    artwork_only = bool(deduped) and all(a["platform"] in ARTWORK_PLATFORMS for a in deduped)

    def name_of(account):
        return normalize_person_name(account.get("authorName"))

    named_match = []
    if consensus["prefix"] and not artwork_only:
        named_match = [
            a
            for a in deduped
            if name_of(a)
            and (name_of(a).startswith(consensus["prefix"]) or consensus["prefix"].startswith(name_of(a)))
        ]

    # 只要认出了主轴，就只报「名字对得上」的号：对不上的、没名字的一律不报。
    strict = bool(strict_names) and bool(consensus["prefix"]) and not artwork_only
    kept = named_match if strict else deduped

    # 兜底：严格过滤后一个都不剩，说明「主轴」大概率判错了（同一个人的不同写法、
    # 或者把角色名当成了人名）。这时候空手而归是最差的结果 —— 把候选都列出来、
    # 标成「未核实」，让用户自己点开核对，比什么都不报有用。
    if strict and not kept and deduped:
        for account in deduped:
            account["confidence"] = "weak"
            account["fallback"] = True
        kept = deduped

    if strict and on_drop is not None:
        for account in deduped:
            if account in named_match:
                continue
            try:
                on_drop(account, consensus)
            except Exception:  # 统计用的回调，出错也不能影响结果
                pass

    # 排序：direct → weak（相似图）→ inferred；同级再按相似度降序、平台顺序
    def sort_key(account):
        sim = account["evidence"].get("similarity")
        sim = -1.0 if sim is None else float(sim)
        return (_rank(account), -sim, PLATFORM_WEIGHT.get(account["platform"], 9))

    kept = sorted(kept, key=sort_key)
    kept = kept[: max(1, int(max_accounts or 3))]
    # 最后再挂「最相关的那条具体链接」：抽账号时一律收敛到主页，但用户真正想点开
    # 的是这条图对应的推文/作品页，缺了它回话里就只剩一个主页地址，等于没给原始内容。
    attach_match_links(kept, items, consensus_name=str(consensus.get("name") or ""))
    return kept


# ── 「最相关的那条具体链接」 ───────────────────────────────────────────────
# 一条链接如果既不是主页、也不是主页上的功能页（/media、/followers…），就认为它指向
# 某个具体内容页（推文 / 微博正文 / pixiv 作品 / 小红书笔记）。
_TAB_PATH_RE = re.compile(
    r"/(?:media|followers|following|likes|liked|with_replies|replies|tagged|search|about)$",
    re.I,
)


def _url_key(url) -> str:
    """比较两条链接是不是同一个页面：去掉 query / fragment / 尾部斜杠。"""
    text = re.sub(r"[?#].*$", "", str(url if url is not None else "").strip())
    return text.rstrip("/").lower()


def is_specific_page(url, profile_url) -> bool:
    """这条链接是不是「比主页更具体」的内容页。

    只用来判断「值不值得单独报一条」，所以宁可保守：主页本身、主页上的 tab 页
    （x.com/<id>/media 这种）不算；其余同一个号名下的链接都算。
    """
    key = _url_key(url)
    if not key:
        return False
    if profile_url and key == _url_key(profile_url):
        return False
    return not _TAB_PATH_RE.search(key)


def _linked_handles(url) -> set[tuple[str, str]]:
    """一条链接可能属于哪个号：普通形态走 match_platform，B站图文页再从 mid 里挖。"""
    out: set[tuple[str, str]] = set()
    hit = match_platform(url)
    if hit:
        out.add((hit["platform"], str(hit.get("handleKey") or "").lower()))
    bili = BILIBILI_AUTHOR_RE.search(str(url if url is not None else ""))
    if bili:
        out.add(("bilibili", bili.group(1)))
    return out


def _link_score(entry: dict, url: str, consensus: str, evidence_url: str) -> float:
    """给候选链接打分：越像「这张图的出处」分越高。

    依次看：是不是这条账号的直接证据 → 是不是原帖（非「别人的回帖」）
    → 引擎给的相似度 → 标题里有没有出现主轴名字 → 结果页里的先后顺序。
    """
    score = 0.0
    if evidence_url and url == evidence_url:
        score += 3.0
    score += -2.0 if entry["weak"] else 2.0
    similarity = entry["similarity"]
    if isinstance(similarity, (int, float)) and not isinstance(similarity, bool):
        score += float(similarity) * 2.0
    if consensus and consensus in normalize_person_name(f"{entry['title']} {url}"):
        score += 2.0
    if entry["title"]:
        score += 0.5
    # 同分时保留引擎给出的相关性顺序（Lens 的卡片本身就是按相关性排的）
    return score - float(entry["index"]) * 0.05


def attach_match_links(accounts, candidates, consensus_name: str = "") -> None:
    """给每条账号挂上「与图片内容最相关的那条具体链接」（原地修改）。

    回话里的两个链接字段，用途是分开的：

    * `url` / `profileUrl` —— 主页，看这个人的全部作品、判断是不是同一个人；
    * `matchUrl` —— 内容最接近原图的那条具体页面（推文 / 微博正文 / 作品页 / 笔记），
      用户点开就能直接核对「这张图是不是他发的」。

    找不到比主页更具体的页面时，`matchUrl` 留空（不要拿主页凑数，那等于把主页报两遍）。
    另外给出 `matchUrls`（最多 3 条，按相关性排序）备查。
    """
    consensus = normalize_person_name(consensus_name)

    pool: list[dict] = []
    for index, candidate in enumerate(list(candidates or [])):
        urls: list[str] = []
        for value in (
            candidate.get("authorUrl"),
            candidate.get("sourceUrl"),
            candidate.get("url"),
            *(candidate.get("links") or []),
        ):
            if value:
                urls.append(str(value))
        # 标题/摘要里的完整链接也算证据（SauceNAO 的作者行有时只出现在文字里）
        for text in (candidate.get("title"), candidate.get("snippet")):
            urls.extend(_URL_IN_TEXT.findall(str(text if text is not None else "")))

        deduped: list[str] = []
        for url in urls:
            if url.startswith("http") and url not in deduped:
                deduped.append(url)
        if not deduped:
            continue
        handles: set[tuple[str, str]] = set()
        for url in deduped:
            handles |= _linked_handles(url)
        pool.append(
            {
                "urls": deduped,
                "handles": handles,
                "title": trim_text(candidate.get("title") or "", 60),
                "similarity": candidate.get("similarity"),
                "weak": bool(candidate.get("weak")),
                "index": index,
            }
        )

    for account in accounts or []:
        profile = str(account.get("url") or "").strip()
        own = {
            (
                str(account.get("platform") or ""),
                str(account.get("handle") or "").lstrip("@").lower(),
            )
        }
        evidence_url = str((account.get("evidence") or {}).get("sourceUrl") or "")

        best_by_key: dict[str, tuple[float, str, dict]] = {}
        for entry in pool:
            # 这张卡片是不是在讲这个号：要么卡里带着同一个 handle，要么它就是这条账号的证据
            if not (entry["handles"] & own or (evidence_url and evidence_url in entry["urls"])):
                continue
            for url in entry["urls"]:
                if not is_specific_page(url, profile):
                    continue
                linked = _linked_handles(url)
                if linked and not (linked & own):
                    continue  # 同一张卡片里混进来的别人的号，别当成本人的原帖
                if not linked and host_of(url) != host_of(profile):
                    continue  # 既没写号、又不是同一个站，认不出来
                key = _url_key(url)
                score = _link_score(entry, url, consensus, evidence_url)
                if key in best_by_key and best_by_key[key][0] >= score:
                    continue
                best_by_key[key] = (score, url, entry)

        ranked = sorted(best_by_key.values(), key=lambda row: -row[0])
        account["profileUrl"] = profile
        if not ranked:
            account["matchUrl"] = ""
            account["matchTitle"] = ""
            account["matchHost"] = ""
            account["matchUrls"] = []
            continue
        _score, best_url, best_entry = ranked[0]
        account["matchUrl"] = strip_tracking(best_url)
        account["matchTitle"] = best_entry["title"]
        account["matchHost"] = host_of(best_url)
        account["matchUrls"] = [
            {"url": strip_tracking(url), "title": entry["title"], "host": host_of(url)}
            for _s, url, entry in ranked[:3]
        ]



def account_name_of(account: dict, candidates=None) -> str:
    """名字：优先 weibo 昵称形态的 handle，其次图源候选标题里的人名，最后退回 handle。"""
    url = str(account.get("url") or "")
    nick = _WEIBO_NICK.search(url)
    if nick:
        try:
            return unquote(nick.group(1))
        except Exception:
            return nick.group(1)
    handle = str(account.get("handle") or "").lstrip("@")
    own = str(account.get("authorName") or "").strip()
    if own:
        return own
    source = (account.get("evidence") or {}).get("sourceUrl") or ""
    items = list(candidates or [])
    same = None
    if source:
        same = next((c for c in items if c.get("sourceUrl") == source), None)
    if same is None:
        same = next((c for c in items if c.get("sourceUrl") and c.get("title")), None)
    from_title = ""
    if same:
        from_title = profile_title_name(same.get("title"))["name"] or author_name_from_title(
            same.get("title")
        )
    if from_title:
        return from_title
    if handle and not handle.isdigit():
        return handle
    # 纯数字 id（pixiv 会员号 / 微博 uid）看不出是谁，别硬编名字
    return "未知"


def display_handle(account: dict) -> str:
    """昵称类加 @，纯数字 id（微博 uid / B站 mid / pixiv 会员号）只给数字。"""
    handle = str(account.get("handle") or "").lstrip("@").strip()
    if not handle:
        return "未知"
    return handle if handle.isdigit() else f"@{handle}"


def platform_label(platform: str) -> str:
    name = PLATFORM_LABELS.get(platform, platform)
    return f"{name}（画作平台）" if platform in ARTWORK_PLATFORMS else name


def drop_weak_artwork(accounts, min_similarity: float = 0.8) -> tuple[list[dict], int]:
    """画作平台的「相似画作」不等于图源。

    SauceNAO 在真人照片上常常返回一堆「画风像 / 同角色」的 pixiv 作品，相似度只有 0.6~0.7，
    报出去就是拿画师的作品页冒充 coser 的号。所以 pixiv 类账号加一道相似度门槛，
    不够就直接丢，由调用方把「丢了几张」告诉用户。
    """
    limit = float(min_similarity or 0.0)
    kept: list[dict] = []
    dropped = 0
    for account in accounts or []:
        if account.get("platform") not in ARTWORK_PLATFORMS:
            kept.append(account)
            continue
        raw = (account.get("evidence") or {}).get("similarity")
        try:
            similarity = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            similarity = 0.0
        if similarity >= limit:
            kept.append(account)
        else:
            dropped += 1
    return kept, dropped
