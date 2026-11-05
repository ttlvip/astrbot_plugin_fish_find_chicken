"""回话排版：把抽出来的账号变成一条能直接发群里的消息。

版式是固定的（`🎯 鱼鱼找鸡结果 / 🌐 平台 / 👤 名字 / 🆔 账号`），改文案前请同步 README。
「没查到」的三种原因要分开说，否则用户分不清是引擎没工作、还是翻到了但对不上人。
"""

from __future__ import annotations

from urllib.parse import unquote

from .accounts import (
    ARTWORK_PLATFORMS,
    account_name_of,
    is_social_host,
    display_handle,
    normalize_person_name,
    platform_label,
)
from .engines import engine_labels
from .textutil import host_of, trim_text


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
            -(r["similarity"] if r["similarity"] is not None else -1.0),
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
    """把候选图源排成「序号. 站点｜标题 — 链接」的一行一条。"""
    lines = []
    for position, row in enumerate(rows, start=1):
        site = row["host"] or "未知站点"
        title = row["title"]
        # 链接太长就把中间省略，保留域名 + 尾部（帖子 id 在尾部，最有用）
        url = row["url"]
        if len(url) > 78:
            url = url[:52] + "…" + url[-24:]
        head = f"{position}. {site}"
        if row["similarity"] is not None:
            head += f"（相似度 {row['similarity'] * 100:.0f}%）"
        if title:
            head += f"｜{title}"
        lines.append(f"{head} — {url}")
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
        for account in accounts:
            url = str(account.get("url") or "").strip()
            guess = ""
            if account.get("confidence") == "inferred":
                guess = "（推断，未核实）"
            elif account.get("confidence") == "weak":
                guess = "（相似图，未核实）"
            handle = display_handle(account) + (f" · {url}" if url else "")
            # 推特昵称经常带一长串后缀（`月城りあ🌙🍼10/11(日)コスROM▹▸C-14`），抄全了没法看
            name = trim_text(account_name_of(account, candidates), 32)
            blocks.append(
                "\n".join(
                    [
                        f"🌐 平台：{platform_label(account.get('platform'))}",
                        f"👤 名字：{name}",
                        f"🆔 账号：{handle}{guess}",
                    ]
                )
            )
        text = "\n".join([HEAD + ("（缓存）" if cached else ""), "", "\n\n".join(blocks)])
        # 只有画作平台的结果时，得说清楚这不是 coser 本人的号
        if note:
            text += f"\n\n· {note}"
        if all(a.get("platform") in ARTWORK_PLATFORMS for a in accounts):
            text += (
                "\n\n· Pixiv 是画作平台（画师的作品页），不是 coser 本人的账号；"
                "真人号一般在推特"
            )
        if len(text) > 900:
            text = text[:897] + "…"
        return text

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
    if len(text) > 900:
        text = text[:897] + "…"
    return text
