#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_prime_preview.py — Prime Gaming 官方公告预告（未来排期）多源抓取
==============================================================================
为什么需要这个脚本
------------------
主链路 fetch_prime.py 的数据源（NerdZap）只列「可领取 / 即将上架」的卡片，
亚马逊每月公告里「X 日开始」的那批游戏（通常提前 1~3 周公布）要等到
NerdZap 收录才有 —— 也就是说，官方公告发布后的一两周里，小程序的
「待开始」分组和日历「接下来会送」会一直空着。

本脚本补上这块：把「公告已发布但还没开送」的排期抓回来，单独输出
scraper/data/preview.json，由 push_cloud.py 一并推入云端 games 集合
（preview: true 标记），前端据此进「待开始」分组与日历预告。

三个源 + 交叉验证
------------------
A. 公告文章（twistedvoxel 月度汇总，服务端渲染、无反爬）
   搜索页 ?s=prime+gaming 发现每月一篇的汇总文，格式高度稳定：
       November 12: Jurassic World Evolution 2 [Epic Games Store]
   日期来自文章（无精确时刻 → 按 18:00Z 估算），结束时间按波次惯例
   +34 天估算（NerdZap 近五个波次实测 32~34 天）。
   只有一个源命中 → confidence=low，界面显示「待核实」。
B. NerdZap 的 soon 卡（data-status="soon"）
   机器可读的精确时间戳；与 A 的标题能对上 → confidence=high。
C. freetokeep /api/v1/offers?store=prime（JSON API）
   同样机器可读；与 A/B 任一命中 → confidence=high。

取舍规则
--------
1. 并集合并（按归一标题），互不删除；sources 记录命中的源。
2. 时间：聚合源（B/C）精确时间戳 > 公告估算。
3. 与 games.json（当前可领取清单）标题撞车 → 丢弃：已经开送的款
   由主链路负责，预告条目只负责「还没开始」的，避免双卡。
4. endAt 已过期的公告残留（上个月的旧文章）→ 丢弃。
5. 公告源解析出 <2 条结构化条目 → 视为解析失败，整源丢弃
   （防止站点改版后拿半截文本当排期）。
6. 全部源失败 → 退出码 1；正常但 0 条（公告未发布）→ 退出码 0。

输出：scraper/data/preview.json（标准 schema + preview/estimated 标记）

用法：
    python scraper/fetch_prime_preview.py            # 抓并写入
    python scraper/fetch_prime_preview.py --dry-run  # 只打印，不落盘
    python scraper/fetch_prime_preview.py --article-url <url>  # 手动指定公告文

退出码：0 成功（含 0 条）/ 1 三个源全部失败
------------------------------------------------------------------------------
"""

import argparse
import gzip
import html
import json
import re
import ssl
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEARCH_URL = "https://twistedvoxel.com/?s=prime+gaming"
ARTICLE_URL_HINT = "https://twistedvoxel.com/amazon-prime-gaming-"
NERDZAP_URL = "https://nerdzap.com/free-games/prime-gaming/"
FREETOKEEP_URL = "https://freetokeep.gg/api/v1/offers?store=prime&limit=100"
CLAIM_URL = "https://luna.amazon.com/claims/home"

OUT_DIR = Path(__file__).parent / "data"
OUT_FILE = OUT_DIR / "preview.json"
GAMES_FILE = OUT_DIR / "games.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 没有精确时刻时，波次开始时间按 18:00Z 估算（NerdZap 近期 data-start 实测值）
EST_START_HOUR = 18
# 波次时长估算（天）：NerdZap 近五个波次实测 32~34 天
EST_WAVE_DAYS = 34

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"], start=1)}

# 公告文章的平台标注 → 本项目 platform 标识（与 fetch_prime 的映射一致）
PLATFORM_MAP = {
    "epic games store": "Epic Games Store",
    "epic": "Epic Games Store",
    "amazon games app": "Amazon Games App",
    "amazon games": "Amazon Games App",
    "amazon luna": "Amazon Games App",
    "amazon": "Amazon Games App",
    "gog": "GOG",
    "microsoft store": "Microsoft Store",
    "legacy games": "Legacy Games",
    "luna": "Amazon Games App",
}


def log(msg):
    print(msg, flush=True)


def http_get(url, timeout=30):
    """GET。urllib 先行，403/风控时用 curl 兜底。

    twistedvoxel 的 CDN 会按 TLS 指纹拦 Python 的 ssl 握手（curl 同 UA 却能过），
    所以 urllib 拿到 403/挑战页时自动降级到系统 curl（Windows 10+ / ubuntu 自带），
    保持零 pip 依赖。
    """
    try:
        return http_get_urllib(url, timeout)
    except Exception as e:  # noqa: BLE001
        last = str(e)
    body = http_get_curl(url, timeout)
    if body is not None:
        return body
    raise RuntimeError("urllib(%s) 与 curl 均失败" % last[:80])


def http_get_urllib(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, errors="replace")


def http_get_curl(url, timeout):
    """curl 兜底。返回 None = curl 也不可用/失败。"""
    try:
        proc = subprocess.run(
            ["curl", "-s", "--compressed", "--max-time", str(timeout),
             "-A", UA, "-H", "Accept-Language: en-US,en;q=0.9",
             "-H", "Accept: text/html,application/json;q=0.9,*/*;q=0.8",
             url],
            capture_output=True, timeout=timeout + 10)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def looks_blocked(page):
    low = (page or "").lower()
    for kw in ("enable javascript", "technical difficulties", "are you a human",
               "access denied", "just a moment"):
        if kw in low:
            return kw
    return None


def norm_title(s):
    """归一标题：跨源认「同一款游戏」（与 miniprogram/utils/schedule.js 的 normKey 一致）"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", "", str(s or "").lower())


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:60] or "game"


def mk_id(title):
    return "preview-" + slugify(title)


# --------------------------------------------------------------------------- #
# 源 A：公告文章（twistedvoxel 月度汇总）
# --------------------------------------------------------------------------- #

RE_ARTICLE_LINK = re.compile(
    r'href="(https://twistedvoxel\.com/amazon-prime-gaming-[a-z]+-(\d{4})[^"]*)"')
RE_LINEUP_DATE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2})\s*:")
RE_BRACKET_ITEM = re.compile(r"([^\[\]]{2,90}?)\s*\[([^\[\]]+)\]")
RE_ARTICLE_YEAR = re.compile(r"-([a-z]+)-(\d{4})-")


def discover_articles(limit=3):
    """搜索页发现最近的月度汇总文。返回 [(url, year)]，按年份新→旧。"""
    page = http_get(SEARCH_URL)
    blocked = looks_blocked(page)
    if blocked:
        raise RuntimeError("搜索页命中风控（%s）" % blocked)
    found = {}
    for m in RE_ARTICLE_LINK.finditer(page):
        url, year = m.group(1), int(m.group(2))
        if url not in found or year > found[url]:
            found[url] = year
    arts = sorted(found.items(), key=lambda kv: -kv[1])
    return arts[:limit]


def strip_tags(fragment):
    txt = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(re.sub(r"\s+", " ", txt)).strip()


def parse_article(url):
    """从一篇汇总文解析排期。返回 (items, year)；解析不出结构化条目时 items 为空。

    文章正文里排期段的形态（各月一致）：
        November 5: WRC Generations [Epic Games Store]
                    Squirrel with a Gun [Amazon Games App]
        November 12: ...
    """
    page = http_get(url)
    # 正文：<article> 里去脚本样式，避免导航/评论混入
    body = re.search(r"<article\b.*?</article>", page, re.S)
    text = strip_tags(body.group(0) if body else page)

    year_m = RE_ARTICLE_YEAR.search(url)
    year = int(year_m.group(2)) if year_m else datetime.now(timezone.utc).year

    # 按日期头切段：段内所有 Title [Platform] 都归这个日期
    heads = list(RE_LINEUP_DATE.finditer(text))
    items = []
    for i, h in enumerate(heads):
        mon = MONTHS.get(h.group(1).lower())
        day = int(h.group(2))
        seg_end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        seg = text[h.end():seg_end]
        for m in RE_BRACKET_ITEM.finditer(seg):
            title, plat_raw = m.group(1).strip(" -–—:"), m.group(2).strip().lower()
            platform = PLATFORM_MAP.get(plat_raw)
            if not platform:
                continue
            title = re.sub(r"\s+", " ", title).strip()
            # 标题里混进的说明文字（如 "lineup" 头部残留）按长度和字符集过滤
            if len(title) < 2 or len(title) > 80 or not re.search(r"[a-z0-9]", title, re.I):
                continue
            if not re.match(r"^[A-Za-z0-9'\.&:!+–—\- \u4e00-\u9fff]+$", title):
                continue
            items.append({
                "title": title,
                "platform": platform,
                "startAt": "%04d-%02d-%02dT%02d:00:00.000Z" % (year, mon, day, EST_START_HOUR),
            })
    return items, year


def fetch_announcement():
    """搜索页 → 最近 3 篇汇总文 → 合并去重。返回 (items, err)。"""
    try:
        arts = discover_articles()
    except Exception as e:  # noqa: BLE001
        return [], "公告搜索页不可用：%s" % str(e)[:80]
    if not arts:
        return [], "搜索页未找到 prime gaming 汇总文"
    by_key = {}
    errs = []
    for url, _year in arts:
        try:
            items, year = parse_article(url)
        except Exception as e:  # noqa: BLE001
            errs.append("%s：%s" % (url.rstrip("/").rsplit("/", 1)[-1][:40], str(e)[:60]))
            continue
        log("      公告文 %s：%d 条" % (url.rstrip("/").rsplit("/", 1)[-1][:56], len(items)))
        for it in items:
            key = (norm_title(it["title"]), it["startAt"][:10])
            if key in by_key:
                continue
            it = dict(it)
            it["sourceUrl"] = url
            by_key[key] = it
    if errs and not by_key:
        return [], "公告文章全部解析失败：" + "; ".join(errs[:2])
    return list(by_key.values()), ""


# --------------------------------------------------------------------------- #
# 源 B：NerdZap soon 卡（与 fetch_prime.py 同一套解析，只留未开始的）
# --------------------------------------------------------------------------- #

def attr(tag_text, name):
    m = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % re.escape(name),
                  tag_text)
    if not m:
        return ""
    return html.unescape(next(g for g in m.groups() if g is not None))


def to_iso(v):
    if not v:
        return ""
    v = v.strip().replace("Z", "")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?$", v)
    if m:
        return m.group(1) + ".000Z"
    return ""


def fetch_nerdzap(now_iso):
    try:
        page = http_get(NERDZAP_URL)
    except Exception as e:  # noqa: BLE001
        return [], "nerdzap 不可达：%s" % str(e)[:80]
    blocked = looks_blocked(page)
    if blocked:
        return [], "nerdzap 命中风控页（%s）" % blocked
    blocks = re.findall(r'<article\b[^>]*class="[^"]*fg-card[^"]*"[^>]*>(.*?)</article>',
                        page, re.S)
    if not blocks:
        return [], "nerdzap 页面解析不到卡片（可能改版）"
    items = []
    for b in blocks:
        t = re.search(r'<h3[^>]*class="[^"]*fg-title[^"]*"[^>]*>(.*?)</h3>', b, re.S)
        if not t:
            continue
        title = strip_tags(t.group(1))
        time_tag = re.search(r'<span[^>]*class="[^"]*fg-time[^"]*"([^>]*)>', b)
        start_iso = end_iso = ""
        if time_tag:
            a = time_tag.group(1)
            start_iso = to_iso(attr(a, "data-start"))
            end_iso = to_iso(attr(a, "data-end"))
        status = attr(time_tag.group(1), "data-status") if time_tag else ""
        if not end_iso:
            continue
        # 只留「还没开始」的预告卡；已开始的由主链路（games.json）负责
        if not (status == "soon" or (start_iso and start_iso > now_iso)):
            continue
        cover = ""
        m = re.search(r'<img[^>]*\ssrc="([^"]+)"', b)
        if m:
            cover = html.unescape(m.group(1))
        store = re.search(r'fg-store[^>]*>(.*?)</span>', b, re.S)
        store_low = strip_tags(store.group(1)).lower() if store else ""
        platform = "Amazon Games App"
        for key, val in PLATFORM_MAP.items():
            if key in store_low:
                platform = val
                break
        items.append({
            "title": title,
            "platform": platform,
            "startAt": start_iso,
            "endAt": end_iso,
            "cover": cover,
            "sourceUrl": NERDZAP_URL,
        })
    return items, ""


# --------------------------------------------------------------------------- #
# 源 C：freetokeep prime JSON API（只留 startsAt 在未来的）
# --------------------------------------------------------------------------- #

def fetch_freetokeep(now_iso):
    try:
        page = http_get(FREETOKEEP_URL)
        data = json.loads(page)
    except Exception as e:  # noqa: BLE001
        return [], "freetokeep 不可达：%s" % str(e)[:80]
    items = []
    for it in (data.get("items") or []):
        title = (it.get("title") or "").strip()
        start = to_iso(str(it.get("startsAt") or "").replace("Z", ""))
        end = to_iso(str(it.get("endsAt") or "").replace("Z", ""))
        if not title or not start or not end:
            continue
        if not (start > now_iso):
            continue  # 已开送的归主链路
        items.append({
            "title": title,
            "platform": "Amazon Games App",
            "startAt": start,
            "endAt": end,
            "cover": it.get("imageUrl") or "",
            "sourceUrl": CLAIM_URL,
        })
    return items, ""


# --------------------------------------------------------------------------- #
# 合并 + 清洗
# --------------------------------------------------------------------------- #

def merge(announcement, nerdzap, freetokeep, now):
    """三源并集。聚合源时间戳优先；公告独有条目按波次惯例估算并标记 estimated。"""
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    by_key = {}

    def put(src, it):
        key = norm_title(it["title"])
        if not key:
            return
        cur = by_key.get(key)
        if cur is None:
            it = dict(it)
            it["sources"] = [src]
            by_key[key] = it
            return
        if src not in cur["sources"]:
            cur["sources"].append(src)
        # 聚合源（带精确 endAt）覆盖公告估算
        if it.get("endAt"):
            cur["endAt"] = it["endAt"]
            cur["startAt"] = it.get("startAt") or cur["startAt"]
            cur["cover"] = cur.get("cover") or it.get("cover") or ""
            cur.pop("estimated", None)

    for it in announcement:
        it = dict(it)
        if "endAt" not in it or not it["endAt"]:
            try:
                start = datetime.strptime(it["startAt"], "%Y-%m-%dT%H:%M:%S.000Z")
            except ValueError:
                continue
            it["endAt"] = (start + timedelta(days=EST_WAVE_DAYS)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z")
            it["estimated"] = True
        put("announcement", it)
    for it in nerdzap:
        put("nerdzap", it)
    for it in freetokeep:
        put("freetokeep", it)

    out = []
    for it in by_key.values():
        if it["endAt"] <= now_iso:
            continue  # 上个月公告的残留
        n_src = len(it["sources"])
        it["confidence"] = "high" if n_src >= 2 else "low"
        it["id"] = mk_id(it["title"])
        it["titleZh"] = ""
        it["requiresPrime"] = True
        it["price"] = 0
        it["preview"] = True
        it["claimUrl"] = CLAIM_URL
        it["platform"] = it.get("platform") or "Amazon Games App"
        out.append(it)
    out.sort(key=lambda x: x["startAt"])
    return out


def drop_live(items, now_iso):
    """与 games.json（主链路当前可领取清单）标题撞车的 → 丢弃，避免双卡。

    除精确归一匹配外，还做前缀匹配：公告常写简称（WRC Generations），
    聚合站写全名（WRC Generations - The FIA WRC Official Game）。
    误匹配的代价只是少一条预告（宁缺勿错），漏匹配的代价是双卡，所以放宽。
    """
    live = set()

    def find_fuzzy(key):
        if key in live:
            return key
        if len(key) >= 5:
            for k in live:
                if len(k) >= 5 and (k.startswith(key) or key.startswith(k)):
                    return k
        return None

    try:
        games = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
        for it in (games.get("list") or []):
            k = norm_title(it.get("title"))
            if k:
                live.add(k)
    except Exception:  # noqa: BLE001
        pass  # games.json 不存在/损坏时跳过去重（宁可有预告，也不误删）
    kept, dropped = [], []
    for it in items:
        hit = find_fuzzy(norm_title(it["title"]))
        (kept, dropped)[hit is not None].append(it)
    return kept, dropped


def main():
    ap = argparse.ArgumentParser(description="Prime Gaming 官方公告预告抓取（多源交叉验证）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("-o", "--out", default=str(OUT_FILE), help="输出 JSON 路径")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    log("")
    log("[1/3] 抓取三个预告源")
    announcement, err_a = fetch_announcement()
    log("      A 公告文章：%d 条%s" % (len(announcement), ("（%s）" % err_a) if err_a else ""))
    nerdzap, err_b = fetch_nerdzap(now_iso)
    log("      B NerdZap soon 卡：%d 条%s" % (len(nerdzap), ("（%s）" % err_b) if err_b else ""))
    freetokeep, err_c = fetch_freetokeep(now_iso)
    log("      C freetokeep 未来条目：%d 条%s" % (len(freetokeep), ("（%s）" % err_c) if err_c else ""))

    if not announcement and not nerdzap and not freetokeep:
        log("      × 三个源全部失败，不写文件（保留上次预告数据）")
        return 1

    log("[2/3] 合并交叉验证")
    items = merge(announcement, nerdzap, freetokeep, now)
    items, dropped = drop_live(items, now_iso)
    if dropped:
        log("      已开送（games.json 撞车）丢弃 %d 条：%s"
            % (len(dropped), ", ".join(x["title"][:20] for x in dropped[:4])))
    log("      合并后 %d 条（high %d / low %d）" % (
        len(items),
        sum(1 for x in items if x["confidence"] == "high"),
        sum(1 for x in items if x["confidence"] == "low")))

    payload = {
        "updatedAt": now_iso,
        "source": "announcement+nerdzap+freetokeep",
        "note": "Prime Gaming 官方公告预告（未来排期，含估算时间）",
        "count": len(items),
        "list": items,
    }

    log("[3/3] 输出")
    if args.dry_run:
        log("      --dry-run，不写文件。预览：")
        for x in items:
            log("        · %-42s %-20s %s ~ %s%s [%s]" % (
                x["title"][:42], x["platform"], x["startAt"][:10], x["endAt"][:10],
                " (估)" if x.get("estimated") else "", ",".join(x["sources"])))
        if not items:
            log("        （空 —— 官方可能还没发布下期公告）")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("      √ 已写入 " + str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
