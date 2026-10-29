# prime-games-helper-public · 数据源

## 它做什么

GitHub Actions 每 6 小时自动运行一次：

1. `scraper/fetch_prime.py`：抓取 Prime Gaming 当月可领游戏 → `scraper/data/games.json`
2. `scraper/fetch_free.py`：抓取 Epic / GOG / Steam 限免 → `scraper/data/free.json`
3. `scraper/check_ready.py`：健康检查，抓取不完整就跳过本轮推送（避免把正常游戏误删）
4. `scraper/push_cloud.py`：合并数据并推送到云开发数据库（经 HTTP 网关）

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
python scraper/push_cloud.py --dry-run    # 只统计，不推送
python scraper/push_cloud.py              # 推送（读 INGEST_URL / INGEST_TOKEN）
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

## 目录

```
.github/workflows/scrape.yml   定时任务（每 6 小时 + 手动触发）
scraper/
├── fetch_prime.py             Prime 游戏 → data/games.json
├── fetch_free.py              Epic/GOG/Steam 限免 → data/free.json
├── check_ready.py             抓取健康检查
├── push_cloud.py              合并并推送到云开发
└── data/                      抓取产物（作为空结果兜底）
```

## 触发方式

- 定时：每 6 小时（GitHub 用 UTC；对应北京时间 02:00 / 08:00 / 14:00 / 20:00）
- 手动：Actions 页面 → 「自动抓取并同步游戏数据」→ Run workflow

> GitHub 的定时工作流在仓库连续 60 天无提交后可能被自动停用；届时到 Actions 页面重新 Enable 即可。
