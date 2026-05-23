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

TigerDuck Backend 是 [TigerDuck](https://github.com/tigerduck-app/tigerduck-app) iOS App 的後端服務，跑在 `api.tigerduck.app`。負責三件事：

- 📣 **公告管線** — 抓取 NTUST 各處室公告 → 去重 → LLM 分類（canonical_org / content_tags / importance）→ 訂閱比對 → 推播
- 📲 **推播服務** — APNs Push-to-Start（iOS Live Activity 啟動）、FCM fan-out（Android 推播）、bad-token 分類與清理
- ⏰ **排程同步** — Live Activity token 重送、課表 schedule sync、retention 清理，全部走 APScheduler 單一 worker 在 FastAPI lifespan 內

服務本身刻意做得「**容器化、可重起、無狀態**」：所有狀態都在 Postgres，重啟 backend 容器不會丟事件、scheduler 也會接著做。

## 功能模組

### 📣 公告（`server/bulletins/`）
- **scraper** — 從 NTUST 公告列表抓 HTML、解 metadata；TLS chain 是壞的所以走自簽 CA bundle 或 `verify=False`
- **dedup** — `content_hash` 去重（同 source、同 hash 視為 repost，標 `skipped` 不重發推播）
- **LLM 分類** — OpenAI-compatible API（預設指向 host 上的 [llama-server](https://github.com/ggml-org/llama.cpp)），輸出 `canonical_org` / `content_tags` / `importance` / `title_clean` / `summary` / `body_clean`
- **訂閱比對 + dispatch** — 比對每個裝置的 `BulletinSubscription` 規則，命中後送 APNs / FCM
- **狀態機** — `pending` → `processed` / `skipped` / `failed`；`failed` 也會在 attempts 未滿前回到 `pending` 重試

### 📲 推播
- **APNs** — JWT 認證、Push-to-Start、Live Activity update / end
- **FCM** — 批次 fan-out、`UNREGISTERED` / `SENDER_ID_MISMATCH` 自動清 token
- **shared secret** — 寫入類路由（裝置註冊、訂閱寫入）需驗 `X-Shared-Secret`，讀類路由（公告 list / detail / taxonomy）開放

### ⏰ 排程
- **單一 worker** — APScheduler 跑在 lifespan 裡，副本數固定 1；多副本會 double-send（見 [`docs/scheduler.md`](docs/scheduler.md)）
- **tick 設計** — scrape / process / dispatch / retention 各自 interval trigger，互不阻塞

## 技術棧

| 層 | 用什麼 |
|---|---|
| Web | FastAPI 0.115 + Uvicorn + structlog（JSON log）|
| ORM | SQLAlchemy 2.x async + Alembic |
| DB | Postgres 17（容器化、internal-only network）|
| 排程 | APScheduler 3.x（IntervalTrigger）|
| 推播 | `aioapns`（APNs）、`google-auth` + `httpx`（FCM v1）|
| LLM | OpenAI-compatible client → llama-server (host)、`response_format: json_object` + JSON schema |
| 部署 | Docker Compose + nginx-proxy-manager 反代 |

## 系統架構

```
       公網                                            host (macOS / Linux)
   ┌──────────────┐                              ┌────────────────────────────┐
   │  iOS / 安卓  │ ── HTTPS ──▶ nginx-proxy ──▶ │  tigerduck-internal        │
   └──────────────┘              -manager  ──┐   │  (FastAPI + APScheduler)   │
                                             │   │           │                │
   ┌──────────────┐                          │   │           ├── APNs        │
   │ 管理者瀏覽器 │ ── HTTPS ──▶ cloudflared ─┼──▶│  tigerduck-portal         │
   └──────────────┘   (Zero Trust)           │   │  (FastAPI + Jinja, :40010)│
                                             │   │           │                │
                                             │   │           ▼                │
                                             │   │  ┌────────────────┐        │
                                             │   │  │ tigerduck-db   │        │
                                             │   │  │ (Postgres 17)  │        │
                                             │   │  └────────────────┘        │
                                             │   │           ▲                │
                                             │   │           │                │
                                             │   │  ┌────────────────┐        │
                                             │   │  │ llama-server   │◀───────┘
                                             │   │  │ (native, Metal)│
                                             │   │  └────────────────┘
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
| `./clean-db.sh` | **危險** — 砍掉 postgres volume，整個資料重來（不影響 portal 的 `tigerduck_portal_data` volume） |

### 管理介面（portal）

`tigerduck-portal` 是另一個 compose service，跟 backend 一起起來。dev 模式 publish 到 `http://localhost:40010`，prod 想加登入的話前面套 cloudflared / Cloudflare Zero Trust（portal 本身不擋）。可以做的事：

- 看 stack 狀態（containers 走 docker engine UDS、postgres rows、LLM 連線、APNs/FCM secrets 在不在）
- 看每個 container 的 log（5 個 tab：Backend / DB / Portal / Android / Apple），每個 tab 自帶搜尋；Android / Apple 是針對 backend log 做關鍵字過濾
- 匯出 `tigerduck-export-<timestamp>.tar.gz`（含 `pg_dump --format=custom` + portal 的 SQLite + manifest）/ 匯入相同格式或單純的 `pg_dump` 檔
- 預留「自訂推播」分頁（TODO，先放空殼）

詳細設計見 [`docs/portal-design.md`](docs/portal-design.md)。

### LLM（host 端）

backend 連的 LLM 是 host 上的 [llama-server](https://github.com/ggml-org/llama.cpp)：

```bash
# 範例（gemma-style instruct 小模型）
llama-server \
  --hf ggml-org/gemma-4-E4B-it-GGUF \
  --alias gemma-4-E4B-it-GGUF \
  --host 0.0.0.0 --port 40006 \
  --api-key <your-key> \
  --json-schema '{}'
```

對應 `.env`：

```dotenv
TIGERDUCK_LLM_BASE_URL=http://host.docker.internal:40006/v1
TIGERDUCK_LLM_API_KEY=<your-key>
TIGERDUCK_LLM_MODEL=gemma-4-E4B-it-GGUF
```

> ⚠️ 帶 reasoning channel（harmony 格式 / `<|channel>thought<channel|>`）的模型目前**不相容** — JSON parser 只剝 markdown fence、不認 channel marker。請挑純 instruct 模型。

macOS 上長期跑可以參考 `deploy/launchd/ai.tigerduck.llm.plist` 把 llama-server 包成 launchd 服務。

## API 端點概覽（v2）

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/v2/health` | liveness | 無 |
| `POST` | `/v2/devices` | 裝置註冊（含 APNs token、`platform=apple` / `android`） | shared secret |
| `GET` | `/v2/bulletins` | 公告列表（cursor 分頁、newest first） | 無 |
| `GET` | `/v2/bulletins/{id}` | 公告詳情 | 無 |
| `GET` | `/v2/bulletins/taxonomy` | 取得 org / tag 標籤對照 | 無 |
| `GET/PUT` | `/v2/devices/{id}/subscriptions` | 訂閱規則讀寫 | shared secret |
| `POST` | `/v2/live-activities/start-tokens` | Live Activity push-to-start token 上報 | shared secret |
| `POST` | `/v2/schedule/sync` | 課表同步（驅動 Live Activity 排程） | shared secret |

`/v1/*` 保留為 deprecated alias，iOS 1.6.1 起改打 `/v2`。

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
│   ├── routes/                  # devices / schedule / bulletins / live_activities / debug
│   ├── push/                    # apns_client / fcm_client / payload / router
│   ├── scheduler/               # APScheduler runtime、dispatch、retention
│   ├── bulletins/               # scraper / dedup / matcher / dispatcher / taxonomy
│   │   └── llm/                 # OpenAI-compatible client + prompt
│   ├── secrets/                 # APNs .p8（gitignored）
│   ├── migrations/              # Alembic
│   └── tests/                   # pytest（單元 + 整合）
├── portal/                      # 管理介面 — 另一個 FastAPI app（見 docs/portal-design.md）
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── app/                     # main / config / db (SQLite) / auth / status / routes / templates / static
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

本專案採用 [GNU Affero General Public License v3.0](LICENSE) 授權，與 [tigerduck-app](https://github.com/tigerduck-app/tigerduck-app) 一致。
