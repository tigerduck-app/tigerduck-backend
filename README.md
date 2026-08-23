<div align="center">
<a href="https://tigerduck.app/">
  <img width="2000" src="https://github.com/user-attachments/assets/cf6a1d18-a348-4b83-adfd-81c6dc82855f" alt="TigerDuck Backend Banner"/>
</a>
<br>

[![License](https://img.shields.io/github/license/tigerduck-app/tigerduck-backend?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Postgres](https://img.shields.io/badge/Postgres-17-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docs.docker.com/compose/)

**繁體中文** | [English](README.en.md)

</div>

## 總覽

TigerDuck Backend 是 [TigerDuck](https://github.com/tigerduck-app/tigerduck-app)（iOS / Android）的後端服務，跑在 `api.tigerduck.app`。負責五件事：

- 🔐 **帳號與認證（v3）** — NTUST SSO 登入驗證、JWT access + refresh rotation（盜用偵測滅族）、AES-256-GCM 憑證加密儲存、使用者裝置與 push token 管理
- 🔄 **使用者資料同步（v3）** — 課表 / 作業 / 設定 / 公告訂閱的多裝置同步：client 初始上傳 + per-user changelog 增量下行，伺服器端再定期代抓 Moodle 作業權威更新
- 📣 **公告管線** — 抓取 NTUST 各處室公告 → 去重 → LLM 分類（canonical_org / content_tags / importance）→ 訂閱比對 → 推播（匿名裝置與登入使用者雙軌）
- 📲 **推播服務** — 登入使用者走 `push_jobs` → `push_deliveries` 兩階段投遞（作業 / 課程提醒、公告、系統通知）；APNs Push-to-Start（iOS Live Activity）、FCM fan-out（Android）、bad-token 分類與清理
- ⏰ **排程同步** — server-side academic sync、推播 pipeline、提醒掃描、Live Activity token、retention 清理，全部走 APScheduler 單一 worker 在 FastAPI lifespan 內

服務本身刻意做得「**容器化、可重起、無狀態**」：所有狀態都在 Postgres，重啟 backend 容器不會丟事件、scheduler 也會接著做。

## 功能模組

### 🔐 帳號與認證（`server/auth/`）
- **登入** — App 端完成 NTUST SSO 後，後端用 Moodle token 驗證身分（不代打 SSO，避免觸發學校 IP rate limit）
- **Token** — 短效 JWT access token + 90 天 refresh token rotation；重放偵測直接撤銷整條 session 鏈與同裝置 session，60 秒內的單次 grace 重試容忍掉線 client
- **憑證保管** — NTUST 密碼以 AES-256-GCM + per-row AAD 加密存放（支援金鑰輪替），僅供伺服器端 Moodle token 刷新使用
- **裝置管理** — `/v3/devices` 註冊使用者裝置與 push token（standard / live_activity_update）；刪除裝置同步撤銷 session 並失效 token

### 🔄 使用者同步（`server/sync/`）
- **Client-authoritative 鏡像** — 課表（含 schedule_json）、作業快照、per-field overrides、設定文件（namespace + revision 樂觀並發）、公告訂閱與已讀狀態
- **增量同步** — per-user changelog：revision 嚴格遞增（row lock 保證 commit 順序 = revision 順序），client 用 `since_revision` 拉增量；過舊回 410 觸發 full sync
- **Retention** — changelog 定期壓縮清理，記錄 compacted revision 供 410 判斷

### 🎓 Server-Side Academic Sync（`server/syncjobs/`）
- **排程代抓** — `sync_policies`（admin 可調）× `sync_jobs`（登入時 provision）× `sync_runs`（審計）；executor 用 advisory lock + `FOR UPDATE SKIP LOCKED` 認領，多 worker 安全
- **密碼鐵律** — 每次 SSO 嘗試前先 durably commit attempt marker，密碼最多用一次；認證類失敗絕不重試，直接停用同步並排一筆 reauth 系統通知
- **作業鏡像** — 抓 Moodle 作業權威 upsert / 軟刪除，寫入 changelog 讓所有裝置同步
- **Pull-to-refresh** — `POST /v3/sync-jobs/run-now`（per-user cooldown，policy 停用時拒絕）

### 📣 公告（`server/bulletins/`）
- **scraper** — 從 NTUST 公告列表抓 HTML、解 metadata；TLS chain 是壞的所以走自簽 CA bundle 或 `verify=False`
- **dedup** — `content_hash` 去重（同 source、同 hash 視為 repost，標 `skipped` 不重發推播）
- **LLM 分類** — OpenAI-compatible API（預設指向 host 上的 [llama-server](https://github.com/ggml-org/llama.cpp)），輸出 `canonical_org` / `content_tags` / `importance` / `title_clean` / `summary` / `body_clean`
- **訂閱比對 + dispatch（雙軌）** — 匿名裝置比對 `BulletinSubscription` 走既有 `bulletin_dispatches`；登入使用者比對 `user_bulletin_subscriptions` 走 `bulletin_user_matches` + `push_jobs`。同一實體裝置登入後標記 `linked_user_id`，匿名管道跳過避免重複推播
- **狀態機** — `pending` → `processed` / `skipped` / `failed`；`failed` 也會在 attempts 未滿前回到 `pending` 重試

### 📲 推播（`server/push/`）
- **使用者推播 pipeline** — `push_jobs`（dedupe key 防重）→ materialize 成 per-token `push_deliveries` → APNs / FCM 投遞 → 聚合 `sent` / `partial_failed` / `failed`；round-based retry、stale lock 回收
- **提醒來源** — 作業提醒（due 前 24h / 2h，依使用者 notification 設定）、課程提醒（從 schedule_json × NTUST 節次表計算上課時間，預設前 10 分鐘）；繳交 / 退選 / 課表變更會取消過期提醒
- **APNs** — JWT 認證、Push-to-Start、Live Activity update / end
- **FCM** — 批次 fan-out、`UNREGISTERED` / `SENDER_ID_MISMATCH` 自動清 token
- **認證** — 所有 v3 路由走 `Authorization: Bearer <JWT>`；管理端點走 `X-Shared-Secret`；公告讀取開放

### ⏰ 排程
- **單一 worker** — APScheduler 跑在 lifespan 裡，副本數固定 1；多副本會 double-send（見 [`docs/scheduler.md`](docs/scheduler.md)）
- **tick 設計** — 公告 scrape / process / dispatch（匿名 + 使用者層）、sync jobs、推播 pipeline、作業 / 課程提醒掃描、retention 各自 interval trigger，互不阻塞；跨 worker 安全由 DB 鎖保證（advisory lock + `SKIP LOCKED`）

## 技術棧

| 層 | 用什麼 |
|---|---|
| Web | FastAPI 0.115 + Uvicorn + structlog（JSON log）|
| 管理介面 | FastAPI 供 API + React 19 / Vite 8 / Tailwind 4 / TypeScript 7 SPA |
| ORM | SQLAlchemy 2.x async + Alembic |
| DB | Postgres 17（容器化、internal-only network）|
| 排程 | APScheduler 3.x（IntervalTrigger）|
| 推播 | `aioapns`（APNs）、`google-auth` + `httpx`（FCM v1）|
| LLM | OpenAI-compatible client → llama-server (host)、`response_format: json_object` + JSON schema |
| 部署 | Docker Compose + nginx-proxy-manager 反代 |

## 系統架構

```
       公網                                            host (macOS / Linux)
   ┌──────────────┐                                ┌────────────────────────────┐
   │  iOS / 安卓   │ ── HTTPS ──▶ nginx-proxy ──▶   │  tigerduck-internal        │
   └──────────────┘              -manager  ────┐   │  (FastAPI + APScheduler)   │
                                               │   │           │                │
   ┌──────────────┐                            │   │           ├── APNs         │
   │ 管理者瀏覽器   │ ── HTTPS ──▶ cloudflared ──┼──▶│  tigerduck-portal          │
   └──────────────┘   (Zero Trust)             │   │  (FastAPI + React, :40010) │
                                               │   │           │                │
                                               │   │           ▼                │
                                               │   │  ┌────────────────┐        │
                                               │   │  │ tigerduck-db   │        │
                                               │   │  │ (Postgres 17)  │        │
                                               │   │  └────────────────┘        │
                                               │   │           ▲                │
                                               │   │           │                │
                                               │   │  ┌────────────────┐        │
                                               │   │  │ llama-server   │        │
                                               │   │  │ (native, Metal)│        │
                                               │   │  └────────────────┘        │
                                               │   └────────────────────────────┘
                                               │
                                               └── proxy-net 上同時掛 backend 和 portal
```

- **`tigerduck-db` 網路**：internal-only bridge，postgres 完全沒有外網路由
- **`proxy-net`**：與 nginx-proxy-manager 共用；backend + portal 都加入
- **`tigerduck-host`（僅 dev）**：`docker-compose.dev.yml` 開的橋接網路，讓 backend 40000 / portal 40010 能 publish 到 host port
- **llama-server**：native 跑在 host 上（Docker Desktop / macOS 沒辦法直通 Metal GPU），backend 透過 `host.docker.internal` 連回去
- **portal**：stateless 只讀的操作介面，本身不做 app-level 登入驗證（dev / prod 都一樣）；若要把關，前面套 Cloudflare Zero Trust Application 或其他 auth-proxy

## 取得與部署

### 系統需求

| 項目 | 需求 |
|---|---|
| 作業系統 | macOS / Linux（任何能跑 Docker Compose 的） |
| Docker | Docker Engine 24+ / Docker Desktop 4.30+ |
| Postgres | 17（由 compose 啟動，不需要 host 安裝） |
| llama-server | 1 顆能跑 instruct 小模型的機器（建議 ≤7B，OpenAI 相容 endpoint）|
| 反向代理 | nginx-proxy-manager 或同等物，把 `api.<your-domain>` 導到 `tigerduck-internal:40000` |

### 一鍵啟動

```bash
git clone https://github.com/tigerduck-app/tigerduck-backend.git
cd tigerduck-backend

# 1. 複製範本。預設是 development 模式（會自動載入 docker-compose.dev.yml）；
#    正式部署請把 TIGERDUCK_ENV 改成 production。
cp .env.example .env

# 2. 把 APNs 私鑰丟到 server/secrets/AuthKey_<KEY_ID>.p8（已 gitignored）

# 3. 啟動 stack（postgres + backend）
./start.sh                       # docker compose up -d --build + 跟 log

# 4. 健康檢查
docker compose exec backend curl -sS localhost:40000/health
```

### 操作腳本

四個腳本都會讀 `.env` 裡的 `TIGERDUCK_ENV`，遇到 `development` 就額外載入 `docker-compose.dev.yml`（把 backend 40000 / portal 40010 publish 到 host，並用一條非 internal 的橋接網路規避 proxy-net 在本機沒有 NPM 的問題）。換句話說：把 mode 寫在 `.env`，腳本自己會挑對的 compose 檔。

| 腳本 | 用途 |
|---|---|
| `./start.sh` | `docker compose up -d --build` 後印 mode/ports/skip-LLM 等狀態摘要 |
| `./stop.sh` | `docker compose down`（保留 volume） |
| `./logs.sh` | tail 指定 service（預設 backend） |
| `./clean-db.sh` | **危險** — 砍掉 postgres volume，整個資料重來。portal 本身是無狀態的，沒有 volume 要保留 |

### 管理介面（portal）

`tigerduck-portal` 是另一個 compose service，跟 backend 一起起來。前端是 `portal/web` 的 React SPA，由 Dockerfile 的 node build stage 打包成 `web/dist` 後由 FastAPI 靜態供應，`/api/*` 才是 JSON 端點。dev 模式 publish 到 `http://localhost:40010`，prod 想加登入的話前面套 cloudflared / Cloudflare Zero Trust（portal 本身不擋）。可以做的事：

- 看 stack 狀態（containers 走 docker engine UDS、postgres rows、LLM 連線、APNs/FCM secrets 在不在）
- 看每個 container 的 log（5 個 tab：Backend / DB / Portal / Android / Apple），每個 tab 自帶搜尋；Android / Apple 是針對 backend log 做關鍵字過濾
- 匯出 `tigerduck-export-<timestamp>.tar.gz`（含 `pg_dump --format=custom` + manifest）/ 匯入相同格式或單純的 `pg_dump` 檔
- 組合並發送自訂推播，支援單一裝置或命名裝置清單作為目標，含 payload 預覽與最近發送紀錄

詳細設計見 [`docs/portal-design.md`](docs/portal-design.md)。

### LLM（host 端）

backend 連的 LLM 是 host 上的 [llama-server](https://github.com/ggml-org/llama.cpp)：

```bash
# 範例（gemma-style instruct 小模型）
llama-server \
  --hf ggml-org/gemma-4-E4B-it-GGUF \
  --alias gemma-4-e4b-it \
  --host 0.0.0.0 --port 40001 \
  --api-key <your-key> \
  --json-schema '{}'
```

對應 `.env`：

```dotenv
TIGERDUCK_LLM_BASE_URL=http://host.docker.internal:40001/v1
TIGERDUCK_LLM_API_KEY=<your-key>
TIGERDUCK_LLM_MODEL=gemma-4-e4b-it
```

> ⚠️ 帶 reasoning channel（harmony 格式 / `<|channel>thought<channel|>`）的模型目前**不相容** — JSON parser 只剝 markdown fence、不認 channel marker。請挑純 instruct 模型。

macOS 上長期跑可以參考 `deploy/launchd/ai.tigerduck.llm.plist` 把 llama-server 包成 launchd 服務。

## 已日落 API

`/v1/*` 和 `/v2/*` 已全面日落，所有請求回 **410 Gone**。舊版 client 需更新 App 才能使用 `/v3` 端點。

## API 端點概覽（v3）

所有 v3 路由走 `Authorization: Bearer <JWT>`，除下方標註外。

### 認證

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/auth/login` | NTUST SSO 登入（Moodle token 驗證）→ access + refresh token | 無 |
| `POST` | `/v3/auth/refresh` | refresh token rotation（重放偵測滅族） | refresh token |
| `PATCH` | `/v3/auth/credentials` | 更新已儲存的加密憑證 | JWT |
| `POST` | `/v3/auth/logout` | 撤銷目前 session | JWT |

### 裝置

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/devices/register` | 使用者裝置 + push token 註冊 | JWT |
| `GET` | `/v3/devices` | 裝置列表 | JWT |
| `DELETE` | `/v3/devices/{id}` | 刪除裝置（連動撤銷 session、失效 token） | JWT |
| `PATCH` | `/v3/devices/{id}/preferences` | 更新同步偏好（sync_courses / colors / names / assignments） | JWT |

### 同步

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/sync/initial-upload` | 初始上傳本機資料（課表 / 作業 / 設定 / 訂閱） | JWT |
| `GET` | `/v3/sync?since_revision=N` | changelog 增量同步（過舊回 410） | JWT |
| `GET` | `/v3/sync/full` | 全量快照 | JWT |
| `GET` | `/v3/sync/revision` | 目前 revision 數字 | JWT |
| `POST` | `/v3/sync/courses/upload` | 上傳課表 | JWT |
| `DELETE` | `/v3/sync/courses` | 刪除全部課程 | JWT |
| `DELETE` | `/v3/sync/courses/{key}` | 刪除單一課程 | JWT |
| `POST` | `/v3/sync/assignments/upload` | 上傳作業 | JWT |
| `PATCH` | `/v3/sync/courses/{moodle_id}/override` | 課程顏色 / 名稱覆寫（outbox drain） | JWT |
| `PATCH` | `/v3/sync/assignments/{moodle_id}/override` | 作業狀態覆寫（outbox drain） | JWT |

### 課表 / 作業

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/v3/courses` | 課表列表 | JWT |
| `PUT` | `/v3/courses/{id}/override` | 課程覆寫（authoritative） | JWT |
| `PUT` | `/v3/courses/{id}/skipped-dates/{date}` | 新增停課日 | JWT |
| `DELETE` | `/v3/courses/{id}/skipped-dates/{date}` | 移除停課日 | JWT |
| `GET` | `/v3/assignments` | 作業列表 | JWT |
| `PUT` | `/v3/assignments/{id}/override` | 作業狀態覆寫（authoritative） | JWT |

### 設定

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/v3/settings` | 列出所有 namespace | JWT |
| `GET` | `/v3/settings/{namespace}` | 取得特定 namespace 設定 | JWT |
| `PUT` | `/v3/settings/{namespace}` | 更新設定（revision CAS，衝突回 409） | JWT |

### 公告

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/v3/bulletins` | 公告列表（cursor 分頁） | JWT |
| `GET` | `/v3/bulletins/{id}` | 公告詳情 | JWT |
| `GET` | `/v3/bulletins/taxonomy` | org / tag 標籤對照 | JWT |
| `GET` | `/v3/bulletin-subscriptions` | 訂閱規則列表 | JWT |
| `PUT` | `/v3/bulletin-subscriptions` | 批次覆寫訂閱規則 | JWT |
| `POST` | `/v3/bulletin-subscriptions` | 新增訂閱規則 | JWT |
| `PATCH` | `/v3/bulletin-subscriptions/{id}` | 更新訂閱規則（base_revision） | JWT |
| `DELETE` | `/v3/bulletin-subscriptions[/{id}]` | 刪除訂閱規則（可帶 id 或批次） | JWT |
| `GET` | `/v3/bulletin-states` | 公告已讀 / 星號 / 隱藏狀態 | JWT |
| `PUT` | `/v3/bulletin-states/{id}` | 設定單一公告狀態 | JWT |

### 排程 / Live Activity

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/schedule/sync` | 課表同步（驅動 Live Activity 排程） | JWT |
| `DELETE` | `/v3/schedule/sync` | 刪除排程資料 | JWT |
| `POST` | `/v3/live-activities/register` | 註冊 Live Activity update token | JWT |

### 伺服器代抓 / 管理

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/sync-jobs/run-now` | Pull-to-refresh 觸發伺服器代抓（cooldown） | JWT |
| `GET` | `/v3/admin/sync-policies` | 列出同步策略 | shared secret |
| `PATCH` | `/v3/admin/sync-policies/{job_type}` | 更新同步策略 | shared secret |

### 基礎

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/health` | liveness | 無 |
| `GET` | `/version` | 版本與 API base path | 無 |

## 開發

```bash
# host 端跑單元測試（不需要 docker）
uv sync
uv run pytest

# Alembic migration
uv run alembic revision --autogenerate -m "your change"
uv run alembic upgrade head
```

線上跑的 migration 由容器 entrypoint 自動執行（`entrypoint.sh`），平常不用手動。

## 專案架構

```
tigerduck-backend/
├── server/
│   ├── main.py                  # FastAPI entrypoint + lifespan（建 scheduler / LLM / push router）
│   ├── config.py                # pydantic-settings，所有設定走 TIGERDUCK_* env
│   ├── db.py / models.py        # SQLAlchemy async engine、DeviceRegistration
│   ├── security.py              # shared-secret dependency
│   ├── _ssl_compat.py           # OpenSSL 3 寬容模式（NTUST TLS chain 是壞的）
│   ├── auth/                    # v3 身分層：crypto（憑證加密）/ tokens / service / rate_limit / moodle / models
│   ├── sync/                    # v3 使用者同步：upload / changelog / serializers / retention / models
│   ├── syncjobs/                # 伺服器代抓：executor / credentials（密碼鐵律）/ moodle_client / assignments / provisioning
│   ├── routes/                  # v3：auth / user_devices / sync / academics / overrides / settings_docs / bulletins_feed / bulletins_v3 / sync_jobs / schedule_v3 / live_activities_v3
│   ├── push/                    # apns_client / fcm_client / router / pipeline（兩階段投遞）/ reminders / course_reminders / job_payloads
│   ├── scheduler/               # APScheduler runtime、dispatch、retention
│   ├── bulletins/               # scraper / dedup / matcher / dispatcher（匿名）/ user_dispatch（登入使用者）/ taxonomy
│   │   └── llm/                 # OpenAI-compatible client + prompt
│   ├── secrets/                 # APNs .p8（gitignored）
│   ├── migrations/              # Alembic
│   └── tests/                   # pytest（單元 + 整合）
├── portal/                      # 管理介面 — 另一個 FastAPI app（見 docs/portal-design.md）
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/                     # FastAPI：main / config / db (asyncpg) / logs / status / routes / static
│   └── web/                     # React 19 + Vite 8 + Tailwind 4 SPA（build 進 image 的 web/dist）
├── scripts/                     # backfill / seed 等一次性腳本
├── deploy/launchd/              # macOS launchd plist（llama-server 等 host-side service）
├── docker-compose.yml           # 基底（backend + postgres + portal，都掛 proxy-net）
├── docker-compose.dev.yml       # TIGERDUCK_ENV=development 時自動載入，publish ports + 換成 host bridge
├── _compose-files.sh            # 共用：根據 TIGERDUCK_ENV 算出要載入哪些 compose 檔
├── Dockerfile / entrypoint.sh   # backend 容器
├── start.sh / stop.sh / logs.sh / clean-db.sh
├── .env.example
└── pyproject.toml / uv.lock
```

## 貢獻

歡迎 PR 與 Issue。送出前請確認：
1. `uv run pytest` 全綠
2. 有改 schema 的話附上 alembic revision
3. 以 `feature/your-feature` 或 `fix/your-fix` 命名分支，PR 目標分支 `dev`
4. PR 描述寫清楚 user-visible 影響（會 ship 給 iOS / Android client 的部分）

## 授權

本專案採用 [GNU Affero General Public License v3.0](LICENSE) 授權，與 [tigerduck-app](https://github.com/tigerduck-app/tigerduck-app) 與 [tigerduck-app-android](https://github.com/tigerduck-app/tigerduck-app-android) 一致。
