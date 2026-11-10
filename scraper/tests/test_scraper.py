#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper 数据层回归测试（标准库 unittest，无需 pip install）
==============================================================================
覆盖最脆弱的正则/解析/合并逻辑：
    nerdzap.parse_cards        卡片解析（live fixture + 合成边界用例）
    fetch_free.clean_title     聚合源标题残留清洗
    fetch_free.norm_title      归一（复数 giveaway(s)）
    fetch_free.merge_section   跨源合并（缩写前缀包含匹配）
    fetch_prime_preview.parse_article   公告文章排期解析
    fetch_prime_preview.merge / drop_live   预告合并与撞车去重
    push_cloud.normalize       入库归一（startAt 兜底 / preview 透传）

运行：
    python -m unittest discover -s scraper/tests -v
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRAPER = HERE.parent
sys.path.insert(0, str(SCRAPER))
sys.path.insert(0, str(SCRAPER.parent / "tools"))

import nerdzap  # noqa: E402
import fetch_free  # noqa: E402
import fetch_prime_preview  # noqa: E402
import push_cloud  # noqa: E402


# --------------------------------------------------------------------------- #
# nerdzap 卡片解析
# --------------------------------------------------------------------------- #

class TestNerdzap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fixture = HERE / "fixtures" / "nerdzap.html"
        if fixture.exists():
            cls.page = fixture.read_text(encoding="utf-8", errors="replace")
        else:
            cls.page = ""

    def test_fixture_parses_ten_cards(self):
        if not self.page:
            self.skipTest("fixture 不存在")
        cards = nerdzap.parse_cards(self.page)
        self.assertEqual(len(cards), 10)
        titles = [c["title"] for c in cards]
        self.assertIn("Squirrel with a Gun", titles)
        self.assertIn("WRC Generations - The FIA WRC Official Game", titles)

    def test_fixture_platform_mapping(self):
        if not self.page:
            self.skipTest("fixture 不存在")
        cards = {c["title"]: c for c in nerdzap.parse_cards(self.page)}
        # 'Prime Gaming → Amazon Games / Luna' → Amazon Games App
        self.assertEqual(cards["Squirrel with a Gun"]["platform"], "Amazon Games App")
        # 'Prime Gaming → Epic Games Store' → Epic Games Store
        self.assertEqual(
            cards["WRC Generations - The FIA WRC Official Game"]["platform"],
            "Epic Games Store")
        # 'Prime Gaming → GOG' → GOG
        self.assertEqual(cards["Conarium"]["platform"], "GOG")

    def test_fixture_iso_timestamps(self):
        if not self.page:
            self.skipTest("fixture 不存在")
        cards = {c["title"]: c for c in nerdzap.parse_cards(self.page)}
        sq = cards["Squirrel with a Gun"]
        self.assertTrue(sq["startAt"].endswith(".000Z"))
        self.assertEqual(sq["startAt"][:10], "2026-11-05")
        self.assertEqual(sq["status"], "now")

    def test_synthetic_human_date_fallback(self):
        """没带 data-end 属性时，从 'Ends 16 Dec 2026' 文案兜底。"""
        page = """
        <article class="fg-card" data-id="prime-x">
          <a class="fg-img" href="https://luna.amazon.com/claims/home"><img src="c.jpg"/></a>
          <h3 class="fg-title">Some Game</h3>
          <span class="fg-store">Prime Gaming &rarr; GOG</span>
          <span class="fg-time">Ends 16 Dec 2026</span>
        </article>"""
        cards = nerdzap.parse_cards(page)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["endAt"], "2026-12-16T23:59:59.000Z")
        self.assertEqual(cards[0]["platform"], "GOG")

    def test_synthetic_arrow_entity_platform(self):
        """&rarr; 实体 + 大小写混合的平台文案。"""
        page = """
        <article class="fg-card">
          <h3 class="fg-title">Epic Only</h3>
          <span class="fg-store">Prime Gaming &rarr; EPIC Games Store</span>
          <span class="fg-time" data-start="2026-11-01T18:00:00Z"
                data-end="2026-12-01T18:00:00Z" data-status="soon">Ends 1 Dec</span>
        </article>"""
        cards = nerdzap.parse_cards(page)
        self.assertEqual(cards[0]["platform"], "Epic Games Store")
        self.assertEqual(cards[0]["status"], "soon")
        self.assertEqual(cards[0]["endAt"], "2026-12-01T18:00:00.000Z")

    def test_empty_page(self):
        self.assertEqual(nerdzap.parse_cards(""), [])
        self.assertEqual(nerdzap.parse_cards(None), [])


# --------------------------------------------------------------------------- #
# fetch_free：标题清洗 / 归一 / 合并
# --------------------------------------------------------------------------- #

class TestCleanTitle(unittest.TestCase):

    def test_strips_store_giveaway_suffix(self):
        cases = [
            ("GMH - Good Morning Human (Epic Games) Giveaways", "GMH - Good Morning Human"),
            ("Fall Guys (Epic Games) Giveaway", "Fall Guys"),
            ("Some Game (PC) Free", "Some Game"),
            ("X (GOG) 100% off", "X"),
        ]
        for src, want in cases:
            self.assertEqual(fetch_free.clean_title(src), want, src)

    def test_keeps_mid_parentheses(self):
        """游戏名自带括号（不在尾部）不动。"""
        self.assertEqual(fetch_free.clean_title("Hue (Original Soundtrack)"),
                         "Hue (Original Soundtrack)")

    def test_never_returns_empty(self):
        self.assertEqual(fetch_free.clean_title("(Epic Games) Giveaway"),
                         "(Epic Games) Giveaway")

    def test_norm_title_plural(self):
        # 复数 giveaways 也要剥干净，才能与官方/干净源归一到一起
        self.assertEqual(
            fetch_free.norm_title("GMH - Good Morning Human (Epic Games) Giveaways"),
            "gmhgoodmorninghuman")
        self.assertEqual(fetch_free.norm_title("Good Morning Human"),
                         "goodmorninghuman")


class TestMergeSection(unittest.TestCase):

    def _merge(self, official, cross):
        now = datetime.now(timezone.utc)
        return fetch_free.merge_section([official], [cross], {"epic-api"},
                                        now.timestamp() * 1000)

    def test_acronym_prefix_titles_merge(self):
        future = "2099-12-31T18:00:00.000Z"
        base = {"titleZh": "", "platform": "Epic Games Store", "requiresPrime": False,
                "giveaway": "epic", "price": 0, "startAt": "2099-12-01T18:00:00.000Z",
                "endAt": future, "platforms": ["pc"]}
        official = dict(base, id="epicfree-gmh", title="Good Morning Human",
                        sources=["epic-api"])
        gp = dict(base, id="gp-gmh",
                  title="GMH - Good Morning Human (Epic Games) Giveaways",
                  sources=["gamerpower"])
        out = self._merge(official, gp)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Good Morning Human")  # 官方标题优先
        self.assertEqual(sorted(out[0]["sources"]), ["epic-api", "gamerpower"])
        self.assertEqual(out[0]["confidence"], "high")

    def test_prefix_rule_merges_short_pairs(self):
        """前缀规则（≥4 字符）依旧生效：Doom / Doom Eternal 视为同款。"""
        future = "2099-12-31T18:00:00.000Z"
        base = {"titleZh": "", "platform": "GOG", "requiresPrime": False,
                "giveaway": "gog", "price": 0, "platforms": ["pc"],
                "startAt": "2099-12-01T00:00:00.000Z", "endAt": future}
        a = dict(base, id="a", title="Doom", sources=["gp"])
        b = dict(base, id="b", title="Doom Eternal", sources=["gp"])
        out = self._merge(a, b)
        self.assertEqual(len(out), 1)

    def test_containment_threshold_short_keys_do_not_merge(self):
        """包含匹配阈值 12 字符：被包含方过短（"Doom" ⊂ "GMH Doom"）不合，
        否则任何带缩写前缀的短名都会被吞进长名里。"""
        future = "2099-12-31T18:00:00.000Z"
        base = {"titleZh": "", "platform": "GOG", "requiresPrime": False,
                "giveaway": "gog", "price": 0, "platforms": ["pc"],
                "startAt": "2099-12-01T00:00:00.000Z", "endAt": future}
        a = dict(base, id="a", title="GMH Doom", sources=["gp"])
        b = dict(base, id="b", title="Doom", sources=["gp"])
        out = self._merge(a, b)
        self.assertEqual(len(out), 2)


# --------------------------------------------------------------------------- #
# fetch_prime_preview：公告解析 / 预告合并
# --------------------------------------------------------------------------- #

ARTICLE_HTML = """
<html><body><article>
  <h1>Amazon Prime Gaming November 2026 Lineup Features 7 Games</h1>
  <p>Amazon has announced the lineup of games to be added to Prime Gaming
     for the month of November, 2026.</p>
  <p>November 5: WRC Generations [Epic Games Store]
     Squirrel with a Gun [Amazon Games App]</p>
  <p>November 12: Jurassic World Evolution 2 [Epic Games Store]
     Radiolight [Epic Games Store]</p>
  <p>November 19: Not A Real Platform [Dreamcast 2]</p>
</article></body></html>
"""


class TestParseArticle(unittest.TestCase):

    def _parse(self):
        return fetch_prime_preview.parse_article(
            "https://twistedvoxel.com/amazon-prime-gaming-november-2026-wrc-generations/")

    def test_lineup_items(self):
        items, year = self._parse()
        self.assertEqual(year, 2026)
        titles = [x["title"] for x in items]
        self.assertIn("WRC Generations", titles)
        self.assertIn("Squirrel with a Gun", titles)
        self.assertIn("Jurassic World Evolution 2", titles)
        self.assertIn("Radiolight", titles)
        # 未知平台被过滤
        self.assertNotIn("Not A Real Platform", titles)

    def test_wave_start_time(self):
        items = {x["title"]: x for x in self._parse()[0]}
        self.assertEqual(items["WRC Generations"]["startAt"],
                         "2026-11-05T18:00:00.000Z")
        self.assertEqual(items["Jurassic World Evolution 2"]["startAt"],
                         "2026-11-12T18:00:00.000Z")


class TestPreviewMerge(unittest.TestCase):

    def test_merge_estimates_end(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        announcement = [{"title": "Future Game", "platform": "Amazon Games App",
                         "startAt": "2026-11-20T18:00:00.000Z",
                         "sourceUrl": "https://x/y"}]
        out = fetch_prime_preview.merge(announcement, [], [], now)
        self.assertEqual(len(out), 1)
        it = out[0]
        self.assertEqual(it["id"], "preview-future-game")
        self.assertTrue(it["estimated"])
        self.assertEqual(it["confidence"], "low")
        self.assertEqual(it["sources"], ["announcement"])
        # 波次 +34 天估算
        self.assertEqual(it["endAt"][:10], "2026-12-24")

    def test_two_sources_confidence_high(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        a = [{"title": "Soon Game", "platform": "Amazon Games App",
              "startAt": "2026-11-20T18:00:00.000Z", "sourceUrl": "u"}]
        b = [{"title": "Soon Game", "platform": "Amazon Games App",
              "startAt": "2026-11-20T18:00:00.000Z",
              "endAt": "2026-12-24T18:00:00.000Z", "cover": "c.jpg",
              "sourceUrl": "u"}]
        out = fetch_prime_preview.merge(a, b, [], now)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["confidence"], "high")
        self.assertEqual(sorted(out[0]["sources"]), ["announcement", "nerdzap"])
        self.assertNotIn("estimated", out[0])  # 聚合源有精确 endAt → 无需估算标

    def test_expired_dropped(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        old = [{"title": "Last Month Game", "platform": "GOG",
                "startAt": "2026-10-01T18:00:00.000Z",
                "endAt": "2026-11-01T18:00:00.000Z", "sourceUrl": "u"}]
        self.assertEqual(fetch_prime_preview.merge(old, [], [], now), [])


class TestDropLive(unittest.TestCase):

    def test_prefix_fuzzy_dedup(self):
        now_iso = "2026-11-09T12:00:00.000Z"
        items = [
            {"id": "preview-wrc", "title": "WRC Generations",
             "startAt": "2026-11-05T18:00:00.000Z", "endAt": "2099-01-01T00:00:00.000Z",
             "platform": "Epic Games Store", "sources": ["announcement"],
             "confidence": "low", "titleZh": "", "requiresPrime": True,
             "price": 0, "preview": True, "claimUrl": ""},
            {"id": "preview-future", "title": "Far Future Game",
             "startAt": "2026-11-20T18:00:00.000Z", "endAt": "2099-01-01T00:00:00.000Z",
             "platform": "Amazon Games App", "sources": ["announcement"],
             "confidence": "low", "titleZh": "", "requiresPrime": True,
             "price": 0, "preview": True, "claimUrl": ""},
        ]
        # 临时 games.json：写进 data 目录代价太大 → 直接monkeypatch文件读取
        games_payload = {"list": [
            {"title": "WRC Generations - The FIA WRC Official Game"},
            {"title": "Squirrel with a Gun"},
        ]}
        orig = fetch_prime_preview.GAMES_FILE
        try:
            import tempfile
            tmp = Path(tempfile.mkdtemp()) / "games.json"
            tmp.write_text(json.dumps(games_payload), encoding="utf-8")
            fetch_prime_preview.GAMES_FILE = tmp
            kept, dropped = fetch_prime_preview.drop_live(items, now_iso)
        finally:
            fetch_prime_preview.GAMES_FILE = orig
        self.assertEqual([x["title"] for x in kept], ["Far Future Game"])
        self.assertEqual([x["title"] for x in dropped], ["WRC Generations"])


# --------------------------------------------------------------------------- #
# push_cloud 入库归一
# --------------------------------------------------------------------------- #

class TestPushCloudNormalize(unittest.TestCase):

    def test_missing_start_clamped(self):
        out = push_cloud.normalize({
            "id": "a", "title": "T", "platform": "GOG", "requiresPrime": True,
            "price": 0, "endAt": "2099-06-01T18:00:00Z",
        })
        self.assertIsNotNone(out)
        # startAt 被兜成 min(end-30d, now)：绝不能落到未来（防误判「待开始」）
        end_ms = push_cloud.parse_ms("2099-06-01T18:00:00Z")
        self.assertLessEqual(push_cloud.parse_ms(out["startAt"]),
                             min(end_ms - 30 * 86400000,
                                 int(datetime.now(timezone.utc).timestamp() * 1000)))

    def test_future_start_kept(self):
        out = push_cloud.normalize({
            "id": "p", "title": "P", "platform": "Amazon Games App",
            "requiresPrime": True, "price": 0,
            "startAt": "2099-05-01T18:00:00Z", "endAt": "2099-06-01T18:00:00Z",
            "preview": True, "estimated": True, "confidence": "low",
        })
        self.assertIsNotNone(out)
        self.assertEqual(out["startAt"][:10], "2099-05-01")  # 未来排期保留
        self.assertTrue(out["preview"])
        self.assertTrue(out["estimated"])
        self.assertEqual(out["confidence"], "low")

    def test_invalid_dropped(self):
        self.assertIsNone(push_cloud.normalize({"id": "", "endAt": "2099-01-01"}))
        self.assertIsNone(push_cloud.normalize({"id": "x", "endAt": "not-a-date"}))


if __name__ == "__main__":
    unittest.main()
