"""离线单测：账号抽取（core/accounts.py）。

这些都来自真机上踩过的坑：把别人的号报成这张图的人、把微博文章页当成账号。
跑：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.accounts import (  # noqa: E402
    account_name_of,
    display_handle,
    drop_weak_artwork,
    extract_accounts,
    match_platform,
    normalize_person_name,
)


class MatchPlatformTest(unittest.TestCase):
    def test_twitter_post_converges_to_profile(self):
        hit = match_platform("https://x.com/izayoill/status/1234567890?s=20")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["platform"], "twitter")
        self.assertEqual(hit["handle"], "@izayoill")
        self.assertEqual(hit["url"], "https://x.com/izayoill")

    def test_twitter_reserved_paths_are_not_accounts(self):
        for url in (
            "https://x.com/i/status/1",
            "https://x.com/intent/tweet?text=hi",
            "https://x.com/home",
            "https://twitter.com/search?q=coser",
        ):
            self.assertIsNone(match_platform(url), url)

    def test_weibo_nick_and_uid(self):
        nick = match_platform("https://weibo.com/n/%E9%98%BF%E8%96%B0kaOri")
        self.assertEqual(nick["platform"], "weibo")
        self.assertEqual(nick["url"], "https://weibo.com/n/阿薰kaOri")
        uid = match_platform("https://weibo.com/u/1234567890")
        self.assertEqual(uid["handle"], "1234567890")
        self.assertEqual(uid["url"], "https://weibo.com/u/1234567890")
        # 内容路径也应收成主页
        deep = match_platform("https://weibo.com/u/1234567890/XXXX")
        self.assertEqual(deep["url"], "https://weibo.com/u/1234567890")

    def test_instagram_post_converges_to_profile(self):
        hit = match_platform("https://www.instagram.com/some_user/p/AbCdEf123/")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["platform"], "instagram")
        self.assertEqual(hit["handle"], "@some_user")
        self.assertEqual(hit["url"], "https://www.instagram.com/some_user/")
        self.assertIsNone(match_platform("https://www.instagram.com/p/AbCdEf123/"))

    def test_weibo_article_page_is_not_account(self):
        self.assertIsNone(match_platform("https://weibo.com/ttarticle/p/show?id=2309404"))

    def test_pixiv_old_and_new_forms(self):
        old = match_platform("https://www.pixiv.net/member.php?id=12345")
        new = match_platform("https://www.pixiv.net/users/12345")
        self.assertEqual(old["handle"], "12345")
        self.assertEqual(new["url"], "https://www.pixiv.net/users/12345")

    def test_bilibili_space(self):
        hit = match_platform("https://space.bilibili.com/88888/video")
        self.assertEqual(hit["platform"], "bilibili")
        self.assertEqual(hit["handle"], "88888")

    def test_xiaohongshu_profile(self):
        hit = match_platform("https://www.xiaohongshu.com/user/profile/5f1c2d3e000000000101abcd")
        self.assertEqual(hit["platform"], "xiaohongshu")
        self.assertEqual(hit["url"], "https://www.xiaohongshu.com/user/profile/5f1c2d3e000000000101abcd")

    def test_xiaohongshu_explore_is_not_account(self):
        self.assertIsNone(match_platform("https://www.xiaohongshu.com/explore/abc123"))


class ExtractAccountsTest(unittest.TestCase):
    def test_direct_account_from_lens_card(self):
        candidates = [
            {
                "engine": "lens",
                "title": "X阿薰kaOri防盗版(@izayoill) / X",
                "sourceUrl": "https://x.com/izayoill",
                "authorUrl": "",
                "similarity": None,
                "links": ["https://x.com/izayoill"],
            }
        ]
        accounts = extract_accounts(candidates)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["platform"], "twitter")
        self.assertEqual(accounts[0]["url"], "https://x.com/izayoill")
        self.assertEqual(accounts[0]["confidence"], "direct")
        self.assertEqual(account_name_of(accounts[0], candidates), "阿薰kaOri防盗版")

    def test_same_source_weibo_nick_and_uid_collapses_to_one(self):
        source = "https://weibo.com/ttarticle/p/show?id=1"
        candidates = [
            {"engine": "lens", "title": "", "sourceUrl": source, "links": ["https://weibo.com/n/abc"]},
            {"engine": "lens", "title": "", "sourceUrl": source, "links": ["https://weibo.com/u/1234567"]},
        ]
        accounts = extract_accounts(candidates, strict_names=False)
        self.assertEqual(len(accounts), 1)
        # 同一组里留「最正式」的那条：/u/<uid>
        self.assertEqual(accounts[0]["url"], "https://weibo.com/u/1234567")

    def test_signature_platform_ignored(self):
        candidates = [
            {"engine": "lens", "title": "", "sourceUrl": "https://x.com/a_user", "links": []},
        ]
        accounts = extract_accounts(candidates)
        self.assertEqual(accounts[0]["handle"], "@a_user")

    def test_strict_mode_drops_wrong_person(self):
        """同一角色好几个 coser 各拍一版时，只报名字对得上主轴的那个。"""
        candidates = [
            {
                "engine": "lens",
                "title": "CN : 阿薰kaOri 尤诺 正片",
                "sourceUrl": "https://example.com/1",
                "links": [],
            },
            {
                "engine": "lens",
                "title": "X阿薰kaOri(@izayoill) / X",
                "sourceUrl": "https://x.com/izayoill",
                "links": ["https://x.com/izayoill"],
            },
            {
                "engine": "lens",
                "title": "阿薰kaOri 同角色另一套",
                "sourceUrl": "https://example.com/2",
                "links": [],
            },
            {
                "engine": "lens",
                "title": "X阿抽崽(@Akane_ACZ) / X",
                "sourceUrl": "https://x.com/Akane_ACZ",
                "links": ["https://x.com/Akane_ACZ"],
            },
        ]
        dropped = []
        accounts = extract_accounts(
            candidates, strict_names=True, on_drop=lambda acc, cons: dropped.append(acc)
        )
        urls = [a["url"] for a in accounts]
        self.assertIn("https://x.com/izayoill", urls)
        self.assertNotIn("https://x.com/Akane_ACZ", urls)
        self.assertTrue(dropped, "被挡掉的号应该回报给调用方")

    def test_consensus_anchored_to_lens_ignores_anime_noise(self):
        """SauceNAO 的动漫标题不能参与名字共识，否则会把真正的 X 账号全丢光（实测踩过）。"""
        lens_candidates = [
            {
                "engine": "lens",
                "title": "Xくりす on X: \"Lore accurate yuuka\"",
                "sourceUrl": "https://x.com/kuuritac",
                "links": ["https://x.com/kuuritac"],
            },
            {
                "engine": "lens",
                "title": "X白银河on X: \"📭\"",
                "sourceUrl": "https://x.com/qn6sk",
                "links": ["https://x.com/qn6sk"],
            },
        ]
        noise = [
            {
                "engine": "saucenao",
                "title": "VTuberまとめ",
                "sourceUrl": "https://www.pixiv.net/artworks/1",
                "similarity": 0.62,
                "links": ["https://www.pixiv.net/artworks/1"],
            },
            {
                "engine": "saucenao",
                "title": "【再掲】観音坂独歩総受けlog",
                "sourceUrl": "https://www.pixiv.net/artworks/2",
                "similarity": 0.55,
                "links": ["https://www.pixiv.net/artworks/2"],
            },
        ]
        accounts = extract_accounts(lens_candidates + noise, consensus_candidates=lens_candidates)
        urls = {a["url"] for a in accounts}
        self.assertIn("https://x.com/kuuritac", urls)
        self.assertIn("https://x.com/qn6sk", urls)

    def test_strict_all_dropped_falls_back_with_marker(self):
        """严格过滤把号全挡掉时不能空手：列出候选并标成未核实（否则用户什么都拿不到）。"""
        candidates = [
            {"engine": "lens", "title": "CN : 阿薰kaOri 尤诺", "sourceUrl": "https://pixiv.net/a/1", "links": []},
            {"engine": "lens", "title": "阿薰kaOri 另一套", "sourceUrl": "https://pixiv.net/a/2", "links": []},
            {"engine": "lens", "title": "阿薰kaOri 第三套", "sourceUrl": "https://pixiv.net/a/3", "links": []},
            {"engine": "lens", "title": "X@Akane_ACZ", "sourceUrl": "https://x.com/Akane_ACZ",
             "links": ["https://x.com/Akane_ACZ"]},
        ]
        accounts = extract_accounts(candidates, strict_names=True)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["url"], "https://x.com/Akane_ACZ")
        self.assertEqual(accounts[0]["confidence"], "weak")
        self.assertTrue(accounts[0].get("fallback"))

    def test_strict_keeps_match_when_available(self):
        """有对得上主轴的号时，不该触发回退。"""
        candidates = [
            {"engine": "lens", "title": "阿薰kaOri 第一套", "sourceUrl": "https://pixiv.net/a/1", "links": []},
            {"engine": "lens", "title": "阿薰kaOri 第二套", "sourceUrl": "https://pixiv.net/a/2", "links": []},
            {"engine": "lens", "title": "X阿薰kaOri(@izayoill) / X", "sourceUrl": "https://x.com/izayoill",
             "links": ["https://x.com/izayoill"]},
            {"engine": "lens", "title": "X阿抽崽(@Akane_ACZ) / X", "sourceUrl": "https://x.com/Akane_ACZ",
             "links": ["https://x.com/Akane_ACZ"]},
        ]
        accounts = extract_accounts(candidates, strict_names=True)
        urls = [a["url"] for a in accounts]
        self.assertIn("https://x.com/izayoill", urls)
        self.assertNotIn("https://x.com/Akane_ACZ", urls)
        self.assertFalse(any(a.get("fallback") for a in accounts))

    def test_artwork_only_falls_back_to_all(self):
        candidates = [
            {
                "engine": "saucenao",
                "title": "とある絵",
                "sourceUrl": "https://www.pixiv.net/artworks/1",
                "authorUrl": "https://www.pixiv.net/users/999",
                "similarity": 0.95,
                "links": ["https://www.pixiv.net/artworks/1", "https://www.pixiv.net/users/999"],
            }
        ]
        accounts = extract_accounts(candidates)
        self.assertEqual(accounts[0]["platform"], "pixiv")
        self.assertEqual(accounts[0]["handle"], "999")

    def test_display_handle(self):
        self.assertEqual(display_handle({"handle": "@abc"}), "@abc")
        self.assertEqual(display_handle({"handle": "123456"}), "123456")

    def test_normalize_person_name(self):
        self.assertEqual(normalize_person_name("阿薰 kaOri·"), normalize_person_name("阿薰KAORI"))


class MatchLinkTest(unittest.TestCase):
    """账号除了主页，还要带上「内容最接近原图」的那条具体链接。"""

    def test_twitter_post_link_is_kept_beside_profile(self):
        candidates = [
            {
                "engine": "lens",
                "title": "X阿薰kaOri(@izayoill) / X",
                "sourceUrl": "https://x.com/izayoill",
                "links": ["https://x.com/izayoill"],
            },
            {
                "engine": "lens",
                "title": "阿薰kaOri on X: 新作",
                "sourceUrl": "https://x.com/izayoill/status/1234567890?s=20&t=abc",
                "links": ["https://x.com/izayoill/status/1234567890?s=20&t=abc"],
            },
        ]
        accounts = extract_accounts(candidates)
        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account["url"], "https://x.com/izayoill")          # 主页
        self.assertEqual(account["profileUrl"], "https://x.com/izayoill")
        # 分享参数要清掉，否则一串 ?s=20&t=… 看着就不知道是哪条
        self.assertEqual(account["matchUrl"], "https://x.com/izayoill/status/1234567890")
        self.assertEqual(account["matchHost"], "x.com")
        self.assertEqual(account["matchUrls"][0]["url"], account["matchUrl"])

    def test_profile_only_leaves_match_empty(self):
        """只有主页时别拿主页凑数，否则等于把同一个链接报两遍。"""
        candidates = [
            {"engine": "lens", "title": "X阿薰kaOri(@izayoill) / X", "sourceUrl": "https://x.com/izayoill",
             "links": ["https://x.com/izayoill"]},
        ]
        account = extract_accounts(candidates)[0]
        self.assertEqual(account["matchUrl"], "")
        self.assertEqual(account["matchUrls"], [])

    def test_tab_page_is_not_a_post(self):
        """x.com/<id>/media 是主页上的 tab，不是某条推文。"""
        candidates = [
            {"engine": "lens", "title": "X阿薰kaOri(@izayoill) / X", "sourceUrl": "https://x.com/izayoill",
             "links": ["https://x.com/izayoill"]},
            {"engine": "lens", "title": "阿薰kaOri media", "sourceUrl": "https://x.com/izayoill/media",
             "links": ["https://x.com/izayoill/media"]},
        ]
        account = extract_accounts(candidates)[0]
        self.assertEqual(account["matchUrl"], "")

    def test_weibo_post_link(self):
        candidates = [
            {"engine": "lens", "title": "阿薰kaOri", "sourceUrl": "https://weibo.com/u/1234567890",
             "links": ["https://weibo.com/u/1234567890"]},
            {"engine": "lens", "title": "阿薰kaOri 的新微博", "sourceUrl": "https://weibo.com/1234567890/Nabcdefg",
             "links": ["https://weibo.com/1234567890/Nabcdefg"]},
        ]
        account = extract_accounts(candidates)[0]
        self.assertEqual(account["url"], "https://weibo.com/u/1234567890")
        self.assertEqual(account["matchUrl"], "https://weibo.com/1234567890/Nabcdefg")

    def test_pixiv_artwork_preferred_over_member_page(self):
        candidates = [
            {
                "engine": "saucenao",
                "title": "とある絵",
                "sourceUrl": "https://www.pixiv.net/artworks/1",
                "authorUrl": "https://www.pixiv.net/users/999",
                "similarity": 0.95,
                "links": ["https://www.pixiv.net/artworks/1", "https://www.pixiv.net/users/999"],
            }
        ]
        account = extract_accounts(candidates)[0]
        self.assertEqual(account["url"], "https://www.pixiv.net/users/999")
        self.assertEqual(account["matchUrl"], "https://www.pixiv.net/artworks/1")

    def test_other_peoples_post_is_not_attached(self):
        """同一条结果卡片里混着别人的链接时，不能挂到当前账号名下。"""
        candidates = [
            {"engine": "lens", "title": "X阿薰kaOri(@izayoill) / X", "sourceUrl": "https://x.com/izayoill",
             "links": ["https://x.com/izayoill", "https://x.com/someone_else/status/42"]},
        ]
        account = extract_accounts(candidates, strict_names=False)[0]
        self.assertEqual(account["handle"], "@izayoill")
        self.assertNotIn("someone_else", account["matchUrl"])


class DropWeakArtworkTest(unittest.TestCase):
    def test_none_similarity_kept(self):
        accounts = [
            {
                "platform": "pixiv",
                "handle": "1",
                "url": "https://www.pixiv.net/users/1",
                "evidence": {"similarity": None},
            }
        ]
        kept, dropped = drop_weak_artwork(accounts, 0.8)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)

    def test_low_score_dropped(self):
        accounts = [
            {
                "platform": "pixiv",
                "handle": "1",
                "url": "https://www.pixiv.net/users/1",
                "evidence": {"similarity": 0.4},
            }
        ]
        kept, dropped = drop_weak_artwork(accounts, 0.8)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)


if __name__ == "__main__":
    unittest.main()
