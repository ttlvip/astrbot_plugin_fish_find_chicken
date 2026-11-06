"""离线单测：引擎解析（SauceNAO / IQDB / trace.moe）与回话排版。

fixture 是照着真实页面结构手写的（真实页面不能进仓库），
只要这几条通过，说明选择器和字段映射没跑偏。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.engines import (  # noqa: E402
    EngineError,
    engine_labels,
    parse_iqdb,
    parse_saucenao,
    parse_tracemoe,
)
from core.reply import format_reply  # noqa: E402

SAUCENAO_HTML = """
<html><body>
<div id="middle">
 <div class="result" id="res0">
  <div class="resultcontentcolumn">
   <div class="resultsimilarityinfo">96.3%</div>
   <div class="resulttitle"><strong>とある作品のイラスト</strong></div>
   <div class="resultcontent">
    <a href="https://www.pixiv.net/artworks/12345678">pixiv</a>
    Member: <a href="https://www.pixiv.net/member.php?id=998877">豊栄@ﾄﾞﾔｧ</a>
   </div>
  </div>
 </div>
 <div class="result" id="res1">
  <div class="resultsimilarityinfo">72.1%</div>
  <div class="resulttitle"><strong>别的一张</strong></div>
  <div class="resultcontent"><a href="https://twitter.com/someone/status/1">twitter</a></div>
 </div>
</div>
</body></html>
"""

IQDB_HTML = """
<html><body>
<div class="pages">
 <div><a href="//danbooru.donmai.us/post/show/1"><img></a> 96% similarity</div>
 <div><a href="https://yande.re/post/show/2">yande.re</a></div>
 <div><a href="https://iqdb.org/self">self link</a></div>
</div>
</body></html>
"""

TRACEMOE = {
    "result": [
        {
            "similarity": 0.97,
            "episode": 3,
            "filename": "ep03.mkv",
            "anilist": {"id": 101922, "title": {"native": "鬼滅の刃"}},
        },
        {"similarity": 0.5, "anilist": {"id": 2, "title": {"native": "低分"}}},
    ]
}


class ParserTest(unittest.TestCase):
    def test_saucenao(self):
        candidates = parse_saucenao(SAUCENAO_HTML)
        self.assertEqual(len(candidates), 2)
        first = candidates[0]
        self.assertAlmostEqual(first["similarity"], 0.963, places=3)
        self.assertEqual(first["title"], "とある作品のイラスト")
        self.assertEqual(first["sourceUrl"], "https://www.pixiv.net/artworks/12345678")
        self.assertEqual(first["authorUrl"], "https://www.pixiv.net/member.php?id=998877")
        self.assertEqual(first["authorName"], "豊栄@ﾄﾞﾔｧ")
        self.assertEqual(first["host"], "www.pixiv.net")

    def test_saucenao_empty(self):
        self.assertEqual(parse_saucenao("<html></html>"), [])

    def test_iqdb(self):
        candidates = parse_iqdb(IQDB_HTML)
        hosts = [c["host"] for c in candidates]
        self.assertIn("danbooru.donmai.us", hosts)
        self.assertNotIn("iqdb.org", hosts)
        self.assertAlmostEqual(candidates[0]["similarity"], 0.96, places=2)

    def test_iqdb_queue_raises(self):
        with self.assertRaises(EngineError):
            parse_iqdb("<html>Place in queue</html>")

    def test_tracemoe_threshold(self):
        candidates = parse_tracemoe(TRACEMOE)
        self.assertEqual(len(candidates), 1)
        self.assertIn("鬼滅の刃", candidates[0]["title"])
        self.assertIn("第3集", candidates[0]["title"])
        self.assertEqual(candidates[0]["sourceUrl"], "https://anilist.co/anime/101922")

    def test_engine_labels(self):
        self.assertEqual(engine_labels(["lens", "saucenao"]), ["谷歌 Lens", "SauceNAO"])


class ReplyTest(unittest.TestCase):
    def test_accounts_block(self):
        accounts = [
            {
                "platform": "twitter",
                "handle": "@izayoill",
                "url": "https://x.com/izayoill",
                "authorName": "阿薰kaOri",
                "confidence": "direct",
                "evidence": {"engine": "lens", "similarity": None, "sourceUrl": ""},
            }
        ]
        text = format_reply(accounts=accounts, candidates=[], engines_used=["lens"], failures=[])
        self.assertIn("鱼鱼找鸡结果", text)
        self.assertIn("🌐 平台：Twitter", text)
        self.assertIn("👤 名字：阿薰kaOri", text)
        # 主页单独一行：它和「最相关」是两种用途，不能混在账号那一行里
        self.assertIn("🆔 账号：@izayoill", text)
        self.assertIn("🔗 主页：https://x.com/izayoill", text)
        self.assertLessEqual(len(text), 900)

    def test_most_relevant_link_is_reported(self):
        """光有主页不够：要额外给出「内容最接近这张图」的那条具体页面。"""
        accounts = [
            {
                "platform": "twitter",
                "handle": "@izayoill",
                "url": "https://x.com/izayoill",
                "profileUrl": "https://x.com/izayoill",
                "matchUrl": "https://x.com/izayoill/status/1234567890",
                "matchTitle": "阿薰kaOri on X",
                "authorName": "阿薰kaOri",
                "confidence": "direct",
                "evidence": {"engine": "lens", "similarity": None, "sourceUrl": ""},
            }
        ]
        text = format_reply(accounts=accounts, engines_used=["lens"])
        self.assertIn("🔗 主页：https://x.com/izayoill", text)
        self.assertIn("🎯 最相关：https://x.com/izayoill/status/1234567890", text)
        # 两个字段的用途要说清楚，否则用户不知道该点哪条
        self.assertIn("主页 = 这个人的账号页", text)

    def test_no_specific_page_says_so(self):
        accounts = [
            {
                "platform": "twitter",
                "handle": "@izayoill",
                "url": "https://x.com/izayoill",
                "matchUrl": "",
                "authorName": "阿薰kaOri",
                "confidence": "direct",
                "evidence": {"engine": "lens", "similarity": None, "sourceUrl": ""},
            }
        ]
        text = format_reply(accounts=accounts, engines_used=["lens"])
        self.assertNotIn("🎯 最相关", text)
        self.assertIn("只翻到主页", text)

    def test_legacy_account_uses_evidence_link(self):
        """没说 matchUrl 的老结构：evidence 里的具体页面仍要顶上来，主页则不能重复报。"""
        post = "https://x.com/izayoill/status/999"
        text = format_reply(
            accounts=[
                {
                    "platform": "twitter",
                    "handle": "@izayoill",
                    "url": "https://x.com/izayoill",
                    "authorName": "",
                    "confidence": "direct",
                    "evidence": {"engine": "lens", "similarity": None, "sourceUrl": post},
                }
            ]
        )
        self.assertIn(f"🎯 最相关：{post}", text)

        profile_only = format_reply(
            accounts=[
                {
                    "platform": "twitter",
                    "handle": "@izayoill",
                    "url": "https://x.com/izayoill",
                    "authorName": "",
                    "confidence": "direct",
                    "evidence": {
                        "engine": "lens",
                        "similarity": None,
                        "sourceUrl": "https://x.com/izayoill/",
                    },
                }
            ]
        )
        self.assertNotIn("🎯 最相关", profile_only)

    def test_weak_marked(self):
        accounts = [
            {
                "platform": "weibo",
                "handle": "1234567",
                "url": "https://weibo.com/u/1234567",
                "authorName": "",
                "confidence": "weak",
                "evidence": {"engine": "iqdb", "similarity": 0.9, "sourceUrl": ""},
            }
        ]
        text = format_reply(accounts=accounts)
        self.assertIn("相似图，未核实", text)

    def test_miss_no_engine_ran(self):
        text = format_reply(accounts=[], engines_used=[], failures=[{"engine": "lens", "reason": "超时"}])
        self.assertIn("一个引擎都没跑通", text)
        self.assertIn("lens:超时", text)

    def test_miss_with_candidates(self):
        text = format_reply(
            accounts=[],
            candidates=[{"sourceUrl": "https://example.com/a", "title": "x"}],
            engines_used=["lens"],
            failures=[],
        )
        self.assertIn("翻到 1 条相似图", text)
        # 没账号时列出候选图源，用户能点开核对
        self.assertIn("🔗 相似图源", text)
        self.assertIn("https://example.com/a", text)

    def test_miss_empty(self):
        text = format_reply(accounts=[], candidates=[], engines_used=["saucenao"], failures=[])
        self.assertIn("私图、新图", text)


    def test_no_social_sources_explains_why(self):
        """「翻到 59 条但没有账号」必须自己解释清楚，不能让用户以为是坏了。"""
        candidates = [
            {
                "engine": "lens",
                "title": f"作品{i}",
                "sourceUrl": f"https://www.pixiv.net/artworks/{i}",
                "host": "www.pixiv.net",
                "links": [],
            }
            for i in range(59)
        ]
        text = format_reply(accounts=[], candidates=candidates, engines_used=["lens"], failures=[])
        self.assertIn("🔗 相似图源", text)
        self.assertIn("www.pixiv.net", text)
        self.assertIn("没有一条社交平台链接", text)
        # 统计式的「来源分布」已经去掉（对用户没有行动价值）
        self.assertNotIn("来源分布", text)

    def test_dropped_by_name_is_reported(self):
        """有社交链接但名字对不上时，要说清是被「名字共识」挡掉的，而不是没找到。"""
        candidates = [
            {"engine": "lens", "title": "CN : 阿薰kaOri", "sourceUrl": "https://www.pixiv.net/artworks/1",
             "host": "www.pixiv.net", "links": []},
            {"engine": "lens", "title": "X阿抽崽(@Akane_ACZ) / X", "sourceUrl": "https://x.com/Akane_ACZ",
             "host": "x.com", "links": ["https://x.com/Akane_ACZ"]},
        ]
        text = format_reply(
            accounts=[],
            candidates=candidates,
            engines_used=["lens"],
            failures=[],
            name_dropped=1,
            dropped_handles=["@Akane_ACZ"],
            consensus_name="阿薰kaOri",
            consensus_hits=3,
        )
        self.assertIn("翻到 1 个别人的号（@Akane_ACZ）", text)
        # 说清楚「主轴」是算出来的：这批结果里有几条都在讲这个名字
        self.assertIn("有 3 条都在讲「阿薰kaOri」", text)
        # 有社交链接时不该说「没有一条社交平台链接」
        self.assertNotIn("没有一条社交平台链接", text)

    def test_empty_result_is_not_a_failure(self):
        """引擎「空结果」是正常业务结果，不能出现在失败提示里。"""
        text = format_reply(
            accounts=[],
            candidates=[{"sourceUrl": "https://a.com/x", "title": "t"}],
            engines_used=["lens", "tracemoe"],
            failures=[{"engine": "tracemoe", "reason": "空结果"}],
        )
        self.assertNotIn("空结果", text)

    def test_real_failure_is_still_shown(self):
        text = format_reply(
            accounts=[],
            candidates=[],
            engines_used=["lens"],
            failures=[{"engine": "lens", "reason": "谷歌要求人机验证"}],
        )
        self.assertIn("谷歌要求人机验证", text)



class RankedSourcesTest(unittest.TestCase):
    """没账号时给出的候选列表：社交优先、相似度降序、同站点最多 2 条。"""

    def _rank(self, candidates, limit=5):
        from core.reply import _ranked_sources

        return _ranked_sources(candidates, limit=limit)

    def test_social_first_then_similarity(self):
        candidates = [
            {"sourceUrl": "https://www.pixiv.net/artworks/1", "title": "p1", "similarity": 0.99},
            {"sourceUrl": "https://x.com/a_user/status/1", "title": "t1"},
            {"sourceUrl": "https://www.pixiv.net/artworks/2", "title": "p2", "similarity": 0.95},
        ]
        rows = self._rank(candidates)
        self.assertEqual(rows[0]["host"], "x.com")
        # 非社交的按相似度降序
        self.assertEqual(rows[1]["similarity"], 0.99)
        self.assertEqual(rows[2]["similarity"], 0.95)

    def test_per_host_quota(self):
        candidates = [
            {"sourceUrl": f"https://www.pixiv.net/artworks/{i}", "title": f"p{i}"} for i in range(10)
        ] + [{"sourceUrl": f"https://danbooru.donmai.us/posts/{i}", "title": f"d{i}"} for i in range(5)]
        rows = self._rank(candidates)
        hosts = [r["host"] for r in rows]
        self.assertEqual(len(rows), 5)  # 名额一定要给满
        # 两个站点都要出现，且没有哪个独占（否则 5 条全是 pixiv 等于没给信息）
        self.assertGreaterEqual(hosts.count("www.pixiv.net"), 2)
        self.assertGreaterEqual(hosts.count("danbooru.donmai.us"), 2)
        self.assertLessEqual(max(hosts.count("www.pixiv.net"), hosts.count("danbooru.donmai.us")), 3)

    def test_limit_respected_and_empty_urls_skipped(self):
        candidates = [{"sourceUrl": "", "title": "no url"}] + [
            {"sourceUrl": f"https://example.com/{i}", "title": f"e{i}"} for i in range(9)
        ]
        rows = self._rank(candidates, limit=3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["url"] for r in rows))


if __name__ == "__main__":
    unittest.main()
