#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prime Gaming / Amazon Games 免费游戏抓取器

输出：scraper/data/games.json （标准化 schema，直接喂给 ingestGames 云函数）

用法：
    python scrape.py                # 抓取 + 写本地 json
    python scrape.py --push         # 抓取 + 推送到云函数（需环境变量 INGEST_URL / INGEST_TOKEN）
    python scrape.py --dry-run      # 只看能抓到什么，不写文件

环境变量：
    INGEST_URL    云开发 HTTP 访问服务地址，例如
                  https://<env-id>.service.tcloudbase.com/ingestGames
    INGEST_TOKEN  与云函数环境变量 INGEST_TOKEN 一致

依赖：
    pip install -r requirements.txt
    python -m playwright install chromium
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# 2026-10 实测：gaming.amazon.com/home 已 302 跳转到 luna.amazon.com/claims/home
# （Prime Gaming 品牌于 2025-10 并入 Amazon Luna）。直接打最终地址，省一次跳转。
# 旧地址仍可用（会自动跟随），但新地址更稳。
TARGET = os.environ.get("SCRAPE_TARGET", "https://luna.amazon.com/claims/home")

# 被风控 / 地区限制时页面会出现这些文案。
# 此时必须明确判定「失败」，否则会静默返回 0 条，看起来像"这周没有游戏"。
BLOCKED_MARKERS = (
    "we're having technical difficulties",
    "experiencing issues with our service",
    "you need to enable javascript to run this app",
    "not available in your country",
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
)

# 需要代理时设置（国内网络直连 Amazon 大概率被拦截）：
#   Windows:  set HTTPS_PROXY=http://127.0.0.1:7890
#   Linux:    export HTTPS_PROXY=http://127.0.0.1:7890
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""

OUT_DIR = Path(__file__).parent / "data"
OUT_FILE = OUT_DIR / "games.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 平台归一化映射（页面上写法五花八门）
PLATFORM_MAP = {
    "gog": "GOG",
    "epic": "Epic Games Store",
    "epic games": "Epic Games Store",
    "amazon games": "Amazon Games App",
    "amazon": "Amazon Games App",
    "legacy": "Legacy Games",
    "legacy games": "Legacy Games",
    "microsoft": "Microsoft Store",
    "luna": "Luna",
    "in-game": "游戏内礼包",
}


def is_blocked(text: str) -> str | None:
    """页面是否是被风控/地区限制的错误页。返回匹配到的文案，正常页面返回 None。"""
    if not text:
        return "empty response"
    low = text.lower()
    for m in BLOCKED_MARKERS:
        if m in low:
            return m
    return None


# --------------------------------------------------------------------------- #
# 1. 抓取：优先 Playwright，失败降级 requests
# --------------------------------------------------------------------------- #

def fetch_with_playwright():
    """真实浏览器渲染，能拿到 JS 注入后的卡片。"""
    from playwright.sync_api import sync_playwright

    cards = []
    html = ""
    with sync_playwright() as p:
        launch_kw = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if PROXY:
            launch_kw["proxy"] = {"server": PROXY}
        browser = p.chromium.launch(**launch_kw)
        ctx = browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1440, "height": 960},
        )
        # 去掉最明显的自动化痕迹
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = ctx.new_page()
        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        # 触发懒加载：滚到底再回顶
        for _ in range(6):
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(800)
        page.wait_for_timeout(1500)

        html = page.content()

        blocked = is_blocked(html)
        if blocked:
            browser.close()
            raise RuntimeError(f"页面被拦截（命中文案：{blocked}）——换个网络出口或配置 HTTPS_PROXY 再试")

        # 策略 A：从卡片 DOM 直接读（结构变了只改这里）
        cards = page.evaluate(
            """() => {
                const out = [];
                const sels = [
                  '[data-a-target="offer-card"]',
                  '[class*="OfferCard"]',
                  '[class*="offer-card"]',
                  '[data-testid*="offer"]',
                  '[class*="ClaimCard"]',
                  '[class*="GameCard"]',
                  '[class*="game-card"]'
                ];
                let nodes = [];
                for (const s of sels) {
                  nodes = Array.from(document.querySelectorAll(s));
                  if (nodes.length) break;
                }
                nodes.forEach(n => {
                    const t = n.querySelector('[class*="title"], h3, h2');
                    const img = n.querySelector('img');
                    const a = n.querySelector('a[href]');
                    const txt = n.innerText || '';
                    if (t && t.innerText) {
                        out.push({
                            title: t.innerText.trim(),
                            cover: img ? (img.currentSrc || img.src) : '',
                            href: a ? a.href : '',
                            text: txt.slice(0, 400)
                        });
                    }
                });
                return out;
            }"""
        )
        browser.close()
    return cards, html


def fetch_with_requests():
    """无浏览器兜底。注意：页面是纯客户端渲染，这条路径通常拿不到卡片，
    主要作用是判断「能不能连通、有没有被拦截」。"""
    resp = requests.get(
        TARGET,
        headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Mode": "navigate",
            "Upgrade-Insecure-Requests": "1",
        },
        proxies={"https": PROXY, "http": PROXY} if PROXY else None,
        timeout=30,
    )
    resp.raise_for_status()
    blocked = is_blocked(resp.text)
    if blocked:
        raise RuntimeError(f"页面被拦截（命中文案：{blocked}）")
    return [], resp.text


# --------------------------------------------------------------------------- #
# 2. 解析
# --------------------------------------------------------------------------- #

# 例："Ends in 3d 12h" / "结束时间：10月31日 22:59" / "Ends Oct 31, 2026 10:59 PM"
RE_ENDS_IN = re.compile(r"(?:ends?\s*in|剩余)\s*([0-9]+)\s*d(?:\s*([0-9]+)\s*h)?", re.I)
RE_DATE = re.compile(
    r"(?:ends?|claim by|until)\s*:?\s*([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s*\d{4}[^,;]*)", re.I
)


def parse_card(card):
    """把一张卡片的原始文本解析成标准条目，解析不出的字段留空，由后续过滤兜底。"""
    title = (card.get("title") or "").strip()
    if not title or len(title) < 2:
        return None

    text = card.get("text") or ""
    low = text.lower()

    # 平台
    platform = "Amazon Games App"
    for key, val in PLATFORM_MAP.items():
        if key in low:
            platform = val
            break

    # 是否需要 Prime
    requires_prime = not ("without prime" in low or "no prime" in low or "free for all" in low)

    # 结束时间
    end_at = None
    m = RE_ENDS_IN.search(text)
    if m:
        days = int(m.group(1))
        hours = int(m.group(2) or 0)
        end_at = datetime.now(timezone.utc) + timedelta(days=days, hours=hours)
    if end_at is None:
        m2 = RE_DATE.search(text)
        if m2:
            for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d %Y"):
                try:
                    end_at = datetime.strptime(
                        re.sub(r"\s+", " ", m2.group(1)).strip(), fmt
                    ).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

    if end_at is None:
        return None  # 没有截止时间 → 无法倒计时 → 丢弃（宁缺勿错）

    return {
        "id": "amz-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60],
        "title": title,
        "platform": platform,
        "requiresPrime": requires_prime,
        "price": 0,
        "currency": "CNY",
        "rarity": "normal",
        "tags": [],
        "cover": card.get("cover") or "",
        "startAt": datetime.now(timezone.utc).isoformat(),
        "endAt": end_at.isoformat(),
        "claimUrl": card.get("href") or TARGET,
        "sourceUrl": TARGET,
    }


def parse_html_fallback(html):
    """无浏览器时兜底：从内嵌 JSON 里捞 {title, endTime} 结构。"""
    found = []

    def walk(node, depth=0):
        if depth > 8 or len(found) > 80:
            return
        if isinstance(node, list):
            for x in node:
                walk(x, depth + 1)
            return
        if not isinstance(node, dict):
            return
        title = node.get("title") or node.get("productTitle") or node.get("name")
        end = (
            node.get("endTime")
            or node.get("endDate")
            or node.get("claimEndTime")
            or node.get("availableUntil")
        )
        if isinstance(title, str) and end and len(str(end)) > 8:
            found.append({"title": title, "endAt": str(end)})
            return
        for v in node.values():
            walk(v, depth + 1)

    for blob in re.findall(r"__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>", html, re.S):
        try:
            walk(json.loads(blob))
        except json.JSONDecodeError:
            continue

    out = []
    for x in found:
        try:
            end = datetime.fromisoformat(str(x["endAt"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append(
            {
                "id": "amz-" + re.sub(r"[^a-z0-9]+", "-", x["title"].lower()).strip("-")[:60],
                "title": x["title"],
                "platform": "Amazon Games App",
                "requiresPrime": True,
                "price": 0,
                "currency": "CNY",
                "rarity": "normal",
                "tags": [],
                "cover": "",
                "startAt": datetime.now(timezone.utc).isoformat(),
                "endAt": end.isoformat(),
                "claimUrl": TARGET,
                "sourceUrl": TARGET,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 3. 清洗 / 去重 / 落盘
# --------------------------------------------------------------------------- #

def clean(items):
    seen = {}
    for it in items:
        if not it or not it.get("id"):
            continue
        it["title"] = unicodedata.normalize("NFKC", it["title"]).strip()
        # 同一 id 保留截止时间更晚的（更可能是主条目而非 DLC）
        if it["id"] in seen:
            if it["endAt"] > seen[it["id"]]["endAt"]:
                seen[it["id"]] = it
        else:
            seen[it["id"]] = it
    return sorted(seen.values(), key=lambda x: x["endAt"])


def push_to_cloud(items):
    url = os.environ.get("INGEST_URL")
    token = os.environ.get("INGEST_TOKEN")
    if not url or not token:
        print("[push] 缺少 INGEST_URL / INGEST_TOKEN，跳过推送")
        return False

    resp = requests.post(
        url,
        json={"token": token, "list": items, "source": "github-actions"},
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    print(f"[push] {resp.status_code} {resp.text[:300]}")
    return resp.status_code == 200


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="抓取后推送到云函数")
    ap.add_argument("--dry-run", action="store_true", help="不写文件")
    args = ap.parse_args()

    print(f"[1/4] 抓取 {TARGET}")
    if PROXY:
        print(f"      使用代理 {PROXY}")
    try:
        cards, html = fetch_with_playwright()
        print(f"      playwright 提取卡片 {len(cards)} 张")
    except Exception as e:  # noqa: BLE001
        print(f"      playwright 失败（{e}），降级 requests")
        try:
            cards, html = fetch_with_requests()
        except Exception as e2:  # noqa: BLE001
            print(f"!! 两条抓取路径都失败：{e2}")
            print("   常见原因：1) 当前网络出口被 Amazon 风控拦截；")
            print("             2) 需要代理（设置 HTTPS_PROXY 后重试）；")
            print("             3) 页面结构改版，需更新选择器。")
            sys.exit(2)

    print("[2/4] 解析")
    blocked = is_blocked(html)
    if blocked:
        print(f"!! 抓到的是错误页（命中文案：{blocked}），不写文件、不覆盖上次快照")
        print("   解决：换网络出口，或设置 HTTPS_PROXY 后重试")
        sys.exit(2)

    items = [x for x in (parse_card(c) for c in cards) if x]
    if not items:
        items = parse_html_fallback(html)
        print(f"      兜底解析得到 {len(items)} 条")
    else:
        print(f"      卡片解析得到 {len(items)} 条")

    items = clean(items)
    print(f"[3/4] 清洗后 {len(items)} 条")

    if not items:
        print("!! 本轮没有解析到任何有效数据，保留上次快照，不覆盖")
        sys.exit(1)

    if not args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "source": TARGET,
            "count": len(items),
            "list": items,
        }
        OUT_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[4/4] 已写入 {OUT_FILE}")
    else:
        print(json.dumps(items[:3], ensure_ascii=False, indent=2))

    if args.push:
        push_to_cloud(items)


if __name__ == "__main__":
    main()
