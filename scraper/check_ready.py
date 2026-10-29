#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_ready.py — 抓取健康度检查（供 GitHub Actions 在推送前调用）
==================================================================
为什么要它：
    云函数 ingestGames 会把「本次推送里没有的 id」标记成 removed（删除）。
    如果某次抓取只成功了一半（比如 Prime 被拦、或某个限免源不可达），
    直接把残缺数据推上去，会把正常游戏误删，小程序里就忽隐忽现。

判定规则（三者任一不满足即视为“不健康”，跳过本次推送）：
    1) Prime 抓取成功（环境变量 PRIME_OUTCOME == success）
    2) 没有任何限免源处于 unreachable（不可达）状态
    3) Prime + 限免的总条数 > 0

输出：
    向 $GITHUB_OUTPUT 写入 ok=true/false
    返回码：0=健康（可推送）  1=不健康（跳过推送）
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAMES = os.path.join(HERE, "data", "games.json")
FREE = os.path.join(HERE, "data", "free.json")


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        print("[!] 读取失败 %s：%s" % (os.path.basename(path), e))
        return {}


def write_output(ok):
    """把结果写给下一步（GitHub Actions 的 steps.health.outputs.ok）"""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("ok=%s\n" % ("true" if ok else "false"))
    except Exception as e:  # noqa: BLE001
        print("[!] 写 GITHUB_OUTPUT 失败：%s" % e)


def main():
    ok = True

    # 1) Prime 抓取结果
    outcome = os.environ.get("PRIME_OUTCOME", "")
    if outcome != "success":
        print("[x] Prime 抓取失败（outcome=%s）" % (outcome or "未知"))
        ok = False

    # 2) Prime 条数
    games = load(GAMES)
    n_prime = len(games.get("list") or []) if isinstance(games, dict) else 0

    # 3) 限免源状态
    free = load(FREE)
    status = (free.get("status") or {}) if isinstance(free, dict) else {}
    unreachable = [k for k, v in status.items() if v == "unreachable"]
    if unreachable:
        print("[x] 限免源不可达：%s" % ", ".join(unreachable))
        ok = False

    n_free = 0
    if isinstance(free, dict):
        for k in ("epic", "gog", "steam"):
            n_free += len(free.get(k) or [])

    print("统计：Prime=%d 条，限免=%d 条，源状态=%s"
          % (n_prime, n_free, json.dumps(status, ensure_ascii=False)))

    # 4) 总数为 0 也没意义
    if n_prime + n_free == 0:
        print("[x] 两条链路都没有数据")
        ok = False

    write_output(ok)

    if ok:
        print("[√] 抓取健康，可以推送")
        return 0

    print("[!] 本次抓取不完整 → 跳过推送。")
    print("    云端旧数据保持不变，下一轮正常会自动更新（不会误删现有游戏）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
