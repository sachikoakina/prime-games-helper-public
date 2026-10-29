#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
push_cloud.py — 把本地抓到的数据推送到云开发数据库（B 档数据源）
==================================================================
它做什么：
    读 scraper/data/games.json（Prime 每月游戏）+ scraper/data/free.json
    （Epic / GOG / Steam 限免），合并去重后 POST 给云函数 ingestGames，
    由它写入云开发的 games 集合并更新 meta/sync 的时间戳。

需要什么（只需一次）：
    INGEST_URL    HTTP 网关地址，形如
                  https://xxx.service.tcloudbase.com/ingestGames
    INGEST_TOKEN  与云函数 ingestGames 的环境变量 INGEST_TOKEN 一致

用法：
    python scraper/push_cloud.py --dry-run        # 只统计不推送，先看看有多少条
    python scraper/push_cloud.py                  # 推送（读环境变量）
    python scraper/push_cloud.py --url https://xxx/ingestGames --token abc123
    python scraper/push_cloud.py --only prime     # 只推 Prime 每月游戏
    python scraper/push_cloud.py --only free      # 只推限免

退出码：0 成功 / 1 失败（数据没推上去，本地文件不受影响）
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# 只用标准库，不用装任何包

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
GAMES_JSON = os.path.join(DATA_DIR, "games.json")
FREE_JSON = os.path.join(DATA_DIR, "free.json")


# ---------------------------------------------------------------- 读数据

def load_json(path):
    if not os.path.exists(path):
        print("[!] 找不到 %s（跳过）" % os.path.basename(path))
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("[!] %s 解析失败：%s（跳过）" % (os.path.basename(path), e))
        return None


def collect_prime():
    """Prime 每月游戏：games.json 的 list 字段"""
    doc = load_json(GAMES_JSON)
    if not doc:
        return []
    return list(doc.get("list") or [])


def collect_free():
    """限免：free.json 的 epic / gog / steam 三个分组"""
    doc = load_json(FREE_JSON)
    if not doc:
        return []
    out = []
    for key in ("epic", "gog", "steam"):
        out.extend(list(doc.get(key) or []))
    return out


# ---------------------------------------------------------------- 归一

def parse_ms(value):
    """ISO 时间字符串 → 毫秒；解析不了返回 None"""
    if not value:
        return None
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def normalize(raw):
    """
    对齐 cloudfunctions/ingestGames 的入库校验：
    id 与 endAt 必填，endAt 必须能解析成时间，否则整条丢弃。
    """
    if not raw or not isinstance(raw, dict):
        return None
    item_id = str(raw.get("id") or "").strip()
    end_ms = parse_ms(raw.get("endAt"))
    if not item_id or end_ms is None:
        return None

    start_ms = parse_ms(raw.get("startAt"))
    if start_ms is None:
        start_ms = end_ms - 30 * 86400000   # 没上架时间：按截止前 30 天算

    price = raw.get("price")
    try:
        price = float(price)
    except Exception:
        price = 0.0

    out = {
        "id": item_id[:80],
        "title": str(raw.get("title") or raw.get("titleZh") or "")[:120],
        "titleZh": str(raw.get("titleZh") or "")[:120],
        "platform": str(raw.get("platform") or "Unknown")[:40],
        "requiresPrime": bool(raw.get("requiresPrime")),
        "price": price,
        "currency": raw.get("currency") or "USD",
        "rarity": "high" if raw.get("rarity") == "high" else "normal",
        "tags": list(raw.get("tags") or [])[:6],
        "cover": raw.get("cover") or "",
        "startAt": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
        "endAt": datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat(),
        "claimUrl": raw.get("claimUrl") or "https://gaming.amazon.com/home",
        "sourceUrl": raw.get("sourceUrl") or "https://gaming.amazon.com/home",
    }
    # 设备类型（PC / iOS / Android …）：前端按数据判定，能带就带上
    if raw.get("platforms"):
        out["platforms"] = list(raw["platforms"])[:4]
    # 兑换码有效期：仅展示用，不参与买号策略
    if raw.get("codeExpireAt"):
        out["codeExpireAt"] = raw["codeExpireAt"]
    return out


def build_list(only):
    raw_items = []
    if only in (None, "all", "prime"):
        raw_items.extend(collect_prime())
    if only in (None, "all", "free"):
        raw_items.extend(collect_free())

    items = []
    seen = {}
    skipped = 0
    for raw in raw_items:
        item = normalize(raw)
        if not item:
            skipped += 1
            continue
        if item["id"] in seen:
            continue          # 同一 id 多源重复，保留第一条
        seen[item["id"]] = 1
        items.append(item)
    return items, skipped


# ---------------------------------------------------------------- 推送

def push(url, token, items, source="local-push"):
    """POST 给 HTTP 网关；返回 (状态码, 响应文本)"""
    body = {"token": token, "list": items, "source": source}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        # 4xx / 5xx 也带响应体，里面是云函数返回的业务结果
        return e.code, e.read().decode("utf-8", "ignore")


def main():
    ap = argparse.ArgumentParser(description="推送抓取数据到云开发数据库")
    ap.add_argument("--url", help="INGEST_URL（HTTP 网关地址）")
    ap.add_argument("--token", help="INGEST_TOKEN")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不推送")
    ap.add_argument("--only", choices=["all", "prime", "free"], default="all",
                    help="只推哪一类，默认全部")
    args = ap.parse_args()

    print("")
    print("===========================================")
    print("  推送数据到云开发（B 档数据源）")
    print("===========================================")
    print("")

    items, skipped = build_list(args.only)
    print("待推送：%d 条" % len(items))
    if skipped:
        print("（跳过 %d 条：缺 id 或 endAt 无法解析）" % skipped)
    if not items:
        print("")
        print("[x] 没有可推送的数据。先跑一次抓取：")
        print("      python scraper/fetch_prime.py")
        print("      python scraper/fetch_free.py")
        sys.exit(1)

    # 预览前几条，确认字段没串
    print("")
    for it in items[:3]:
        print("   · %s | %s | %s" % (it["title"][:28], it["platform"][:20], it["endAt"][:10]))
    if len(items) > 3:
        print("   … 其余 %d 条省略" % (len(items) - 3))
    print("")

    if args.dry_run:
        print("[dry-run] 以上只是预演，没有真的推送。")
        print("")
        sys.exit(0)

    url = args.url or os.environ.get("INGEST_URL", "").strip()
    token = args.token or os.environ.get("INGEST_TOKEN", "").strip()
    if not url or not token:
        print("[x] 缺少 INGEST_URL / INGEST_TOKEN。")
        print("")
        print("    cmd 里先执行（注意 set 后面不能有空格）：")
        print("      set INGEST_URL=https://你的网关地址/ingestGames")
        print("      set INGEST_TOKEN=你的密钥")
        print("      python scraper/push_cloud.py")
        print("")
        sys.exit(1)

    print("推送到：%s" % url)
    try:
        status, text = push(url, token, items)
    except Exception as e:
        print("")
        print("[x] 请求失败：%s" % e)
        print("    常见原因：网关地址写错 / 网络不通 / 函数未部署")
        sys.exit(1)

    print("")
    print("HTTP %d" % status)
    print("返回：%s" % (text or "")[:400])

    ok = False
    try:
        data = json.loads(text)
        # HTTP 网关模式下，业务结果在 body 字符串里
        if isinstance(data.get("body"), str):
            data = json.loads(data["body"])
        ok = data.get("code") == 0
    except Exception:
        ok = status == 200

    if ok:
        print("")
        print("===========================================")
        print("  推送成功！")
        print("  回开发者工具点「编译」，首页顶部会显示")
        print("  「数据更新于 … · 来源：云函数」")
        print("===========================================")
        print("")
        sys.exit(0)

    print("")
    print("[x] 推送没成功，对照返回信息排查（401 = token 不对）。")
    sys.exit(1)


if __name__ == "__main__":
    main()
