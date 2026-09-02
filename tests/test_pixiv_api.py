# -*- coding: UTF-8 -*-
"""pixiv_api 模块单元测试（不依赖网络与 astrbot）。"""

import asyncio
import json
import os
import tempfile
import time
import unittest


def _run(coro):
    return asyncio.run(coro)


from pixiv_api import (
    APP_API_HOST,
    OAUTH_HOST,
    R18_LEVELS,
    PixivClient,
    PixivError,
    TokenStore,
    _hour_rotation,
    extract_illust_id,
    filter_illusts,
    oauth_headers,
    parse_illust,
    ranking_params,
    sample_balanced,
    search_params,
    user_illusts_params,
)


def make_illust(illust_id=123456, title="Test", bookmarks=5000, views=99999,
                x_restrict=0, tags=None, page_count=1, artist="画师A"):
    tags = tags if tags is not None else [{"name": "miku", "translated_name": "初音未来"}]
    base = {
        "id": illust_id,
        "title": title,
        "type": "illust",
        "x_restrict": x_restrict,
        "total_bookmarks": bookmarks,
        "total_view": views,
        "page_count": page_count,
        "tags": tags,
        "user": {"id": 99, "name": artist},
        "image_urls": {"medium": f"https://i.pximg.net/medium/{illust_id}.jpg",
                         "large": f"https://i.pximg.net/large/{illust_id}.jpg"},
        "meta_single_page": {"original_image_url": f"https://i.pximg.net/original/{illust_id}.png"},
    }
    if page_count > 1:
        base["meta_pages"] = [
            {"image_urls": {"original": f"https://i.pximg.net/original/{illust_id}_p{i}.png"}}
            for i in range(page_count)
        ]
        base["meta_single_page"] = {}
    return base


class TestExtractId(unittest.TestCase):
    def test_link(self):
        self.assertEqual(extract_illust_id("https://www.pixiv.net/artworks/123456"), 123456)

    def test_link_en(self):
        self.assertEqual(extract_illust_id("看看 https://www.pixiv.net/en/artworks/888888 这张"), 888888)

    def test_pure_id(self):
        self.assertEqual(extract_illust_id("123456"), 123456)

    def test_id_with_text(self):
        self.assertEqual(extract_illust_id("pixiv 555555 好看"), 555555)

    def test_invalid(self):
        self.assertIsNone(extract_illust_id("没有数字"))
        self.assertIsNone(extract_illust_id("123"))
        self.assertIsNone(extract_illust_id(""))


class TestOauthHeaders(unittest.TestCase):
    def test_structure(self):
        h = oauth_headers(now_ts=1700000000)
        self.assertIn("x-client-time", h)
        self.assertIn("x-client-hash", h)
        self.assertEqual(h["user-agent"], "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)")

    def test_hash_is_md5_hex(self):
        h = oauth_headers(now_ts=1700000000)
        ts = h["x-client-time"]
        import hashlib
        expected = hashlib.md5((ts + "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c").encode()).hexdigest()
        self.assertEqual(h["x-client-hash"], expected)


class TestParseIllust(unittest.TestCase):
    def test_basic(self):
        item = make_illust()
        r = parse_illust(item)
        self.assertEqual(r["id"], 123456)
        self.assertEqual(r["title"], "Test")
        self.assertEqual(r["artist"], "画师A")
        self.assertEqual(r["bookmarks"], 5000)
        self.assertEqual(r["r18_label"], "一般向")
        self.assertIn("初音未来", r["tags"])
        self.assertEqual(r["url"], "https://www.pixiv.net/artworks/123456")
        self.assertEqual(r["original_url"], "https://i.pximg.net/original/123456.png")

    def test_multipage(self):
        r = parse_illust(make_illust(page_count=3))
        self.assertEqual(r["page_count"], 3)
        self.assertEqual(len(r["pages"]), 3)
        self.assertIn("_p2.png", r["pages"][2])

    def test_ai_tag_detected(self):
        r = parse_illust(make_illust(tags=[{"name": "AI-generated"}]))
        self.assertTrue(r["ai"])
        r2 = parse_illust(make_illust(tags=[{"name": "original"}]))
        self.assertFalse(r2["ai"])

    def test_r18_label(self):
        self.assertEqual(parse_illust(make_illust(x_restrict=1))["r18_label"], "R-18")
        self.assertEqual(parse_illust(make_illust(x_restrict=2))["r18_label"], "R-18G")


class TestFilterIllusts(unittest.TestCase):
    def setUp(self):
        self.items = [
            make_illust(1, "safe1", bookmarks=2000, x_restrict=0),
            make_illust(2, "r18", bookmarks=3000, x_restrict=1),
            make_illust(3, "r18g", bookmarks=4000, x_restrict=2),
            make_illust(4, "low", bookmarks=500, x_restrict=0),
            make_illust(5, "ai", bookmarks=9000, x_restrict=0, tags=[{"name": "AI生成"}]),
        ]

    def test_safe_default(self):
        out = filter_illusts(self.items)
        ids = [r["id"] for r in out]
        self.assertIn(1, ids)
        self.assertNotIn(2, ids)
        self.assertNotIn(3, ids)

    def test_r18_level(self):
        out = filter_illusts(self.items, r18_level="r18")
        self.assertIn(2, [r["id"] for r in out])
        self.assertNotIn(3, [r["id"] for r in out])
        out2 = filter_illusts(self.items, r18_level="r18g")
        self.assertIn(3, [r["id"] for r in out2])

    def test_min_bookmarks(self):
        out = filter_illusts(self.items, min_bookmarks=1000)
        self.assertNotIn(4, [r["id"] for r in out])
        self.assertIn(1, [r["id"] for r in out])

    def test_filter_ai(self):
        out = filter_illusts(self.items, filter_ai=True)
        self.assertNotIn(5, [r["id"] for r in out])

    def test_limit(self):
        out = filter_illusts(self.items, limit=2)
        self.assertEqual(len(out), 2)


class TestSampleBalanced(unittest.TestCase):
    def _hot(self, ids):
        return [make_illust(i, bookmarks=10000 - i * 100) for i in ids]

    def test_small_pool_takes_all(self):
        hot = self._hot([1, 2, 3])
        out = sample_balanced(hot, [], 5)
        self.assertEqual([r["id"] for r in out], [1, 2, 3])

    def test_first_is_hot_top(self):
        hot = self._hot([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        out = sample_balanced(hot, [], 3)
        self.assertEqual(out[0]["id"], 1)

    def test_mid_sampling_and_fresh(self):
        # 10 个热门取 3：首位 + 中段一张 + 最新一张
        hot = self._hot([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        fresh = self._hot([101])
        out = sample_balanced(hot, fresh, 3)
        # 热门中段：rest=[2..10] 取中间索引 4 → id 6
        self.assertEqual([r["id"] for r in out], [1, 6, 101])

    def test_fresh_skips_duplicate(self):
        hot = self._hot([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        fresh = self._hot([1, 101])  # 1 已在热门中出现
        out = sample_balanced(hot, fresh, 5)
        ids = [r["id"] for r in out]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(101, ids)

    def test_empty_hot_uses_fresh(self):
        out = sample_balanced([], self._hot([201, 202]), 3)
        self.assertEqual([r["id"] for r in out], [201, 202])

    def test_zero_hot_zero_fresh(self):
        self.assertEqual(sample_balanced([], [], 3), [])

    def test_hour_rotation_stable_and_in_range(self):
        a = _hour_rotation("miku")
        b = _hour_rotation("miku")
        self.assertEqual(a, b)
        self.assertEqual(a % 30, 0)
        self.assertIn(a, (0, 30, 60))


class TestParams(unittest.TestCase):
    def test_search_params(self):
        p = search_params("miku", scope="tag", sort="popular_desc", filter_ai=True)
        self.assertEqual(p["word"], "miku")
        self.assertEqual(p["search_target"], "partial_match_for_tags")
        self.assertEqual(p["sort"], "popular_desc")
        self.assertNotIn("search_ai_type", p)
        self.assertEqual(p["filter"], "for_android")

    def test_search_params_scope_and_sort(self):
        self.assertEqual(search_params("x", scope="title")["search_target"], "title_and_caption")
        self.assertEqual(search_params("x", scope="both")["search_target"], "partial_match_for_tags")
        self.assertEqual(search_params("x", sort="bogus")["sort"], "popular_desc")
        self.assertEqual(search_params("x", filter_ai=False)["search_ai_type"], 1)
        self.assertEqual(search_params("x", offset=30)["offset"], 30)

    def test_ranking_params(self):
        self.assertEqual(ranking_params("daily"), {"mode": "day", "filter": "for_android"})
        self.assertEqual(ranking_params("weekly")["mode"], "week")
        self.assertEqual(ranking_params("monthly")["mode"], "month")
        self.assertEqual(ranking_params("daily_r18")["mode"], "day_r18")
        self.assertEqual(ranking_params("r18g")["mode"], "week_r18g")
        self.assertEqual(ranking_params("nonsense")["mode"], "day")
        self.assertEqual(ranking_params("daily", date="2026-09-01")["date"], "2026-09-01")

    def test_user_illusts_params(self):
        self.assertEqual(user_illusts_params(12345), {"user_id": "12345", "type": "illust"})


class TestTokenStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token.json")
            ts = TokenStore(path, fallback_refresh="rt-1")
            ts.update("at-1", "rt-2", 3600)
            ts2 = TokenStore(path)
            self.assertEqual(ts2.access_token, "at-1")
            self.assertEqual(ts2.refresh_token, "rt-2")
            self.assertTrue(ts2.usable())

    def test_usable_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = TokenStore(os.path.join(tmp, "t.json"), fallback_refresh="x")
            ts.update("at", "rt", 3600)
            ts.expires_at = time.time() - 10
            self.assertFalse(ts.usable())

    def test_fallback_refresh(self):
        ts = TokenStore(path=None, fallback_refresh="rt-fallback")
        self.assertEqual(ts.refresh_token, "rt-fallback")
        self.assertFalse(ts.usable())


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return json.dumps(self._payload)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.responses:
            payload, status = self.responses.pop(0)
        else:
            payload, status = {}, 200
        return FakeResponse(payload, status)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    async def close(self):
        pass


class TestPixivClient(unittest.TestCase):
    def _client(self, responses=None, refresh_token="rt"):
        session = FakeSession(responses or [({"illusts": [make_illust()]}, 200)])
        c = PixivClient(refresh_token=refresh_token, proxy="http://127.0.0.1:7890", session=session)
        return c, session

    def test_refresh_auth(self):
        token_resp = {"response": {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}}
        c, s = self._client([(token_resp, 200), ({"illusts": [make_illust()]}, 200)])
        _run(c.refresh_auth())
        self.assertEqual(c.tokens.access_token, "at-new")
        self.assertEqual(c.tokens.refresh_token, "rt-new")
        _, url, kwargs = s.calls[0]
        self.assertEqual(url, f"{OAUTH_HOST}/auth/token")
        self.assertEqual(kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(kwargs["data"]["refresh_token"], "rt")
        self.assertIn("x-client-time", kwargs["headers"])

    def test_missing_token_error(self):
        c, _ = self._client([], refresh_token="")
        with self.assertRaises(PixivError) as ctx:
            _run(c.refresh_auth())
        self.assertIn("refresh_token", str(ctx.exception))

    def test_search_uses_proxy_and_filters(self):
        items = [make_illust(1, bookmarks=5000, x_restrict=0), make_illust(2, bookmarks=500, x_restrict=0)]
        c, s = self._client([({"illusts": items}, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(
            c.search_illust("miku", min_bookmarks=1000, limit=5)
        )
        self.assertEqual([r["id"] for r in out], [1])
        _, url, kwargs = s.calls[0]
        self.assertEqual(url, f"{APP_API_HOST}/v1/search/illust")
        self.assertEqual(kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer at")
        self.assertEqual(kwargs["params"]["word"], "miku")

    def test_search_relaxes_threshold(self):
        items = [make_illust(11, bookmarks=100, x_restrict=0), make_illust(12, bookmarks=50, x_restrict=0)]
        c, _s = self._client([({"illusts": items}, 200), ({"illusts": [], "next_url": None}, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.search_illust("miku", min_bookmarks=1000, limit=5))
        self.assertEqual(len(out), 2)
        self.assertEqual([r["id"] for r in out], [11, 12])  # 软降级后按收藏降序

    def test_search_sorted_by_bookmarks(self):
        items = [make_illust(21, bookmarks=50), make_illust(22, bookmarks=9000), make_illust(23, bookmarks=500)]
        c, _s = self._client([({"illusts": items}, 200), ({"illusts": [], "next_url": None}, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.search_illust("x", min_bookmarks=0, limit=5))
        self.assertEqual([r["id"] for r in out], [22, 23, 21])

    def test_premium_balanced_mix(self):
        # 默认均衡混排：第 0 页热门 + 最新一页并发；热门还有下一页才追加轮换页
        hot1 = [make_illust(31, bookmarks=5000), make_illust(32, bookmarks=8000)]
        hot2 = [make_illust(33, bookmarks=7000), make_illust(34, bookmarks=6000)]
        fresh1 = [make_illust(35, bookmarks=2000)]
        c, s = self._client([
            ({"illusts": hot1, "next_url": "http://x?offset=30"}, 200),
            ({"illusts": fresh1}, 200),
            ({"illusts": hot2}, 200),  # 轮换深层页
        ])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.search_illust("miku", min_bookmarks=1000, limit=3, premium=True))
        self.assertEqual(len(s.calls), 3)
        # 第 0 页热门、最新页，以及深层轮换页（offset 必为 30/60/90 之一）
        self.assertEqual(s.calls[0][2]["params"]["sort"], "popular_desc")
        self.assertNotIn("offset", s.calls[0][2]["params"])
        self.assertEqual(s.calls[1][2]["params"]["sort"], "date_desc")
        self.assertEqual(s.calls[2][2]["params"]["sort"], "popular_desc")
        self.assertIn(s.calls[2][2]["params"].get("offset"), (30, 60, 90))
        ids = [r["id"] for r in out]
        # 热门按收藏排序后 [32,33,34,31]：首位=最热，中段取一张，再补一张最新
        self.assertEqual(ids[0], 32)
        self.assertIn(35, ids)
        self.assertEqual(len(set(ids)), 3)

    def test_premium_balanced_small_pool(self):
        # 结果很少（无 next_url）：只请求热门第 0 页 + 最新页，不越界追深层页
        items = [
            make_illust(41, bookmarks=9000), make_illust(42, bookmarks=8000),
            make_illust(43, bookmarks=7000), make_illust(44, bookmarks=6000),
            make_illust(45, bookmarks=5000),
        ]
        fresh1 = [make_illust(46, bookmarks=2000)]
        c, s = self._client([
            ({"illusts": items, "next_url": None}, 200),
            ({"illusts": fresh1}, 200),
        ])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.search_illust("rare_tag", min_bookmarks=1000, limit=3, premium=True))
        self.assertEqual(len(s.calls), 2)  # 没有深层页请求
        ids = [r["id"] for r in out]
        # [41..45] 排序降序：首位 41，中段步进取 44，再补最新 46
        self.assertEqual(ids, [41, 44, 46])

    def test_premium_balanced_extra_empty(self):
        # 深层轮换页越界/为空：静默回退到第 0 页池，不报错
        items = [
            make_illust(51, bookmarks=9000), make_illust(52, bookmarks=8000),
            make_illust(53, bookmarks=7000),
        ]
        fresh1 = [make_illust(56, bookmarks=2000)]
        c, s = self._client([
            ({"illusts": items, "next_url": "http://x?offset=30"}, 200),
            ({"illusts": fresh1}, 200),
            ({"illusts": [], "next_url": None}, 200),  # 深层页空
        ])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.search_illust("small_tag", min_bookmarks=1000, limit=3, premium=True))
        self.assertEqual(len(s.calls), 3)
        self.assertEqual([r["id"] for r in out], [51, 53, 56])

    def test_premium_strict_single_page(self):
        # 关闭均衡混排：单页直取，保持 API 热门顺序（旧行为）
        items = [make_illust(31, bookmarks=5000), make_illust(32, bookmarks=8000)]
        c, s = self._client([({"illusts": items}, 200), ({"illusts": items}, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.search_illust("miku", min_bookmarks=1000, limit=5, premium=True, balanced=False))
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(s.calls[0][2]["params"]["sort"], "popular_desc")
        self.assertEqual([r["id"] for r in out], [31, 32])  # API 顺序保留

    def test_date_desc_keeps_order(self):
        items = [make_illust(41, bookmarks=50), make_illust(42, bookmarks=9000), make_illust(43, bookmarks=500)]
        c, _s = self._client([({"illusts": items}, 200), ({"illusts": [], "next_url": None}, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.search_illust("x", sort="date_desc", min_bookmarks=0, limit=5))
        self.assertEqual([r["id"] for r in out], [41, 42, 43])  # date_desc 保持 API 顺序

    def test_ranking(self):
        c, s = self._client([({"illusts": [make_illust()]}, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.illust_ranking("daily"))
        self.assertEqual(len(out), 1)
        self.assertEqual(s.calls[0][2]["params"]["mode"], "day")

    def test_http_error_raises(self):
        c, _s = self._client([({"error": {"message": "auth failed"}}, 403)])
        c.tokens.update("at", "rt", 3600)
        with self.assertRaises(PixivError) as ctx:
            _run(c.illust_ranking("daily"))
        self.assertIn("403", str(ctx.exception))

    def test_related(self):
        items = [make_illust(71, bookmarks=5000), make_illust(72, bookmarks=100, x_restrict=1)]
        c, s = self._client([({"illusts": items}, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.related_illusts(123456, r18_level="safe", min_bookmarks=1000, limit=5))
        self.assertEqual([r["id"] for r in out], [71])  # R18/门槛过滤
        self.assertEqual(s.calls[0][2]["params"]["illust_id"], "123456")

    def test_related_relax(self):
        items = [make_illust(81, bookmarks=50)]
        c, _s = self._client([({"illusts": items}, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.related_illusts(1, r18_level="safe", min_bookmarks=1000, limit=5))
        self.assertEqual([r["id"] for r in out], [81])  # 软降级

    def test_detail(self):
        resp = {"illust": {"illust": make_illust(777)}}
        c, s = self._client([(resp, 200)])
        c.tokens.update("at", "rt", 3600)
        out = _run(c.illust_detail(777))
        self.assertEqual(out["id"], 777)
        self.assertEqual(s.calls[0][2]["params"]["illust_id"], "777")

    def test_r18_levels_consistency(self):
        self.assertEqual(R18_LEVELS["safe"], {0})
        self.assertEqual(R18_LEVELS["r18"], {0, 1})
        self.assertEqual(R18_LEVELS["r18g"], {0, 1, 2})


if __name__ == "__main__":
    unittest.main()