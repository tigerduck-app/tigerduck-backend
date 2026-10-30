<div align="center">
<a href="https://tigerduck.app/">
  <img width="2000" src="https://github.com/user-attachments/assets/cf6a1d18-a348-4b83-adfd-81c6dc82855f" alt="TigerDuck Backend Banner"/>
</a>
<br>

[![License](https://img.shields.io/github/license/tigerduck-app/tigerduck-backend?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.142-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Postgres](https://img.shields.io/badge/Postgres-17-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docs.docker.com/compose/)

**繁體中文** | [English](README.en.md)

</div>

## 總覽

TigerDuck Backend 是 [TigerDuck](https://github.com/tigerduck-app/tigerduck-app)（iOS / Android）的後端服務，跑在 `api.tigerduck.app`。負責五件事：

- 🔐 **帳號與認證（v3）** — NTUST SSO 登入驗證、JWT access + refresh rotation（重送舊 token 不會登出裝置）、AES-256-GCM 憑證加密儲存、使用者裝置與 push token 管理
- 🔄 **使用者資料同步（v3）** — 課表 / 作業 / 設定的多裝置同步：client 初始上傳 + per-user changelog 增量下行，伺服器端再定期代抓 Moodle 作業權威更新
- 📣 **公告管線** — 抓取 NTUST 各處室公告 → 去重 → LLM 分類（canonical_org / content_tags / importance）→ 訂閱比對 → 推播（匿名裝置與登入使用者雙軌）
- 📲 **推播服務** — 登入使用者走 `push_jobs` → `push_deliveries` 兩階段投遞（作業 / 課程提醒、公告、系統通知）；APNs Push-to-Start（iOS Live Activity）、FCM fan-out（Android）、bad-token 分類與清理
- ⏰ **排程同步** — server-side academic sync、推播 pipeline、提醒掃描、Live Activity token、retention 清理，全部走 APScheduler 單一 worker 在 FastAPI lifespan 內

服務本身刻意做得「**容器化、可重起、無狀態**」：所有狀態都在 Postgres，重啟 backend 容器不會丟事件、scheduler 也會接著做。

## 功能模組

### 🔐 帳號與認證（`server/auth/`）
- **登入** — App 自行完成 NTUST SSO，再把 Moodle token 交給後端驗證身分，避免觸發學校的 IP rate limit。後端不會拿密碼登入：沒帶 Moodle token 的登入回 `400 moodle_token_required`，一併送來的密碼一律忽略。連不上 Moodle，或 Moodle 回的錯誤與 token 無關（例如維護模式）時回 `503 moodle_unreachable`，只有 Moodle 拒絕的 token 才回 `401 moodle_verification_failed`。不論有沒有帶 token，每次嘗試都計入依 IP 與學號的登入限流
- **Token** — 短效 JWT access token + 90 天 refresh token rotation。被 rotation 換掉的 token 再送來時，只要它換出的 session 都還沒用過，就每次再 rotation 一次（最多保留最新的 16 組），直到 token 本身過期；這些 session 最先被使用的那組會讓其餘失效，所以晚到的重複 refresh 不會讓 client 手上的那組 token 失效。已有一組用過之後，舊 token 回 `401 session_revoked`，不撤銷任何 session
- **憑證保管** — 只存 Moodle token，以 AES-256-GCM + per-row AAD 加密（支援金鑰輪替），供伺服器端代抓使用；不保存 NTUST 密碼
- **裝置管理** — `/v3/devices` 註冊使用者裝置與 push token（standard / live_activity_update）；刪除裝置同步撤銷 session 並失效 token；裝置回報語系與硬體型號，並各自帶有同步與推播開關（作業提醒、即時動態、公告推播等）

### 🔄 使用者同步（`server/sync/`）
- **Client-authoritative 鏡像** — 課表（含 schedule_json）、作業快照、per-field overrides、設定文件（namespace + revision 樂觀並發）、公告已讀狀態
- **增量同步** — per-user changelog：revision 嚴格遞增（row lock 保證 commit 順序 = revision 順序），client 用 `since_revision` 拉增量；過舊回 410 觸發 full sync
- **Retention** — changelog 定期壓縮清理，記錄 compacted revision 供 410 判斷

### 🎓 Server-Side Academic Sync（`server/syncjobs/`）
- **排程代抓** — `sync_policies`（admin 可調）× `sync_jobs`（登入時 provision）× `sync_runs`（審計）；executor 用 advisory lock + `FOR UPDATE SKIP LOCKED` 認領，多 worker 安全
- **憑證失效** — 代抓只用已存的 Moodle token，不會拿密碼重新登入。token 遺失或被拒屬於認證類失敗，絕不重試：直接停用該使用者所有同步工作，並排一筆 reauth 系統通知；App 用 `PATCH /v3/auth/credentials` 送新 token 後同步自動恢復
- **學校端故障** — Moodle 或 SSO 故障、Moodle 限流時退避重試；重試用完後隔一個 policy 間隔（預設 8 小時）再試，不會停在 `failed`。只有我們這邊的錯誤（未預期的例外、資料庫錯誤、worker 執行到一半中止）才會讓同步工作停在 `failed`，直到 portal 的 Retry all failed 重新排入
- **作業鏡像** — 抓 Moodle 作業權威 upsert / 軟刪除，寫入 changelog 讓所有裝置同步
- **助教課程** — 只抓使用者能繳交作業的課程（`mod/assign:submit`）：擔任助教的課程不抓，下次代抓會軟刪除已存的作業並撤回其提醒
- **繳交狀態** — 每次作業同步後，向 Moodle 查詢時間窗內（預設 48 小時）到期作業的繳交狀態；新繳交的作業撤回待送提醒、結束倒數中的即時動態
- **Pull-to-refresh** — `POST /v3/sync-jobs/run-now`（per-user cooldown，policy 停用時拒絕）

### 📣 公告（`server/bulletins/`）
- **scraper** — 從 NTUST 公告列表抓 HTML、解 metadata；NTUST 送的 TLS chain 常缺中繼憑證，所以用 certifi 的根憑證加上 repo 內附的 NTUST 中繼憑證驗證，其他缺的中繼憑證經 AIA 補上
- **dedup** — `content_hash` 去重（同 source、同 hash 視為 repost，標 `skipped` 不重發推播）
- **LLM 分類** — OpenAI-compatible API（預設指向 host 上的 [llama-server](https://github.com/ggml-org/llama.cpp)），輸出 `canonical_org` / `content_tags` / `importance` / `title_clean` / `summary` / `body_clean`
- **訂閱比對 + dispatch（雙軌）** — 匿名裝置比對 `BulletinSubscription` 走既有 `bulletin_dispatches`；登入使用者比對 `user_bulletin_subscriptions`（規則屬於各裝置，不在 TigerSync 同步範圍）走 `bulletin_user_matches` + `push_jobs`，只推給規則命中、且沒關閉公告推播的裝置。同一實體裝置登入後標記 `linked_user_id`，匿名管道跳過避免重複推播
- **狀態機** — `pending` → `processed` / `skipped` / `failed`；`failed` 也會在 attempts 未滿前回到 `pending` 重試

### 📲 推播（`server/push/`）
- **使用者推播 pipeline** — `push_jobs`（dedupe key 防重）→ materialize 成 per-token `push_deliveries` → APNs / FCM 投遞 → 聚合 `sent` / `partial_failed` / `failed`；round-based retry、stale lock 回收
- **tick** — 每 5 秒一次，每次把所有到期的 job 清完。Live Activity 的 start / end 以 priority 10 排在其他 job（預設 100）前面
- **提醒來源** — 作業提醒（提前時間取自 notification 設定文件，只送給開啟作業提醒同步的 iPhone / iPad；使用者忽略作業的課程不送）、課程提醒（從 schedule_json × NTUST 節次表計算上課時間，預設前 10 分鐘，只送給 iPhone / iPad；Android 自己排）；繳交 / 退選 / 課表變更會取消過期提醒
- **學期起訖與校曆假日** — 課程提醒送給兩個 App 當天顯示的學期的課程：最近開學的已公布學期（`semester_terms`）；一個學期都沒公布時，照舊送給使用者最新同步的學期。課程提醒與課堂類 Live Activity（`classPreparing` / `inClass`）在沒有任何已公布學期上課的日子不送（從開學日到結業日，兩端都算，與兩個 App 相同），在校曆假日（`academic_holidays`）也不送，除非使用者對該假日開了「還要上課？」例外（`user_holiday_overrides`）。例外只放回假日，不會放回學期外的日子。Live Activity 由 App 提前排入，所以在**送出當下**才判斷：事後公布的學期或假日、事後切換的例外，都會作用在已排好的 job 上，被擋下的記為 `cancelled`、`last_error` 為 `outside_term` 或 `holiday`。作業類 Live Activity 兩者都不受影響
- **課程異動** — 學校的停課與教室、上課、下課時間異動（`course_changes`，在 portal 輸入）依日期與節次套用到 iPhone / iPad 的課程提醒與課堂類 Live Activity：異動套用到從它某一節開始的那段課。掃描不為停課的那段課排提醒，依新的上課時間排定提醒，並把新教室寫進內文。第一次送出前 pipeline 再查一次：之後才停課或改時間的提醒、之後才停課的 Live Activity 記為 `cancelled`、`last_error` 為 `class_changed`；Live Activity 換上新的教室與時間，上課時間延後時等到同樣晚才送。查不到結果時取消，記為 `unverified_before_send`。Android 自己排，並自行套用異動 feed
- **同一時刻只響一次**（預設關閉）— 設定 `push_countdown_dedupe_window_seconds`（300）後，作業的 `assignmentUrgent` Live Activity 啟動推播在作業提醒的發送時間前這麼多秒內送到某台 iPhone，且那台的 App 已註冊這個 Live Activity 時，這則提醒就不再送往那台：啟動推播的提示已經響過。提醒緊接在啟動推播之後、且發送前檢查的寬限時間還夠時，會等一次（一分鐘）讓 App 註冊；到時仍未註冊的 iPhone 照常收到提醒。所有裝置都略過的提醒記為 `cancelled`、`last_error` 為 `countdown_start_sent`；設為 `0` 即關閉
- **文案在地化** — 作業提醒與 reauth 通知的文字在投遞時依每台裝置的語系，從 app-translation 產生
- **重新登入提醒** — TigerSync 開著、三天內出現過，卻一直在沒有可用 session 的情況下開啟 App 的裝置，會收到一則只送給它的 `reauth_required` 推播（`reason: "session"`）：最新的 session 結束（因登出以外的原因撤銷，或過期）一小時後仍沒有有效 session，或有效 session 已 24 小時沒更新。每台裝置七天最多一次，session 結束超過 28 天就不再提醒，只在學校時間 09:00 到 21:00 送出，送出前會再檢查一次
- **通知分組** — iOS 的 `thread-id` 分 `course` / `assignment` / `other` 三組。課程與作業提醒不帶共用的 collapse id，所以會疊在一起，不會互相取代
- **APNs** — JWT 認證、Push-to-Start、Live Activity update / end；連線送完後保持開著（TCP keepalive），單次發送上限 15 秒
- **FCM** — 批次 fan-out、`UNREGISTERED` / `SENDER_ID_MISMATCH` 自動清 token；sync trigger 用 normal priority、TTL 12 小時；每個 HTTP request 上限 10 秒
- **認證** — 所有 v3 路由走 `Authorization: Bearer <JWT>`；管理端點只在內部的 admin port（`:9001`）上提供，並檢查 `X-Push-Token`；公告讀取開放

推播的完整流程圖在 [`docs/push-notification-flows.drawio`](docs/push-notification-flows.drawio)（用 draw.io / diagrams.net 開啟）。

### ⏰ 排程
- **單一 worker** — APScheduler 跑在 lifespan 裡，副本數固定 1；多副本會 double-send
- **tick 設計** — 公告 scrape / process / dispatch（匿名 + 使用者層）、sync jobs、推播 pipeline、作業 / 課程提醒掃描、重新登入提醒掃描、retention 各自 interval trigger，互不阻塞；跨 worker 安全由 DB 鎖保證（advisory lock + `SKIP LOCKED`）

## 技術棧

| 層 | 用什麼 |
|---|---|
| Web | FastAPI 0.142 + Uvicorn + structlog（JSON log）|
| 管理介面 | FastAPI 供 API + React 19 / Vite 8 / Tailwind 4 / TypeScript 7 SPA |
| ORM | SQLAlchemy 2.x async + Alembic |
| DB | Postgres 17（容器化、internal-only network）|
| 排程 | APScheduler 3.x（IntervalTrigger）|
| 推播 | `aioapns`（APNs）、`firebase-admin`（FCM）|
| LLM | OpenAI-compatible client → llama-server (host)、`response_format: json_object` + JSON schema |
| 部署 | Docker Compose + nginx-proxy-manager 反代 |
| 監控 | Prometheus + Grafana + Loki（Grafana Alloy）、postgres-exporter、sql_exporter |

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
- **`tigerduck-host`（僅 dev）**：`docker-compose.dev.yml` 開的橋接網路，讓 backend 40000 / portal 40010 / Grafana 40020 / Prometheus 40021 能 publish 到 host port
- **`tigerduck-monitoring`**：internal-only bridge，給 Prometheus、Loki 和它們收資料的對象用；監控服務裡只有 Grafana 掛 `proxy-net`，見[監控](#監控grafana)
- **llama-server**：native 跑在 host 上（Docker Desktop / macOS 沒辦法直通 Metal GPU），backend 透過 `host.docker.internal` 連回去
- **portal**：stateless 的操作介面，正式環境放在 Cloudflare Access 後面。每個 `/api/*` request 都要帶 Access 加上的 `Cf-Access-Jwt-Assertion`，portal 驗過簽章、issuer 與 audience 才放行，否則回 403；只有 `TIGERDUCK_ENV=development` 不檢查

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
git clone --recurse-submodules https://github.com/tigerduck-app/tigerduck-backend.git
cd tigerduck-backend

# 1. 複製範本。預設是 development 模式（會自動載入 docker-compose.dev.yml）；
#    正式部署請把 TIGERDUCK_ENV 改成 production。
cp .env.example .env

# 2. 把 APNs 私鑰丟到 server/secrets/AuthKey_<KEY_ID>.p8（已 gitignored）
#    TLS pin 文件的簽章金鑰放 server/signing/tls-pins-<key_id>.pem（只掛進 backend）

# 3. 啟動整個 stack（postgres、backend、portal、監控）
./start.sh                       # docker compose up -d --build，再印出狀態摘要

# 4. 健康檢查
docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:40000/health').read().decode())"
```

### 操作腳本

四個腳本都會讀 `.env` 裡的 `TIGERDUCK_ENV`，遇到 `development` 就額外載入 `docker-compose.dev.yml`（把 backend 40000 / portal 40010 / Grafana 40020 / Prometheus 40021 publish 到 host，並用一條非 internal 的橋接網路規避 proxy-net 在本機沒有 NPM 的問題）。換句話說：把 mode 寫在 `.env`，腳本自己會挑對的 compose 檔。

| 腳本 | 用途 |
|---|---|
| `./start.sh` | `docker compose up -d --build` 後印 mode/ports/skip-LLM 等狀態摘要 |
| `./stop.sh` | `docker compose down`（保留 volume） |
| `./logs.sh` | tail 指定 service（預設 backend） |
| `./clean-db.sh` | **危險** — 砍掉 postgres volume，整個資料重來。portal 本身是無狀態的，沒有 volume 要保留 |

### 管理介面（portal）

`tigerduck-portal` 是另一個 compose service，跟 backend 一起起來。前端是 `portal/web` 的 React SPA，由 Dockerfile 的 node build stage 打包成 `web/dist` 後由 FastAPI 靜態供應，`/api/*` 才是 JSON 端點。dev 模式 publish 到 `http://localhost:40010`，prod 要放在 Cloudflare Access 後面：portal 驗每個 `/api/*` request 的 Access JWT，用的是 Grafana 那兩個 `TIGERDUCK_CF_ACCESS_*` 值（見[監控](#監控grafana)的正式環境設定），沒設的話 API 一律回 403。可以做的事：

- 看 stack 狀態（containers 走 docker engine UDS、postgres rows、LLM 連線、APNs/FCM secrets 在不在）
- 看每個 container 的 log（6 個 tab：Backend / Announcement / DB / Portal / Android / Apple），每個 tab 自帶搜尋；Announcement / Android / Apple 是針對 backend log 做關鍵字過濾
- 匯出 `tigerduck-export-<timestamp>.tar.gz`（含 `pg_dump --format=custom` + manifest）/ 匯入相同格式或單純的 `pg_dump` 檔
- 組合並發送自訂推播，支援單一裝置或命名裝置清單作為目標，含 payload 預覽與最近發送紀錄
- 逐裝置檢視同步開關與同步項目、公告訂閱、硬體型號與排隊中的推播；Tests 區可送出測試用的 reauth 通知與即時動態
- 撰寫與修改公告
- 註銷裝置（撤銷 session、失效 push token）、管理裝置清單；Deregister 頁刪除學生與他的所有資料
- 在 Moodle 頁暫停與恢復伺服器同步、重新排入失敗的同步工作、立刻跑推播 tick、取消某位學生排隊中的推播、送出同步觸發推播
- 編輯學期起訖與校曆假日、轉址連結和 TLS pin
- 顯示 Access token 裡的登入者 email 與 Sign out 連結，按下會結束其 Access session；dev 模式沒有 Access session，連結不會出現

### 監控（Grafana）

Grafana、Prometheus、Loki 跟整個 stack 一起起來，設定都在 `monitoring/`。portal 不留歷史，這套補上：誰在線上、同步與推播的健康狀況隨時間的變化、request 量、所有 container 的 log。

| | Development | Production |
|---|---|---|
| Grafana | `http://localhost:40020`（以及 `./start.sh` 印出的 LAN IP），不用登入 | `https://portal.<your-domain>/grafana/`，由 Cloudflare Access 登入 |
| Prometheus | `http://localhost:40021` | 只在內部網路，從 Grafana 查 |

Dashboard 都在 **TigerDuck** 資料夾，每張右上角的 TigerDuck 選單可以互相切換。

| Dashboard | 內容 |
|---|---|
| Userbase | 已註冊使用者（以學號計）與裝置（iPhone + iPad / macOS / Android），對照 14 天內活躍的數量，折線圖上方附目前數字。由資料庫重建，歷史可回溯到上線那天 |
| Overview | 線上 / 活躍裝置、每個 scrape target 的健康、request 量與錯誤、推播延遲、逾期的同步工作、上次公告爬取時間、最近的錯誤 |
| Devices | portal Devices 分頁的所有內容（裝置表、平台 / App / OS / 型號分布、清單），加上線上（N 分鐘內出現過）與活躍（14 天內出現過）裝置的趨勢。可依平台、App 版本、兩個時間窗篩選，並有搜尋框 |
| Moodle sync | 同步政策、每位學生的同步工作、執行次數與耗時、失敗原因、需要重新登入的 NTUST 帳號、每位使用者的活動紀錄 |
| Push | push_jobs 佇列、各 provider 的投遞、APNs / FCM 發送耗時、失敗、自訂推播 |
| Bulletins | 爬取新鮮度、LLM 待處理與失敗、各單位新公告、配對與推播 |
| Logs | 所有 container 的 log（Loki，保留 30 天），可依 service 與 level 篩選、全文搜尋 |
| Backend API | 各 route 與狀態碼的 request、延遲、排程 job 的執行次數與耗時、SQLAlchemy pool、CPU 與記憶體 |
| Postgres | 依狀態與來源的連線數、即時 session、lock、transaction、資料表大小 |

數據來源：

- **postgres-exporter**：Postgres server 本身的統計。
- **sql-exporter**：每 30 秒用 SQL 取一次 app 層的數字（線上裝置、推播佇列、同步工作⋯），讓它們有歷史。查詢都在 `monitoring/sql-exporter/collectors/`。
- **Backend `:9000/metrics`**（`server/metrics.py`）：HTTP、排程、DB pool、APNs / FCM / LLM 耗時。這個 port 只綁在 backend 於內部 `tigerduck-monitoring` 網路上的位址（`TIGERDUCK_METRICS_HOST`），`proxy-net` 上的東西都連不到；對外的 `:40000` 永遠不回應 `/metrics`。
- **Grafana 的 SQL datasource**：表格類 panel 直接讀資料庫，用唯讀的 `tigerduck_monitor` role；這個 role 由一次性的 `monitor-role` service 在每次 `./start.sh` 時建立或更新。它有自己的密碼（`TIGERDUCK_MONITOR_DB_PASSWORD`，沒設的話 `./start.sh` 會產生一組寫進 `.env`），並有 30 秒的 statement timeout，重的查詢不會卡住 migration 要等的 lock。
- **Alloy → Loki**：透過 docker socket 收每個 container 的輸出。

從 Prometheus 畫的圖從第一次部署監控那天開始累積；SQL panel 則是資料表裡有什麼就顯示什麼。

**正式環境設定**

1. 在 `.env` 設 `TIGERDUCK_CF_ACCESS_TEAM_DOMAIN` 與 `TIGERDUCK_CF_ACCESS_AUD`（portal 那個 Access application 的 AUD tag）。portal 的 `/api/*` 也用這兩個值驗同一個 token。Grafana 沒有自己的登入：它讀 Access 加在每個 request 上的 `Cf-Access-Jwt-Assertion` token，驗過簽章、issuer 與 audience 後直接登入。沒經過 Access 就到達 Grafana 的 request（LAN 直連 NPM、`proxy-net` 上的其他 container）會被拒絕；這兩個值沒設的話，誰都進不去。portal host 不是 `portal.tigerduck.app` 的話，另外設 `TIGERDUCK_GRAFANA_ROOT_URL=https://<portal host>/grafana/`。
2. 把 portal host 的 `/grafana` 導到 `tigerduck-grafana:3000`，保留 `/grafana` 前綴：
   - nginx-proxy-manager：在 portal 的 proxy host 加一個 Custom Location `/grafana` → `http`、`tigerduck-grafana`、`3000`，host 後面不要加路徑。要用 Grafana Live 的話，在該 location 的 advanced config 加上 `proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`
   - cloudflared 直接指向 portal 的話：在 portal 那條規則之前加一條 ingress rule，`hostname: portal.<your-domain>`、`path: ^/grafana`、`service: http://tigerduck-grafana:3000`。
3. Cloudflare Access 不用改：portal host 上的 application 已經涵蓋 `/grafana`（只要它的 path 留空）。從 portal 登出也會一併登出 Grafana。

**修改方式**

- `monitoring/grafana/dashboards/` 裡的 dashboard 由 `monitoring/grafana/build_dashboards.py` 產生，多個 panel 共用的篩選與查詢只定義一次；無法在 UI 直接存檔。改那支腳本後執行 `python3 monitoring/grafana/build_dashboards.py`（只需要 Python 3），Grafana 30 秒內會讀到新的 JSON。在 UI 試出來的 panel 要搬回腳本裡。在 UI 從頭新建的 dashboard 會存在 Grafana 的 volume 裡。
- 想替 app 數字加歷史：在 `monitoring/sql-exporter/collectors/*.collector.yml` 加一條查詢。
- Prometheus 會在 `prometheus.yml` 改動後 30 秒內自動重讀，Grafana 的 dashboard 也是。`monitoring/` 底下其他設定只在啟動時讀：改了之後重啟對應的 service，例如 `docker compose restart grafana`（或 `sql-exporter`、`loki`、`alloy`）。
- Prometheus 與 Loki 都保留 30 天。
- 版本都已鎖定：每個監控 image 都以 tag + digest 固定，Grafana 的 plugin 版本寫在 `GF_PLUGINS_PREINSTALL`，重啟不會拉新版。升級方式寫在 `docker-compose.yml` 的註解裡。

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
| `POST` | `/v3/auth/refresh` | refresh token rotation | refresh token |
| `PATCH` | `/v3/auth/credentials` | 更新已儲存的加密憑證 | JWT |
| `POST` | `/v3/auth/logout` | 撤銷目前 session | JWT |

### 裝置

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/devices/anonymous` | 未登入裝置 + push token 註冊，供匿名公告推播與自訂推播 | 無 |
| `POST` | `/v3/devices/register` | 使用者裝置 + push token 註冊 | JWT |
| `GET` | `/v3/devices` | 裝置列表 | JWT |
| `DELETE` | `/v3/devices/{id}` | 刪除裝置（連動撤銷 session、失效 token） | JWT |
| `PATCH` | `/v3/devices/{id}/preferences` | 更新裝置偏好（同步項目、作業提醒 / 即時動態同步、公告與伺服器推播開關、語系） | JWT |

### 同步

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/sync/initial-upload` | 初始上傳本機資料（課表 / 作業 / 覆寫 / 設定） | JWT |
| `GET` | `/v3/sync?since_revision=N` | changelog 增量同步（過舊回 410） | JWT |
| `GET` | `/v3/sync/full` | 全量快照 | JWT |
| `GET` | `/v3/sync/revision` | 目前 revision 數字 | JWT |
| `POST` | `/v3/sync/courses/upload` | 上傳課表 | JWT |
| `DELETE` | `/v3/sync/courses` | 刪除全部課程 | JWT |
| `DELETE` | `/v3/sync/courses/{key}` | 刪除單一課程 | JWT |
| `POST` | `/v3/sync/assignments/upload` | 上傳作業 | JWT |
| `PATCH` | `/v3/sync/courses/{moodle_id}/override` | 課程顏色 / 名稱 / 忽略作業覆寫（outbox drain；伺服器沒有該課程回 404 `course_not_found`） | JWT |
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
| `GET` | `/v3/settings?namespaces=a,b` | 取得多個 namespace 的設定 | JWT |
| `GET` | `/v3/settings/{namespace}` | 取得特定 namespace 設定 | JWT |
| `PUT` | `/v3/settings/{namespace}` | 更新設定（revision CAS，衝突回 409） | JWT |

### 公告

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/v3/bulletins` | 公告列表（cursor 分頁） | 無 |
| `GET` | `/v3/bulletins/{id}` | 公告詳情 | 無 |
| `GET` | `/v3/bulletins/taxonomy` | org / tag 標籤對照 | 無 |
| `GET` | `/v3/bulletin-subscriptions` | 此裝置的訂閱規則列表 | JWT |
| `PUT` | `/v3/bulletin-subscriptions` | 批次覆寫此裝置的訂閱規則 | JWT |
| `POST` | `/v3/bulletin-subscriptions` | 新增此裝置的訂閱規則 | JWT |
| `PATCH` | `/v3/bulletin-subscriptions/{id}` | 更新訂閱規則（base_revision） | JWT |
| `DELETE` | `/v3/bulletin-subscriptions/{id}` | 刪除訂閱規則（base_revision） | JWT |
| `GET` | `/v3/bulletin-states` | 公告已讀 / 星號 / 隱藏狀態 | JWT |
| `PUT` | `/v3/bulletin-states/{id}` | 設定單一公告狀態 | JWT |

### 校曆

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/v3/calendar/semesters` | 學期起訖與校曆假日（ETag） | 無 |
| `GET` | `/v3/calendar/course-changes?term=` | 一個學期的全部課程異動（預設為目前學期），各附伺服器算出的上課日（ETag） | 無 |
| `GET` | `/v3/sync/holiday-overrides` | 使用者的假日例外（「還要上課？」） | JWT |
| `PUT` | `/v3/sync/holiday-overrides/{holiday_id}` | 設定某個假日是否照常送課程通知 | JWT |

### 課程目錄

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/v3/course-catalog/{semester}?language=zh\|en` | 一個學期的完整課程、系所與通識向度，每兩小時從 NTUST 更新（gzip、ETag） | JWT |

### 排程 / Live Activity

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/schedule/sync` | 課表同步（驅動 Live Activity 排程；課堂類在送出時才依學期起訖、校曆假日與課程異動判斷） | JWT |
| `DELETE` | `/v3/schedule/{source_id}` | 取消此裝置某個 source 待送的排程 | JWT |
| `POST` | `/v3/live-activities/register` | 註冊 Live Activity update token | JWT |

### 伺服器代抓 / 管理

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `POST` | `/v3/sync-jobs/run-now` | Pull-to-refresh 觸發伺服器代抓（cooldown） | JWT |
| `GET` | `/v3/admin/sync-policies` | 列出同步策略（只在 admin port） | shared secret |
| `PATCH` | `/v3/admin/sync-policies/{job_type}` | 更新同步策略（只在 admin port） | shared secret |
| `POST` | `/push-tick` | 立刻跑一次推播 tick（只在 admin port，portal 呼叫） | shared secret |
| `GET` | `/admin/links` | 列出轉址連結與上次檢查結果（只在 admin port） | shared secret |
| `POST` | `/admin/links` | 新增轉址連結（只在 admin port） | shared secret |
| `PATCH` | `/admin/links/{name}` | 修改、啟用或停用轉址連結（只在 admin port） | shared secret |
| `POST` | `/admin/links/{name}/test` | 立刻檢查轉址連結（只在 admin port） | shared secret |
| `POST`/`PUT` | `/tls-pins/*` | TLS pin 的增刪、監看主機、到期日、auto_publish、立即發布或重簽（只在 admin port，portal 呼叫） | shared secret |

### 基礎

| Method | Path | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/health` | liveness | 無 |
| `GET` | `/ready` | readiness：排程器在跑且資料庫連得上，否則 503 | 無 |
| `GET` | `/ping` | 不帶版本前綴的探測 | 無 |
| `GET` | `/version` | 版本與 API base path | 無 |
| `GET` | `/links/{name}` | 307 轉到連結的目標，目標在 portal 編輯 | 無 |
| `GET` | `/.well-known/tigerduck/tls-pins.json` | App 的 TLS pin 簽章文件（Ed25519） | 無 |

## 開發

```bash
# host 端跑測試。不需要起 stack，但要有一個 Postgres：預設連 localhost:5432
# （tigerduck / tigerduck），測試自己建 tigerduck_test 資料庫；
# 別的位置用 TIGERDUCK_TEST_DATABASE_URL 指定（見 server/tests/conftest.py）
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
│   ├── metrics.py               # Prometheus metrics，開在 :9000（不是對外的 :40000）
│   ├── _ssl_compat.py           # OpenSSL 3 寬容模式（NTUST TLS chain 是壞的）
│   ├── auth/                    # v3 身分層：crypto（憑證加密）/ tokens / service / rate_limit / moodle / models
│   ├── sync/                    # v3 使用者同步：upload / changelog / serializers / retention / models
│   ├── syncjobs/                # 伺服器代抓：executor / credentials（Moodle token、失效處理）/ moodle_client / assignments / provisioning
│   ├── routes/                  # v3：auth / user_devices / sync / academics / overrides / settings_docs / bulletins_feed / bulletins_v3 / sync_jobs / schedule_v3 / live_activities_v3
│   ├── push/                    # apns_client / fcm_client / router / pipeline（兩階段投遞）/ reminders / course_reminders / class_starts / job_payloads
│   ├── scheduler/               # APScheduler runtime、dispatch、retention
│   ├── bulletins/               # scraper / dedup / matcher / dispatcher（匿名）/ user_dispatch（登入使用者）/ taxonomy
│   │   └── llm/                 # OpenAI-compatible client + prompt
│   ├── secrets/                 # APNs .p8（gitignored）
│   ├── signing/                 # TLS pin 文件的 Ed25519 簽章金鑰，只掛進 backend（gitignored）
│   ├── migrations/              # Alembic
│   └── tests/                   # pytest（單元 + 整合）
├── portal/                      # 管理介面 — 另一個 FastAPI app
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/                     # FastAPI：main / config / db (asyncpg) / logs / status / routes / static
│   ├── tests/                   # pytest，不需要資料庫
│   └── web/                     # React 19 + Vite 8 + Tailwind 4 SPA（build 進 image 的 web/dist）
├── monitoring/                  # Grafana（provisioning + dashboards）、Prometheus、Loki、Alloy、sql-exporter collectors
├── docs/                        # push-notification-flows.drawio（推播流程圖）
├── scripts/                     # backfill / seed 等一次性腳本
├── deploy/launchd/              # macOS launchd plist（llama-server 等 host-side service）
├── docker-compose.yml           # 基底（backend + postgres + portal 掛 proxy-net，加上監控服務）
├── docker-compose.dev.yml       # TIGERDUCK_ENV=development 時自動載入，publish ports + 換成 host bridge
├── _compose-files.sh            # 共用：根據 TIGERDUCK_ENV 算出要載入哪些 compose 檔
├── Dockerfile / entrypoint.sh   # backend 容器
├── start.sh / stop.sh / logs.sh / clean-db.sh
├── .env.example
└── pyproject.toml / uv.lock
```

## 貢獻

歡迎 PR 與 Issue。送出前請確認：
1. `uv run pytest` 全綠；有動到 portal 的 Python 的話，`cd portal && uv run pytest` 也要綠
2. portal 前端有動到的話，`cd portal/web && npm run build` 也要過（`tsc -b` 的型別檢查是重點）
3. 有改 schema 的話附上 alembic revision
4. 以 `feature/your-feature` 或 `fix/your-fix` 命名分支，PR 目標分支 `dev`
5. PR 描述寫清楚 user-visible 影響（會 ship 給 iOS / Android client 的部分）

上面 1 與 2 會由 `.github/workflows/ci.yaml` 在每個對 `dev` / `main` 的 PR 上自動跑。

## 授權

本專案採用 [GNU Affero General Public License v3.0](LICENSE) 授權，與 [tigerduck-app](https://github.com/tigerduck-app/tigerduck-app) 與 [tigerduck-app-android](https://github.com/tigerduck-app/tigerduck-app-android) 一致。
