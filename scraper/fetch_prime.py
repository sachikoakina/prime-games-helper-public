#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prime Gaming / Amazon Luna 当前可领取游戏 —— 真实数据抓取（次选源）
================================================================
为什么需要这个脚本
------------------
官方入口 https://luna.amazon.com/claims/home 是纯客户端渲染（JS 注入），
用 requests / axios 抓 HTML 一条数据都拿不到；从中国大陆直连还会被风控
拦截，返回 "technical difficulties / enable JavaScript" 的空壳页。

本脚本改从聚合站 NerdZap 取数，它把同样的清单**服务端渲染**成 HTML，
并且每张卡片都带机器可读的时间戳属性（卡片解析逻辑在 nerdzap.py，
与 fetch_prime_preview.py 共用，源站改版只改一处）。

优点（对比直连官方）：
  · 国内网络直连可达，无需代理 / 无需境外出口
  · 无需登录态、Cookie、API Key
  · 服务端渲染 → 不需要 Playwright，零第三方依赖（只用标准库）
  · 时间是精确的 ISO 时间戳，不用猜 "Ends 16 Dec" 是几号几点

输出：scraper/data/games.json（标准格式，可直接喂给 tools/json2mock.js）

用法：
    python scraper/fetch_prime.py                 # 抓并写入 scraper/data/games.json
    python scraper/fetch_prime.py --dry-run       # 只打印结果，不落盘
    python scraper/fetch_prime.py -o out.json     # 指定输出路径
    python scraper/fetch_prime.py --url <url>     # 换源（页面结构需一致）

退出码：0 成功 / 1 网络或解析失败 / 2 命中风控页（拿到空壳）
------------------------------------------------------------------------------
"""

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 同目录共享模块（Actions 以 `python scraper/fetch_prime.py` 运行时脚本目录
# 天然在 sys.path；显式注入是为了 `python -I` / 其他 cwd 下也能跑）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nerdzap

SRC_URL = "https://nerdzap.com/free-games/prime-gaming/"
OUT_DIR = Path(__file__).parent / "data"
OUT_FILE = OUT_DIR / "games.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CLAIM_URL = "https://luna.amazon.com/claims/home"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def log(msg):
    print(msg, flush=True)


def http_get(url, timeout=30):
    """带 UA 的 GET。ssl 上下文宽松处理，避免个别环境证书链不全导致失败。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, errors="replace")


def looks_blocked(page):
    """拿到的是不是风控空壳页。"""
    low = page.lower()
    for kw in ("enable javascript", "technical difficulties", "are you a human",
               "request unsuccessful", "access denied"):
        if kw in low:
            return kw
    return None


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:60] or "game"


# --------------------------------------------------------------------------- #
# 解析（卡片结构解析在 nerdzap.py，fetch_prime_preview 共用同一份）
# --------------------------------------------------------------------------- #
def parse_page(page):
    """从 HTML 里抽出卡片列表：共用 nerdzap.parse_cards，这里只做主链路补齐。"""
    items = []
    for c in nerdzap.parse_cards(page):
        if not c["endAt"]:
            continue  # 没有截止时间的条目对小程序没意义
        items.append({
            "id": c["dataId"] or ("prime-" + slugify(c["title"])),
            "title": c["title"],
            "titleZh": "",
            "platform": c["platform"],
            "requiresPrime": True,
            "price": 0,
            "rarity": "normal",
            "tags": [c["reviews"]] if c["reviews"] else [],
            "cover": c["cover"],
            # 有 data-start 才写；没有就留空，让下游按「已在送」兜底。
            # 旧写法拿 endAt 冒充 startAt，会把 _status=soon 的预告伪装成已开始。
            "startAt": c["startAt"],
            "endAt": c["endAt"],
            "claimUrl": c["claim"],
            "sourceUrl": SRC_URL,
            "_status": c["status"],
        })
    return items


def dedup(items):
    seen, out = set(), []
    for it in items:
        key = (it["title"].lower(), it["endAt"][:10])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="抓取 Prime Gaming / Amazon Luna 当前可领取游戏")
    ap.add_argument("--url", default=SRC_URL, help="数据源页面（默认 NerdZap Prime 页）")
    ap.add_argument("-o", "--out", default=str(OUT_FILE), help="输出 JSON 路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    ap.add_argument("--local", help="解析本地 HTML 文件（调试用，或 Python 出站被拦截时配合 curl 使用）")
    args = ap.parse_args()

    log("")
    log("[1/3] 抓取  " + args.url)
    if args.local:
        p = Path(args.local)
        if not p.exists():
            log("      × 本地文件不存在：" + str(p))
            return 1
        page = p.read_text(encoding="utf-8", errors="replace")
        log("      √ 读取本地文件 %.0f KB" % (len(page) / 1024))
    else:
        try:
            page = http_get(args.url)
        except urllib.error.URLError as e:
            log("      × 网络失败：" + str(e))
            log("")
            log("        常见原因：断网 / 公司网络拦截 / 站点改版。")
            log("        若你能访问但脚本拿不到，试这两步绕开：")
            log("          1. curl -A \"Mozilla/5.0\" -o /tmp/pg.html " + args.url)
            log("          2. python scraper/fetch_prime.py --local /tmp/pg.html")
            log("        需要代理时设置 HTTPS_PROXY 环境变量。")
            return 1
        except Exception as e:  # noqa: BLE001
            log("      × 抓取异常：" + str(e))
            return 1
        log("      √ 拿到 %d KB 页面" % (len(page) // 1024))

    blocked = looks_blocked(page)
    if blocked:
        log("      × 命中风控页（页面含 \"%s\"）" % blocked)
        log("")
        log("        这说明对端没有返回真实内容。换个网络出口再试，或改用 C 档（云端抓取）。")
        return 2

    log("[2/3] 解析")
    items = parse_page(page)
    if not items:
        log("      × 一张卡片都没解析到。")
        log("")
        log("        多半是站点改版了 HTML 结构。把下面这份页面内容发出来即可定位：")
        log("          " + args.url)
        return 1
    items = dedup(items)
    items.sort(key=lambda x: x["endAt"])
    log("      √ 解析出 %d 条" % len(items))

    now = datetime.now(timezone.utc)
    available = [x for x in items if x["endAt"] > now.strftime("%Y-%m-%dT%H:%M:%S.000Z")]
    log("        其中未过期 %d 条" % len(available))

    payload = {
        "updatedAt": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "source": args.url,
        "note": "Prime Gaming / Amazon Luna 当前可领取清单（聚合站快照，非官方接口）",
        "count": len(items),
        "list": items,
    }

    log("[3/3] 输出")
    if args.dry_run:
        log("      --dry-run，不写文件。前 5 条预览：")
        for x in items[:5]:
            log("        · %-46s %-20s 截止 %s" % (x["title"][:46], x["platform"], x["endAt"][:10]))
        log("")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("      √ 已写入 " + str(out))

    log("")
    log("  下一步：把它灌进小程序")
    log("    node tools/json2mock.js")
    log("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
