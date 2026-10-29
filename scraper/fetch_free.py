#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_free.py — 免会员直领限免多源抓取 + 交叉验证（Epic / GOG / Steam）
==============================================================================

零依赖（urllib + json + re），与 fetch_prime.py 同风格。

数据源
------
Epic   主源  Epic 官方 API
             https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions
             请求两次：locale=zh-CN&country=CN（拿中文标题）
                       locale=en-US&country=US（拿美元原价）
       交叉  https://freetokeep.gg/api/v1/offers?store=epic   （免 key JSON API）
             https://www.gamerpower.com/api/giveaways?platform=epic-games-store
GOG    主源  https://www.gog.com/en/giveaways （404 = 当前无活动，空态属正常）
       交叉  freetokeep ?store=gog、GamerPower ?platform=gog
Steam  主源  https://store.steampowered.com/api/featuredcategories（specials 里
             discount_percent==100 且 original_price>0 = Free to keep）
       交叉  freetokeep ?store=steam
             注意：Steam 域名在部分地区网络不可达，失败时整体不中断，
             该源标记 unreachable，生成器会保留上一次的数据。

冲突取舍规则（数据源打架时听谁的）
----------------------------------
1. 并集：所有源按「归一标题」合并，互不删除（宁多勿漏）。
2. 时间：官方源（Epic API / Steam API / GOG 页面）的 startAt/endAt 覆盖聚合源。
3. 标题：取官方源；titleZh 仅当官方源返回含中文（CJK）才写入，否则留空交给 meta.js。
4. 价格：优先官方美元价（美分/100）；没有就不填，不做汇率换算。
5. 溯源：sources 记录命中的源名；含官方源 → confidence=high，仅聚合源 → low。
6. 聚合源独有条目：保留，confidence=low（界面显示「来源待核实」）。
7. 同款多端重复（freetokeep 的 epic_pc/ios/android）：按归一标题去重，保留 endAt 最晚。
8. 空结果保护：某源这次返回 0 条但上次非空 → 输出标记 empty，由 gen-free.js
   决定是否写空文件；unreachable 的源保留旧数据。
9. 时间统一输出 UTC ISO（.000Z），展示由前端按东八区换算。
10. 单源失败不中断：记 status / error，人话打印，整体退出码 0（全部失败才 1）。

输出
----
scraper/data/free.json
{
  "updatedAt": "...",
  "status": {"epic": "ok", "gog": "empty", "steam": "unreachable"},
  "epic":  [ ... 条目 ... ],
  "gog":   [],
  "steam": []
}

条目字段与 miniprogram/utils/epicfree.js 同构（交给 tools/gen-free.js 生成）。
用法：
  python scraper/fetch_free.py            # 全部源
  python scraper/fetch_free.py --only epic
  python scraper/fetch_free.py --dry-run  # 只打印，不写文件
"""

import argparse
import json
import re
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

OUT_DIR = Path(__file__).parent / "data"
OUT_FILE = OUT_DIR / "free.json"
PREV_FILE = OUT_DIR / "free.json"          # 空结果保护参照（上次输出）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 20
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

EPIC_API = ("https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
            "?locale={loc}&country={cc}&allowCountries={cc}")
FREETOKEEP = "https://freetokeep.gg/api/v1/offers?store={store}&limit=200"
GAMERPOWER = "https://www.gamerpower.com/api/giveaways?platform={plat}"
GOG_PAGE = "https://www.gog.com/en/giveaways"
STEAM_API = "https://store.steampowered.com/api/featuredcategories/?cc=us&l=en"


# ------------------------------------------------------------------ 基础工具

def http_get(url):
    """返回 (bytes|None, err)；None 表示失败（网络/HTTP 错误都算）"""
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            return r.read(), None
    except Exception as e:  # noqa: BLE001 —— 一切网络错误都按「源不可用」处理
        return None, str(e)[:120]


def to_iso(s):
    """把各种时间字符串统一成 UTC ISO（.000Z）；失败返回 ''"""
    if not s:
        return ""
    s = str(s).strip()
    # 已是 ISO
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?", s)
    if m:
        tail = "Z" if s.endswith("Z") else ""
        if not tail:  # 无时区标记：GamerPower / 常见聚合源按 UTC 处理
            return "%sT%s:%s.000Z" % (m.group(1), m.group(2), m.group(3) or "00")
        return "%sT%s:%s.000Z" % (m.group(1), m.group(2), m.group(3) or "00")
    return ""


def platforms_from_text(text):
    # GamerPower 的 platforms 是逗号字符串（如 Epic Games Store, PC, Android）
    s = str(text or "").lower()
    out = []
    if "android" in s:
        out.append("android")
    if "ios" in s or "iphone" in s or "ipad" in s:
        out.append("ios")
    if "mac" in s or "macos" in s:
        out.append("mac")
    if "pc" in s or "windows" in s or "epic" in s or "steam" in s or "gog" in s:
        out.append("pc")
    if "cloud" in s or "luna" in s:
        out.append("cloud")
    return out


def platforms_from_store(store):
    """freetokeep 的 store 字段带端信息：epic_pc / epic_ios / epic_android / gog / steam"""
    s = str(store or "").lower()
    out = []
    if "android" in s:
        out.append("android")
    if "ios" in s or "iphone" in s or "ipad" in s:
        out.append("ios")
    if "pc" in s or "windows" in s:
        out.append("pc")
    if not out:
        # 没标注端的（gog / steam / epic）按 PC 处理
        out.append("pc")
    # 固定顺序，便于比对
    return [p for p in ("pc", "ios", "android") if p in out]


def parse_price(s):
    """'$19.99' / '19,99€' → 19.99（美元优先，取第一段数字；失败 0）"""
    m = re.search(r"([\d.,]+)", str(s or ""))
    if not m:
        return 0
    try:
        return round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return 0


def norm_title(s):
    """归一标题：小写、去标点空白，用于跨源合并/去重。
    顺带剥掉聚合源加的「(Epic Games) Giveaway」这类后缀，
    否则官方条目和交叉源条目合不到一起。"""
    s = str(s or "").lower()
    s = re.sub(r"\((?:epic games store|epic games|epic|gog|steam|pc)\)\s*"
               r"(?:giveaway|free|freebie|100% off)\s*", "", s)
    s = re.sub(r"\b(?:giveaway|freebie)\b", "", s)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)


def has_cjk(s):
    return bool(re.search(r"[\u4e00-\u9fff]", str(s or "")))


def now_utc():
    return datetime.now(timezone.utc)


def iso(ms):
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def active_window_ms(start_iso, end_iso, now):
    """返回 'active' | 'upcoming' | 'ended' | ''（解析失败）"""
    try:
        s = datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%S.000Z") if start_iso else None
        e = datetime.strptime(end_iso, "%Y-%m-%dT%H:%M:%S.000Z") if end_iso else None
    except ValueError:
        return ""
    if e and e < now:
        return "ended"
    if s and s > now:
        return "upcoming"
    return "active"


def slugify(title, prefix):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(title or "").lower()).strip("-")
    return (prefix + "-" + (s or "item"))[:64]


# ------------------------------------------------------------------ Epic 主源

def fetch_epic_official():
    """官方 API 两次请求（中区中文标题 + 美区美元价），按 id 合并。
    返回 (list, err)；条目含 active 与 upcoming。"""
    out, err1, err2 = {}, "", ""
    for tag, loc, cc in (("zh", "zh-CN", "CN"), ("us", "en-US", "US")):
        body, err = http_get(EPIC_API.format(loc=loc, cc=cc))
        if body is None:
            if tag == "zh":
                err1 = err
            else:
                err2 = err
            continue
        try:
            data = json.loads(body.decode("utf-8", "ignore"))
            elements = (data.get("data", {}).get("Catalog", {})
                        .get("searchStore", {}).get("elements", [])) or []
        except Exception as e:  # noqa: BLE001
            if tag == "zh":
                err1 = "json:" + str(e)[:80]
            else:
                err2 = "json:" + str(e)[:80]
            continue
        for el in elements:
            eid = el.get("id") or ""
            title = (el.get("title") or "").strip()
            if not title:
                continue
            # 找促销窗口（当前 + 预告）
            promos = []
            promos += ((el.get("promotions") or {}).get("promotionalOffers") or [])
            promos += ((el.get("promotions") or {}).get("upcomingPromotions") or [])
            for grp in promos:
                for off in (grp.get("promotionalOffers") or []):
                    ds = (off.get("discountSetting") or {})
                    pct = ds.get("discountPercentage")
                    disc = (el.get("price", {}).get("totalPrice", {}) or {}).get("discountPrice")
                    if pct not in (0, 0.0) and disc != 0:
                        continue  # 不是免费
                    start = to_iso(off.get("startDate"))
                    end = to_iso(off.get("endDate"))
                    # 中区/美区标题不同（中文 vs 英文），必须按官方条目 id 合并
                    key = eid or title.lower()
                    cur = out.setdefault(key, {
                        "id": "epicfree-" + slugify(title, "x")[len("x-"):],
                        "title": title,
                        "titleZh": "",
                        "platform": "Epic Games Store",
                        "requiresPrime": False,
                        "giveaway": "epic",
                        "price": 0,
                        "startAt": start,
                        "endAt": end,
                        "isCode": False,
                        "codeExpireAt": "",
                        "claimUrl": "",
                        "sourceUrl": "https://store.epicgames.com/free-games",
                        "cover": "",
                        "sources": [],
                    })
                    # 时间取更晚的窗口（同款多促销时保留最长）
                    if start and (not cur["startAt"] or start < cur["startAt"]):
                        cur["startAt"] = start
                    if end and (not cur["endAt"] or end > cur["endAt"]):
                        cur["endAt"] = end
                    if tag == "zh" and has_cjk(title):
                        cur["titleZh"] = title  # 中区标题即中文
                    if tag == "us":
                        cur["title"] = title    # 英文标题为准
                        orig = ((el.get("price", {}).get("totalPrice", {}) or {})
                                .get("originalPrice") or 0)
                        if orig:
                            cur["price"] = round(orig / 100, 2)
                    # 封面 / 链接（us/zh 任一次拿到即可）
                    for img in (el.get("keyImages") or []):
                        if img.get("type") in ("OfferImageTall", "Thumbnail") and not cur["cover"]:
                            cur["cover"] = img.get("url") or ""
                    slug = None
                    for mp in ((el.get("catalogNs") or {}).get("mappings") or []):
                        if mp.get("pageSlug"):
                            slug = mp["pageSlug"]
                            break
                    cur["claimUrl"] = ("https://store.epicgames.com/p/" + slug) if slug \
                        else "https://store.epicgames.com/free-games"
                    cur.setdefault("platforms", [])
                    if "pc" not in cur["platforms"]:
                        cur["platforms"].append("pc")
                    if "epic-api" not in cur["sources"]:
                        cur["sources"].append("epic-api")
    if not out and (err1 or err2):
        return [], "epic-api 不可达：%s / %s" % (err1, err2)
    # 条目 id 用最终（英文）标题生成，保证稳定且不重复
    for cur in out.values():
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", cur["title"].lower()).strip("-") or "item"
        cur["id"] = ("epicfree-" + slug)[:64]
    return list(out.values()), ""


# ------------------------------------------------------------------ 聚合源

def fetch_freetokeep(store):
    """freetokeep.gg JSON API；epic 的 pc/ios/android 三路按标题去重保留 endAt 最晚"""
    body, err = http_get(FREETOKEEP.format(store=store))
    if body is None:
        return [], "freetokeep 不可达：" + err
    try:
        items = (json.loads(body.decode("utf-8", "ignore")) or {}).get("items") or []
    except Exception as e:  # noqa: BLE001
        return [], "freetokeep json:" + str(e)[:80]
    out = {}
    for it in items:
        title = (it.get("title") or "").strip()
        end = to_iso(it.get("endsAt"))
        start = to_iso(it.get("startsAt"))
        key = norm_title(title)
        if not key:
            continue
        # 实际可用平台：freetokeep 用 store 区分端（epic_pc / epic_ios / epic_android）
        plat = platforms_from_store(it.get("store") or store)
        cur = out.get(key)
        if cur:
            # 同款多端：合并可用平台，并保留更晚的下架时间
            for p in plat:
                if p not in cur["platforms"]:
                    cur["platforms"].append(p)
            if (end or "") > (cur.get("endAt") or ""):
                cur["endAt"] = end
            if (start or "") and (not cur.get("startAt") or start < cur["startAt"]):
                cur["startAt"] = start
            continue
        price = 0
        op = str(it.get("originalPrice") or "")
        if "$" in op:
            price = parse_price(op)
        out[key] = {
            "id": slugify(title, "fk-" + store),
            "title": title,
            "titleZh": "",
            "platform": {"epic": "Epic Games Store", "gog": "GOG", "steam": "Steam"}.get(
                (it.get("store") or store).split("_")[0], store),
            "requiresPrime": False,
            "giveaway": (it.get("store") or store).split("_")[0],
            "price": price,
            "platforms": plat,
            "startAt": start,
            "endAt": end,
            "isCode": False,
            "codeExpireAt": "",
            "claimUrl": it.get("url") or "",
            "sourceUrl": it.get("url") or "",
            "cover": it.get("imageUrl") or "",
            "sources": ["freetokeep"],
        }
    return list(out.values()), ""


def fetch_gamerpower(plat):
    """GamerPower giveaways；end_date 无时区按 UTC 处理"""
    body, err = http_get(GAMERPOWER.format(plat=plat))
    if body is None:
        return [], "gamerpower 不可达：" + err
    try:
        items = json.loads(body.decode("utf-8", "ignore")) or []
    except Exception as e:  # noqa: BLE001
        return [], "gamerpower json:" + str(e)[:80]
    if not isinstance(items, list):
        return [], "gamerpower 返回结构异常（%s）" % type(items).__name__
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "id": slugify(title, "gp"),
            "title": title,
            "titleZh": "",
            "platform": {"epic-games-store": "Epic Games Store",
                         "gog": "GOG", "steam": "Steam"}.get(plat, plat),
            "requiresPrime": False,
            "giveaway": {"epic-games-store": "epic", "gog": "gog", "steam": "steam"}.get(plat, plat),
            "price": parse_price(it.get("worth")),
            "platforms": platforms_from_text(it.get("platforms") or ""),
            "startAt": to_iso(it.get("published_date")),
            "endAt": to_iso(it.get("end_date")),
            "isCode": False,
            "codeExpireAt": "",
            "claimUrl": it.get("open_giveaway_url") or it.get("instructions") or "",
            "sourceUrl": it.get("open_giveaway_url") or "",
            "cover": it.get("thumbnail") or "",
            "sources": ["gamerpower"],
        })
    return out, ""


def fetch_gog_official():
    """GOG 官方 giveaways 页；404 / 无数据 = 当前无活动（空态属正常）"""
    body, err = http_get(GOG_PAGE)
    if body is None:
        # 404 也走这里 —— 当前无活动，不是错误
        if "404" in (err or "") or "410" in (err or ""):
            return [], ""
        return [], "gog 页面不可达：" + err
    html = body.decode("utf-8", "ignore")
    # GOG 页面内嵌 JSON（NEXT_DATA / INITIAL_STATE）里找 giveaway 名称与起止时间；
    # 页面结构会变，解析不到就按空态处理，不报错
    out = []
    for m in re.finditer(
            r'"title"\s*:\s*"([^"]{2,80})"[^{}]{0,400}?"start(?:s)?(?:_at|At|Date)"\s*:\s*"([^"]+)"'
            r'[^{}]{0,400}?"end(?:s)?(?:_at|At|Date)"\s*:\s*"([^"]+)"', html):
        title, start, end = m.group(1), to_iso(m.group(2)), to_iso(m.group(3))
        if not title or not end:
            continue
        out.append({
            "id": slugify(title, "gogfree"),
            "title": title,
            "titleZh": "",
            "platform": "GOG",
            "requiresPrime": False,
            "giveaway": "gog",
            "price": 0,
            "startAt": start,
            "endAt": end,
            "isCode": False,
            "codeExpireAt": "",
            "claimUrl": GOG_PAGE,
            "sourceUrl": GOG_PAGE,
            "cover": "",
            "sources": ["gog-page"],
        })
    return out, ""


def fetch_steam_official():
    """Steam featuredcategories：specials 里 100% 折扣且原价>0 = Free to keep。
    注意：该 API 不给截止时间，只有 freetokeep 交叉源有 —— 没有 endAt 的条目
    在合并阶段会被丢弃（宁缺勿错）。"""
    body, err = http_get(STEAM_API)
    if body is None:
        return [], "steam api 不可达：" + err
    try:
        specials = (json.loads(body.decode("utf-8", "ignore"))
                    .get("specials", {}).get("items") or [])
    except Exception as e:  # noqa: BLE001
        return [], "steam json:" + str(e)[:80]
    out = []
    for it in specials:
        if it.get("discount_percent") != 100:
            continue
        orig, final = it.get("original_price") or 0, it.get("final_price")
        if not orig or final != 0:
            continue  # 只要「原价>0 现在 0 元」，排除常驻免费游戏
        title = (it.get("name") or "").strip()
        if not title:
            continue
        out.append({
            "id": "steamfree-" + str(it.get("id") or slugify(title, "x")[2:]),
            "title": title,
            "titleZh": "",
            "platform": "Steam",
            "requiresPrime": False,
            "giveaway": "steam",
            "price": round(orig / 100, 2),
            "startAt": "",
            "endAt": "",        # 由 freetokeep 交叉补齐，补不齐则丢弃
            "isCode": False,
            "codeExpireAt": "",
            "claimUrl": "https://store.steampowered.com/app/%s/" % it.get("id"),
            "sourceUrl": "https://store.steampowered.com/search/?maxprice=free&specials=1",
            "cover": (it.get("header_image") or "").replace("header.jpg", "library_600x900.jpg"),
            "sources": ["steam-api"],
        })
    return out, ""


# ------------------------------------------------------------------ 合并 + 校验

def merge_section(main_items, cross_items, official_names, now_ms):
    """主源 + 交叉源按归一标题合并，套用冲突取舍规则。
    没有 endAt 的条目一律丢弃（宁缺勿错）。"""
    by_key = {}

    def find_fuzzy(key):
        """前缀同名：短名（如 gamerpower 的 "Home"）是长名
        （如官方的 "Home - A Unique Horror Adventure"）的前缀，
        且较短一方 ≥5 字符时视为同一款 —— 避免同款两条。"""
        if key in by_key:
            return key
        for k in by_key:
            if len(key) >= 4 and k.startswith(key):
                return k
            if len(k) >= 4 and key.startswith(k):
                return k
        return None

    def put(item, is_official):
        key = norm_title(item["title"])
        if not key:
            return
        end_ms = 0
        if item.get("endAt"):
            try:
                end_ms = datetime.strptime(item["endAt"],
                                           "%Y-%m-%dT%H:%M:%S.000Z").timestamp() * 1000
            except ValueError:
                pass
        if end_ms and end_ms <= now_ms:
            return  # 已结束的不收
        if not item.get("endAt") and not item.get("startAt"):
            return  # 两个时间都没有，没法展示 —— 丢弃
        existing = find_fuzzy(key)
        cur = by_key.get(existing) if existing else None
        if not cur:
            item["_official"] = is_official
            by_key[key] = item
            return
        # 规则 2：官方源时间覆盖聚合源
        if is_official:
            for f in ("startAt", "endAt", "price", "claimUrl", "cover"):
                if item.get(f):
                    cur[f] = item[f]
            cur["_official"] = True
        # 可用平台：跨源累积（Epic 常有 PC + iOS + Android 多端同送）
        for p in (item.get("platforms") or []):
            if p not in cur.setdefault("platforms", []):
                cur["platforms"].append(p)
        # 规则 7：保留 endAt 最晚
        if (item.get("endAt") or "") > (cur.get("endAt") or ""):
            cur["endAt"] = item["endAt"]
        # 溯源合并
        for s in item.get("sources", []):
            if s not in cur["sources"]:
                cur["sources"].append(s)

    for it in main_items:
        put(it, any(n in (it.get("sources") or []) for n in official_names))
    for it in cross_items:
        put(it, False)

    out = []
    for it in by_key.values():
        # 无 endAt 的条目（如 Steam 主源独有）丢弃
        if not it.get("endAt"):
            continue
        it.pop("_official", None)
        it["confidence"] = "high" if any(
            s in official_names for s in it.get("sources", [])) else "low"
        if not it.get("titleZh") or not has_cjk(it["titleZh"]):
            it["titleZh"] = it["titleZh"] if has_cjk(it["titleZh"]) else ""
        out.append(it)
    return out


def main():
    ap = argparse.ArgumentParser(description="免会员限免多源抓取（Epic/GOG/Steam）")
    ap.add_argument("--only", choices=["epic", "gog", "steam"], help="只抓一个源")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    ap.add_argument("-o", "--out", default=str(OUT_FILE), help="输出文件路径")
    args = ap.parse_args()

    now_ms = now_utc().timestamp() * 1000
    targets = [args.only] if args.only else ["epic", "gog", "steam"]
    result = {"updatedAt": iso(now_ms), "status": {}, "epic": [], "gog": [], "steam": []}
    prev = {}
    try:
        if OUT_FILE.exists():
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        prev = {}

    ok_all = True
    for store in targets:
        print("\n=== %s ===" % store.upper())
        err = ""
        if store == "epic":
            main_items, err = fetch_epic_official()
            cross, err2 = fetch_freetokeep("epic")
            if err2:
                print("  [交叉] " + err2)
            gp, err3 = fetch_gamerpower("epic-games-store")
            if err3:
                print("  [交叉] " + err3)
            items = merge_section(main_items, cross + gp, {"epic-api"}, now_ms)
        elif store == "gog":
            main_items, err = fetch_gog_official()
            cross, err2 = fetch_freetokeep("gog")
            if err2:
                print("  [交叉] " + err2)
            gp, err3 = fetch_gamerpower("gog")
            if err3:
                print("  [交叉] " + err3)
            items = merge_section(main_items, cross + gp, {"gog-page"}, now_ms)
        else:  # steam
            main_items, err = fetch_steam_official()
            cross, err2 = fetch_freetokeep("steam")
            if err2:
                print("  [交叉] " + err2)
            items = merge_section(main_items, cross, {"steam-api"}, now_ms)

        if err and not items:
            # 主源失败：空结果保护 —— unreachable 时由 gen-free.js 保留旧数据
            status = "unreachable" if "不可达" in err else "empty"
            print("  [!] %s" % err)
            print("  状态: %s（%s）" % (
                status,
                "保留上次数据" if status == "unreachable" else "按无活动处理"))
            if status == "unreachable":
                ok_all = False
            result["status"][store] = status
            result[store] = []
            continue

        result["status"][store] = "ok" if items else "empty"
        result[store] = items
        print("  抓到 %d 条" % len(items))
        for it in items:
            zh = (" / " + it["titleZh"]) if it["titleZh"] else ""
            print("   · %s%s  [%s ~ %s]  %s  (%s, %s)" % (
                it["title"], zh, (it.get("startAt") or "?")[:10],
                (it.get("endAt") or "?")[:10],
                "$%.2f" % it["price"] if it.get("price") else "免费",
                ",".join(it.get("sources", [])), it.get("confidence", "-")))

    # 空结果保护：上次非空这次为 empty → 打印人话提醒（gen-free.js 按状态决定写不写空）
    for store in targets:
        if result["status"].get(store) == "empty" and prev.get(store):
            print("\n[提醒] %s 这次没抓到条目，但上次有 %d 条。"
                  "可能是源站改版或抓取被拦，请核对后用 --force 确认清空。"
                  % (store.upper(), len(prev[store])))

    print("\n状态汇总: %s" % json.dumps(result["status"], ensure_ascii=False))

    if args.dry_run:
        print("\n[dry-run] 未写文件")
        return 0 if (ok_all or not targets) else 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print("已写出 %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
