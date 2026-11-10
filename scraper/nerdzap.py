#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nerdzap.py — NerdZap 聚合站卡片解析（fetch_prime / fetch_prime_preview 共用）
==============================================================================
两个脚本都从 NerdZap 取数，卡片 HTML 结构完全一致，解析逻辑抽到这里，
源站改版时只改一处。仍只用标准库，保持抓取链零 pip 依赖。

卡片结构（服务端渲染，字段带机器可读时间戳）：
    <article class="fg-card" data-id="prime-018d937f-...">
      <a class="fg-img" href="..."><img src="..."/></a>
      <h3 class="fg-title">Caravan Sandwitch</h3>
      <span class="fg-store">Prime Gaming &rarr; Amazon Games / Luna</span>
      <span class="fg-time" data-start="2026-10-15T17:00:00.000Z"
            data-end="2026-12-16T18:00:00.000Z" data-status="now">Ends 16 Dec</span>
      <div class="fg-reviews">...评价文案...</div>

parse_cards() 返回的每个条目：
    { title, platform, startAt, endAt, status, cover, dataId, claim, reviews }
    没有对应字段的为空串；endAt 缺失的条目**不丢弃**（由调用方按用途决定，
    主链路丢弃、预告链路要留 soon 卡——历史教训：以前各自过滤，行为悄悄分叉）。
"""

import html
import re
from datetime import datetime, timezone

# fg-store 文案 → 本项目 platform 标识（顺序敏感：更具体的写在前面前面也行，
# 这里按「最常见先命中」排；箭头右侧优先，见 map_store_platform）
STORE_PLATFORM_MAP = [
    ("amazon games", "Amazon Games App"),
    ("luna", "Amazon Games App"),
    ("prime gaming", "Amazon Games App"),
    ("epic", "Epic Games Store"),
    ("gog", "GOG"),
    ("microsoft", "Microsoft Store"),
    ("xbox", "Microsoft Store"),
    ("legacy", "Legacy Games"),
]

# 兜底领取页（卡片没带链接时用）
DEFAULT_CLAIM_URL = "https://luna.amazon.com/claims/home"


def strip_tags(fragment):
    txt = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(re.sub(r"\s+", " ", txt)).strip()


def attr(tag_text, name):
    """从一段标签文本里取属性值。"""
    m = re.search(
        r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % re.escape(name),
        tag_text,
    )
    if not m:
        return ""
    return html.unescape(next(g for g in m.groups() if g is not None))


def to_iso(v):
    """data-start / data-end 的 ISO 串规范成带 Z 的形式；解析失败返回 ''。"""
    if not v:
        return ""
    v = v.strip().replace("Z", "")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?$", v)
    if m:
        return m.group(1) + ".000Z"
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return ""


def map_store_platform(store_text):
    """'Prime Gaming → Epic Games Store' 这类文案 → 平台标识（取箭头右侧）。"""
    low = (store_text or "").lower()
    for arrow in ("→", "&rarr;", "->", "–", "—"):
        if arrow in low:
            low = low.split(arrow, 1)[1].strip()
            break
    for key, val in STORE_PLATFORM_MAP:
        if key in low:
            return val
    return "Amazon Games App"


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_human_date(text):
    """兜底：从 'Ends 16 Dec 2026' 这类文案里解析日期（无 data-end 属性时用）。"""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(20\d{2})", text or "")
    if not m:
        return ""
    mon = MONTHS.get(m.group(2).lower()[:3])
    if not mon:
        return ""
    try:
        dt = datetime(int(m.group(3)), mon, int(m.group(1)), 23, 59, 59,
                      tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return ""


def parse_cards(page):
    """整页 HTML → 卡片条目列表（见模块 docstring 的字段说明）。"""
    if not page:
        return []
    blocks = re.findall(
        r'<article\b[^>]*class="[^"]*fg-card[^"]*"[^>]*>(.*?)</article>', page, re.S)
    if not blocks:
        blocks = [b for b in re.findall(r"<article\b[^>]*>(.*?)</article>", page, re.S)
                  if "fg-title" in b]

    out = []
    for b in blocks:
        t = re.search(r'<h3[^>]*class="[^"]*fg-title[^"]*"[^>]*>(.*?)</h3>', b, re.S)
        if not t:
            t = re.search(r"<h[23][^>]*>(.*?)</h[23]>", b, re.S)
        title = strip_tags(t.group(1)) if t else ""
        if not title:
            continue

        store_raw = ""
        sm = re.search(r'<span[^>]*class="[^"]*fg-store[^"]*"[^>]*>(.*?)</span>', b, re.S)
        if sm:
            store_raw = strip_tags(sm.group(1))

        start_iso = end_iso = status = ""
        time_text = ""
        tm = re.search(r'<span[^>]*class="[^"]*fg-time[^"]*"([^>]*)>(.*?)</span>', b, re.S)
        if tm:
            a = tm.group(1)
            start_iso = to_iso(attr(a, "data-start"))
            end_iso = to_iso(attr(a, "data-end"))
            status = attr(a, "data-status")
            time_text = strip_tags(tm.group(2))
        if not end_iso:
            # 兜底：卡片没带 data-end 属性时，从 'Ends 16 Dec 2026' 文案里猜
            end_iso = parse_human_date(time_text)

        cover = ""
        cm = re.search(r'<img[^>]*\ssrc="([^"]+)"', b)
        if cm:
            cover = html.unescape(cm.group(1))

        data_id = ""
        dm = re.search(r'data-id="([^"]+)"', b)
        if dm:
            data_id = dm.group(1)

        claim = DEFAULT_CLAIM_URL
        lm = re.search(r'<a\b[^>]*class="[^"]*fg-img[^"]*"[^>]*href="([^"]+)"', b)
        if not lm:
            lm = re.search(
                r'<a\b[^>]*href="(https?://[^"]*(?:luna\.amazon|amazon\.com)[^"]*)"', b)
        if lm:
            claim = html.unescape(lm.group(1))

        reviews = ""
        rm = re.search(r'class="fg-reviews"[^>]*>(.*?)</div>', b, re.S)
        if rm:
            reviews = strip_tags(rm.group(1))[:40]

        out.append({
            "title": title,
            "platform": map_store_platform(store_raw),
            "startAt": start_iso,
            "endAt": end_iso,
            "status": status or "now",
            "cover": cover,
            "dataId": data_id,
            "claim": claim,
            "reviews": reviews,
        })
    return out
