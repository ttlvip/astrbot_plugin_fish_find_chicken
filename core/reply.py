"""回话排版：把抽出来的账号变成一条能直接发群里的消息。

版式是固定的，改文案前请同步 README：

    🎯 鱼鱼找鸡结果
    🌐 平台 / 👤 名字 / 🆔 账号
    🔗 主页     —— 这个人的账号页（看全部作品、确认是不是同一个人）
    🎯 最相关   —— 内容最接近这张图的那条具体页面（推文 / 作品页 / 笔记）

「没查到」的三种原因要分开说，否则用户分不清是引擎没工作、还是翻到了但对不上人。
"""

from __future__ import annotations

from urllib.parse import unquote

from .accounts import (
    ARTWORK_PLATFORMS,
    account_name_of,
    is_social_host,
    is_specific_page,
    display_handle,
    normalize_person_name,
    platform_label,
)
from .engines import engine_labels
from .textutil import host_of, trim_text

# 一条回话最多这么长：三个号的版式约 500 字，留足余量给链接和备注。
# 超长时按行截断（见 `_fit`）—— 从链接中间切会让链接彻底失效。
MAX_REPLY_CHARS = 1500

# 两个链接字段的用途只解释一次（每个号都写一遍太吵）
LINK_FOOTER = (
    "· 主页 = 这个人的账号页；最相关 = 内容最接近这张图的具体页面"
    "（那条推文 / 作品页 / 笔记），要核图先点这条"
)
NO_MATCH_HINT = "· 这次只翻到主页，没找到更具体的那条原帖（推文/作品页），点主页自己翻一下"


def _fit(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    """超长时按行截断，别把链接切一半。"""
    if len(text) <= limit:
        return text
    cut = text[: limit - 12]
    newline = cut.rfind("\n")
    if newline >= limit // 2:
        cut = cut[:newline]
    return cut + "\n…（后面省略）"


def _match_link(account: dict) -> tuple[str, str]:
    """取「最相关链接」和它的标题。

    优先用账号上算好的 `matchUrl`；手工构造/旧数据没有这个字段时，退回
    `evidence.sourceUrl` —— 但只有它确实指向某个具体页面（不是主页）才算数，
    否则等于把主页重复报一遍。
    """
    url = str(account.get("matchUrl") or "").strip()
    if url:
        return url, str(account.get("matchTitle") or "").strip()
    if "matchUrl" in account:
        return "", ""
    profile = str(account.get("url") or "").strip()
    evidence = str((account.get("evidence") or {}).get("sourceUrl") or "").strip()
    if evidence.startswith("http") and is_specific_page(evidence, profile):
        return evidence, ""
    return "", ""


def _has_social_source(candidates) -> bool:
    """候选里有没有社交平台链接（决定「抽不出账号」是正常结论还是被筛掉了）。"""
    return any(
        is_social_host(host_of(item.get("sourceUrl") or "")) for item in candidates or []
    )


def _ranked_sources(candidates, limit: int = 5, per_host: int = 2) -> list[dict]:
    """没查到账号时给用户看的「候选图源」，按「最可能是原主」的顺序排。

    排序依据（依次）：
    1. **社交平台优先** —— 用户要的是「这个人」，x.com/微博 这类链接点开就能确认；
    2. **相似度高的优先** —— SauceNAO 这类引擎给了 similarity，就按它降序；
    3. 其余保持引擎给出的原始顺序（Lens/SerpApi 的 visual_matches 本身就是谷歌相关性排序）。

    为什么要列表而不是统计：统计「pixiv×40」对用户没用，他能点开一条链接自己看。
    """
    rows = []
    for index, item in enumerate(candidates or []):
        url = str(item.get("sourceUrl") or "").strip()
        if not url:
            continue
        host = host_of(url)
        similarity = item.get("similarity")
        sim = float(similarity) if isinstance(similarity, (int, float)) else None
        rows.append(
            {
                "url": url,
                "host": host,
                "title": trim_text(item.get("title") or "", 42),
                "social": is_social_host(host),
                "similarity": sim,
                "index": index,
            }
        )
    rows.sort(
        key=lambda r: (
            0 if r["social"] else 1,
            # 无分数按「未知」处理，不要排到所有有分数结果后面（否则弱 SauceNAO 压过 Lens）。
            -(r["similarity"] if r["similarity"] is not None else 0.0),
            r["index"],
        )
    )

    # 同一个站点最多占 2 条：否则 5 条全是 pixiv，等于什么信息都没给。
    # 名额没用满时再用被挡下的补回来（保持原顺序），不能因为配额而少给。
    picked: list[dict] = []
    overflow: list[dict] = []
    used: dict[str, int] = {}
    for row in rows:
        host = row["host"]
        if used.get(host, 0) >= per_host:
            overflow.append(row)
            continue
        used[host] = used.get(host, 0) + 1
        picked.append(row)
        if len(picked) >= limit:
            break

    if len(picked) < limit:
        # 名额没用满时补回来，但优先补「当前出现次数最少」的站点，别让某个站独占。
        overflow.sort(key=lambda r: used.get(r["host"], 0))
        for row in overflow:
            picked.append(row)
            used[row["host"]] = used.get(row["host"], 0) + 1
            if len(picked) >= limit:
                break
    return picked


def _format_sources(rows: list[dict]) -> list[str]:
    """把候选图源排成「序号. 站点｜标题 — 链接」的一行一条。

    URL 保持完整：从中间截断会让推文 id / 小红书 token 失效，QQ 里点不开。
    总长度吃紧时由上层 ``_fit`` 删掉后面的条目，不要改 URL 字符串。
    """
    lines = []
    for position, row in enumerate(rows, start=1):
        site = row["host"] or "未知站点"
        title = row["title"]
        url = row["url"]
        head = f"{position}. {site}"
        if row["similarity"] is not None:
            head += f"（相似度 {row['similarity'] * 100:.0f}%）"
        if title:
            head += f"｜{title}"
        # 链接单独占一行，避免和标题挤在一起被 _fit 从中间切断。
        lines.append(f"{head}\n{url}")
    return lines

HEAD = "🎯 **鱼鱼找鸡结果**"


def format_reply(
    accounts=None,
    candidates=None,
    engines_used=None,
    failures=None,
    cached: bool = False,
    artwork_dropped: int = 0,
    name_dropped: int = 0,
    consensus_name: str = "",
    consensus_hits: int = 0,
    dropped_handles=None,
    note: str = "",
) -> str:
    accounts = list(accounts or [])
    candidates = list(candidates or [])
    engines_used = list(engines_used or [])
    failures = list(failures or [])

    # 认出主轴时，优先给「写着这个人名字」的原帖：那才是你要顺着找的那条。
    pool = [c for c in candidates if c.get("sourceUrl")]
    consensus_key = normalize_person_name(consensus_name)

    def decode(value):
        try:
            return unquote(str(value if value is not None else ""))
        except Exception:
            return str(value if value is not None else "")

    top = None
    if consensus_key:
        top = next(
            (
                c
                for c in pool
                if consensus_key
                in normalize_person_name(
                    f"{c.get('title') or ''} {decode(c.get('sourceUrl'))}"
                )
            ),
            None,
        )
    if top is None and pool:
        top = pool[0]

    if accounts:
        blocks = []
        has_match = False
        for account in accounts:
            url = str(account.get("url") or "").strip()
            guess = ""
            if account.get("confidence") == "inferred":
                guess = "（推断，未核实）"
            elif account.get("confidence") == "weak":
                guess = "（相似图，未核实）"
            handle = display_handle(account)
            # 推特昵称经常带一长串后缀（`月城りあ🌙🍼10/11(日)コスROM▹▸C-14`），抄全了没法看
            name = trim_text(account_name_of(account, candidates), 32)
            block = [
                f"🌐 平台：{platform_label(account.get('platform'))}",
                f"👤 名字：{name}",
                f"🆔 账号：{handle}{guess}",
            ]
            # 主页和「最相关」分两行写：一个用来认人，一个用来核图，混在一行会分不清哪个是哪个
            if url:
                block.append(f"🔗 主页：{url}")
            match_url, match_title = _match_link(account)
            if match_url:
                has_match = True
                line = f"🎯 最相关：{match_url}"
                if match_title:
                    line += f"｜{trim_text(match_title, 28)}"
                block.append(line)
            blocks.append("\n".join(block))

        text = "\n".join([HEAD + ("（缓存）" if cached else ""), "", "\n\n".join(blocks)])
        text += "\n\n" + (LINK_FOOTER if has_match else NO_MATCH_HINT)
        # 只有画作平台的结果时，得说清楚这不是 coser 本人的号
        if note:
            text += f"\n\n· {note}"
        if all(a.get("platform") in ARTWORK_PLATFORMS for a in accounts):
            text += (
                "\n\n· Pixiv 是画作平台（画师的作品页），不是 coser 本人的账号；"
                "真人号一般在推特"
            )
        return _fit(text)

    tried = " / ".join(engine_labels(engines_used)) if engines_used else ""
    # 「空结果」不是故障：引擎正常工作、只是没匹配到。把它写进「没出结果」里
    # 会让用户以为引擎坏了（真实反馈：「为什么提示空结果」）。
    real_failures = [f for f in failures if str(f.get("reason") or "").strip() not in ("空结果", "")]
    why = (
        "（"
        + "；".join(f"{f.get('engine')}:{trim_text(f.get('reason'), 30)}" for f in real_failures)
        + "）"
        if real_failures
        else ""
    )
    # 三种「没查到」要分开说：引擎没跑通 / 跑通了但对不上人 / 跑通了但一片空白
    cache_tag = "（缓存）" if cached else ""
    if not engines_used:
        head = f"❌ 这次没查成，一个引擎都没跑通{(' ' + why) if why else ''}"
    elif len(candidates) > 0:
        head = (
            f"❌ 没查到账号。{tried}{cache_tag} 翻到 {len(candidates)} 条相似图，"
            f"但没有能确认的账号{(' ' + why) if why else ''}"
        )
    else:
        head = (
            f"❌ 没查到账号。{tried}{cache_tag} 都空 —— "
            f"这种一般是私图、新图，或者裁过{(' ' + why) if why else ''}"
        )

    lines = [HEAD, "", head]
    if int(artwork_dropped or 0) > 0:
        lines.append(
            f"只翻到 {int(artwork_dropped)} 张相似画作（相似度不够，没当成图源）；"
            "真人 coser 的号一般在推特"
        )
    if int(name_dropped or 0) > 0:
        handles = [str(h).strip() for h in (dropped_handles or []) if str(h).strip()][:3]
        shown = f"（{'、'.join(handles)}）" if handles else ""
        if consensus_name and int(consensus_hits or 0) > 0:
            # 说清楚「主轴」是怎么来的：这批结果里有几条都在讲同一个名字。
            # 不写出来，用户会以为插件有个莫名其妙的白名单。
            lines.append(
                f"翻到 {int(name_dropped)} 个别人的号{shown}：这批结果里有 "
                f"{int(consensus_hits)} 条都在讲「{trim_text(consensus_name, 24)}」，"
                "名字对不上，所以没报"
            )
        else:
            lines.append(
                f"翻到 {int(name_dropped)} 个别人的号{shown}，名字和这批结果的主轴对不上，没报"
            )
    # 没账号时，把候选图源按「最可能是原主」的顺序列出来 —— 用户能点开自己核对，
    # 比任何统计都有用。
    sources = _ranked_sources(candidates)
    if sources:
        lines.append("")
        lines.append("🔗 相似图源（按相关性排序，点开核对）：")
        lines.extend(_format_sources(sources))
        if not _has_social_source(candidates):
            lines.append(
                "以上没有一条社交平台链接（都是画作站/图站/转载站），"
                "所以抽不出账号；换成未压缩的原图可能有用。"
            )
    text = "\n".join(lines)
    return _fit(text)

