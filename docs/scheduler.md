# Scheduler — single-worker invariant

## TL;DR

**TigerDuck backend 必須只能跑一個 APScheduler 副本。** 多副本（無論是同一機器多 worker 還是多機器多容器）會造成同一個任務在 tick 邊界被多個 worker 同時 fire，導致：

- APNs / FCM **重複推播**給同一裝置
- 公告 dispatch 邊界 race（同一筆 bulletin 被多個 worker 嘗試標 `processed`）
- Live Activity push token 被同時 expire 與 refresh

## 設計

排程跑在 FastAPI 的 lifespan 裡，跟 web app 同一個 process：

```python
# server/scheduler/runtime.py
scheduler = AsyncIOScheduler(timezone="UTC")
scheduler.add_job(scrape, IntervalTrigger(seconds=settings.bulletin_scrape_interval_seconds), ...)
scheduler.add_job(process, IntervalTrigger(seconds=settings.bulletin_process_interval_seconds), ...)
scheduler.add_job(dispatch, IntervalTrigger(seconds=settings.bulletin_dispatch_interval_seconds), ...)
scheduler.add_job(retention, IntervalTrigger(hours=settings.bulletin_retention_interval_hours), ...)
scheduler.start()
```

選 in-process / lifespan-managed 而不是 sidecar service 的理由：

1. **單一資料庫 connection pool** — scheduler 跟 web handler 共用同一個 SQLAlchemy engine，不會兩邊互打。
2. **生命週期綁定** — 容器停就停、容器起就起，不會出現「web 起來了但 scheduler 還沒接上」這種半開狀態。
3. **零外部 broker** — 不需要 Redis / RabbitMQ；對一個校內服務量級來說，APScheduler 就夠了。

## 為什麼不能水平擴展

APScheduler 預設不做分散式鎖（`MemoryJobStore`）。即使換成 `SQLAlchemyJobStore` 也只解了「任務定義不會重複登記」，**不解「同一個 trigger fire 時哪個 worker 該執行」**——所有副本都會自己看到 next_run_time 到了，然後同時跑。

唯一安全的水平擴展路徑是把排程切出去（Celery beat + 多個 worker，或換成 cron + 一次性 CLI），但目前負載不需要這個複雜度。

## Deployment invariant

- `docker-compose.yml` 內 `backend` service **必須維持 replica = 1**
- 如果以後上 k8s / Nomad：deployment 必須鎖死 `replicas: 1` + `strategy: Recreate`（不要 RollingUpdate，因為滾動更新會短暫存在 2 副本）
- 如果以後出現「我們需要多副本」的需求：先把 scheduler 抽成獨立的 1-replica deployment，web 才能水平擴展

## 自我檢查

部署後最簡單的驗證：`docker compose ps` 確認 `tigerduck-internal` 只有 1 個 container 在跑。Backend log 內 `scheduler.start` 事件每次重啟也只應該出現一次。
