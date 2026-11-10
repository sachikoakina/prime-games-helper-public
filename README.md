# prime-games-helper

Prime Gaming / Epic / GOG / Steam 限免助手的**数据生产端**：只负责抓取与推送，不含任何小程序代码。

> 展示端在独立仓库 `Amazon Prime Gaming free games`（小程序 + 云函数）。
> 本仓库只产出 `scraper/data/*.json` 并推送到云开发数据库，两者互不包含对方文件。

## 仓库结构

```
scraper/            数据抓取（GitHub Actions 每半小时自动跑，只用 Python 标准库）
  fetch_prime.py          Prime Gaming 当前可领 → data/games.json
  fetch_free.py           Epic / GOG / Steam 限免（多源交叉验证）→ data/free.json
  fetch_prime_preview.py  官方公告预告（未来排期，三源交叉验证）→ data/preview.json
  check_ready.py          健康检查：抓取不完整就跳过本轮推送（避免误删正常游戏）
  push_cloud.py           合并推送 → 云开发数据库
  nerdzap.py              NerdZap 卡片解析（两个抓取脚本共用）
  tests/                  unittest 回归测试（python -m unittest discover -s scraper/tests）
.github/workflows/  定时调度（scrape.yml）
README.md           本文
```

本仓库的职责边界写在 `.gitignore` 里：除 `.gitignore`、`README.md`、`.github/`、`scraper/`
之外，其余路径一律不被跟踪。小程序、云函数、文档、编辑器配置都不应出现在这里。

## 自动抓取链路

GitHub Actions 每半小时运行一次（`.github/workflows/scrape.yml`）：

1. `fetch_prime.py` + `fetch_free.py` + `fetch_prime_preview.py` → `scraper/data/*.json`
2. `check_ready.py`：健康检查，抓取不完整就跳过本轮推送
3. `push_cloud.py`：合并数据并推送到云开发数据库（经 HTTP 网关）
4. 快照上传为 artifact（保留 7 天，排查用）

## 启用前：配置两个 Secrets

仓库 `Settings → Secrets and variables → Actions` 新增：

| 名称 | 说明 |
|---|---|
| `INGEST_URL` | 云开发 HTTP 网关地址，形如 `https://<环境ID>.service.tcloudbase.com/ingestGames` |
| `INGEST_TOKEN` | 与云函数 `ingestGames` 环境变量一致的密钥 |

未配置时工作流仍会执行抓取，但推送步骤会自动跳过（不会报错）。

## 手动运行

```bash
python scraper/fetch_prime.py             # 抓 Prime 游戏
python scraper/fetch_free.py              # 抓 Epic / GOG / Steam 限免
python scraper/fetch_prime_preview.py     # 抓官方公告预告（未来排期）
python scraper/push_cloud.py --dry-run    # 只统计，不推送
python scraper/push_cloud.py              # 推送（读 INGEST_URL / INGEST_TOKEN）
python -m unittest discover -s scraper/tests   # 回归测试
```

以上脚本只用 Python 标准库，无需 `pip install`，也不需要 Playwright。

## 状态怎么读

`free.json` 里的 `status` 每个源只有三种值：

| 值 | 含义 | 该怎么做 |
|---|---|---|
| `ok` | 抓到条目 | — |
| `empty` | 抓取成功，但当前确实没有活动 | 正常，不用管；若上次非空会打印提醒 |
| `unreachable` | 源不可达（网络/超时/HTTP 错误），已保留上次数据 | 健康检查会跳过本轮推送 |

两个容易误判的点：

- **GOG 长期是 `empty`**：GOG 官方赠品页 `https://www.gog.com/en/giveaways` 已下线（恒 404），
  没有可用的官方源，GOG 只能靠 GamerPower 与 freetokeep 两个聚合站；两家都报"无活动"时
  就是真的没有活动，不是抓取坏了。
- **GamerPower 无活动时不返回空数组**，而是 `HTTP 201` + `{"status":0,"status_message":...}`。
  `fetch_gamerpower` 已按 `status` 分流：`0` = 空态（不报错）、非 0 = 真报错（打印原因）。

## 触发方式

- 定时：每 30 分钟一轮（GitHub 用 UTC，每小时 `:10` / `:40`）
- 手动：Actions 页面 → 「自动抓取并同步游戏数据」→ Run workflow

> GitHub 的定时工作流在仓库连续 60 天无提交后可能被自动停用；届时到 Actions 页面重新 Enable 即可。
