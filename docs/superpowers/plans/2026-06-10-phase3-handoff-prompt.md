# Phase 3 接續實作提示詞（Session Handoff）

> 用途：開新 session 時直接貼上（或請 agent 讀取本檔），接續實作 TigerDuck 後端 Phase 3。

---

## 你的任務

在 `/Users/xinshou/IdeaProjects/tigerduck-backend`（Python 3.13 + FastAPI + SQLAlchemy async + PostgreSQL 17 + Alembic + APScheduler）實作 migration plan 的 **Phase 3：Server-Side Academic Sync**。

從 `feat/user-sync-phase2` 開新分支 `feat/user-sync-phase3`。先用 superpowers:writing-plans 寫計畫存到 `docs/superpowers/plans/`，再以 TDD 逐任務實作，**每完成一個小任務就 commit**。

## Spec 文件（先讀）

- `docs/superpowers/specs/2026-06-10-backend-data-model.md` — §6 有 `sync_policies` / `sync_jobs` / `sync_runs` 完整 DDL
- `docs/superpowers/specs/2026-06-10-sync-and-push-architecture.md` — §4 server-side sync 執行流程、pull-to-refresh、credential failure 處理
- `docs/superpowers/specs/2026-06-10-migration-plan.md` — Phase 3 章節（API、seed 值、風險）
- 已完成的計畫可參考：`docs/superpowers/plans/2026-06-10-phase1-identity-auth.md`、`2026-06-10-phase2-sync.md`

## 目前狀態（已完成）

分支 `feat/user-sync-phase2`（28 commits ahead of `dev`，未推送），**279 個測試全數通過**。Alembic head：`b8d4f0a2c3e9`。

### Phase 1（feat/user-sync-phase1，已含在 phase2 分支）
- 表：`users`、`external_accounts`、`external_account_credentials`、`auth_sessions`、`user_devices`、`device_push_tokens`、`push_jobs`、`push_deliveries`（後兩張已建表、Phase 4 才啟用）
- `server/auth/`：`models.py`、`crypto.py`（`CredentialCipher`、`build_credential_aad`）、`tokens.py`（JWT HS256 + refresh HMAC）、`rate_limit.py`、`moodle.py`（`MoodleVerifier` protocol + `HttpMoodleVerifier`/`StaticMoodleVerifier`）、`service.py`（login/refresh/logout）、`dependencies.py`（`CurrentAuthDep`，**會查 auth_sessions 撤銷狀態**）
- 路由：`server/routes/auth.py`、`server/routes/user_devices.py`，掛載於 `/v3`（`server/main.py` 的 `_mount_api_v3`）
- Refresh rotation 有 60s grace window + token family 撤銷；登入有 per-student_id/per-IP 滑動視窗限流

### Phase 2
- 表：`user_courses`、`user_course_overrides`、`user_course_skipped_dates`、`user_assignments`、`user_assignment_overrides`、`user_settings_documents`、`user_bulletin_subscriptions`、`user_bulletin_states`、`bulletin_user_matches`、`user_sync_state`、`user_change_log`
- `server/sync/`：`models.py`、`changelog.py`（**核心**：`lock_sync_state()` + `append_change(..., locked_state=)` + `read_changes()`，per-user row lock 保證 revision 提交順序）、`merge.py`（`clamp_ts`/`apply_field`，時鐘偏差 clamp）、`serializers.py`、`upload.py`（initial-upload）、`retention.py`（30 天 purge + `compacted_revision`，已掛入 `server/scheduler/runtime.py`）
- 路由：`server/routes/sync.py`（增量/410/full snapshot/initial-upload）、`academics.py`、`settings_docs.py`、`bulletins_v3.py`

## Phase 3 範圍（migration plan + 安全審查修正）

1. **三張新表** `sync_policies` / `sync_jobs` / `sync_runs`（DDL 照 data-model spec §6）+ Alembic migration（autogenerate 流程見下方「migration 工作流程」）
2. **Seed `sync_policies`**：`moodle_assignments` 28800s enabled、`ntust_courses` 28800s disabled、`calendar` 604800s enabled、`grades` 28800s disabled
3. **Sync job 執行器**（APScheduler 30s tick，掛入 `server/scheduler/runtime.py`）：
   - `FOR UPDATE SKIP LOCKED`、每 tick 上限 5 筆、stale lock 回收（running 超過 10 分鐘）
   - **全域並發上限**：撈取前先數 `status='running'` 的數量，納入上限（審查建議：防多 worker 時打爆學校 API 單一 IP）
   - 流程：解密憑證 → 用 token_cache 的 moodle_token 抓資料 → upsert `user_assignments`/`user_courses` → 變更透過 `lock_sync_state` + `append_change(locked_state=)` 寫 changelog → 更新 `sync_runs`/`sync_jobs`
   - 成功：`status='pending'`、`run_after=now+interval`、`attempts=0`；可重試失敗：backoff；超過 max_attempts：`failed`；憑證失效：`disabled`
4. **密碼重取 token 的鐵律（審查修正 1.4，必守）**：credential blob 裡 `password_verified` 為 `false` 時，密碼對 NTUST SSO **最多用一次**；SSO 失敗**一次即停**（`credential_status='invalid'`、所有 sync_jobs `disabled`、通知使用者重新登入），**絕不自動重試**——重試可能鎖死學生的學校帳號。認證類錯誤與網路類錯誤的 retry 策略必須分開。成功換得 token 後把 blob 的 `password_verified` 改為 `true` 並更新 `token_cache`。
5. **登入時建立 sync_jobs**：login 成功後為該 user 的每個 enabled job_type upsert `sync_jobs`（或用 backfill；二擇一在計畫中決定）
6. **Pull-to-refresh**：`POST /v3/sync-jobs/run-now?job_type=` — 設 `priority=1`、`run_after=now`；同 job 已在跑或已是高優先 pending 則回現有狀態；**per-user 60 秒 cooldown 用 sync_jobs 的 timestamp 判斷（不要存記憶體，多 instance 會失效）**；錯誤碼：`credential_invalid` / `school_rate_limited` / `sync_failed`
7. **Admin API**：`PATCH /v3/admin/sync-policies/{job_type}`（調 interval/enabled/active window）。注意：現有 admin 端點用 `X-Push-Token` shared secret（`server/security.py` 的 `require_shared_secret`），沿用即可
8. **Moodle/NTUST 抓取 client**：參考 `server/auth/moodle.py` 的 Protocol + injectable transport 模式，測試用 stub/MockTransport，不打真實 API
9. **憑證失效通知**：spec 要推播通知使用者，但 push pipeline 是 Phase 4 才啟用——可以先寫一筆 `push_jobs`（channel='system'，dedupe_key `system:account:{external_account_id}:reauth_required`，表已存在）讓 Phase 4 啟用時自然送出，並在計畫中明記這個決定

### 已知開放事項（不在 Phase 3 範圍，勿擴scope）
- 審查 1.6：帳號刪除端點與憑證即時清除（未實作）
- 審查 1.8：Phase 2~4c 期間公告推播雙軌重複問題（Phase 4 處理，需要 `device_registrations` 加 linked-user 標記）
- `assignments` 與 `courses` 同一 student session 共抓的最佳化（spec §4 提到，可在 Phase 3 計畫中決定做不做）

## 程式碼慣例與環境（重要）

- **Commit 格式**：`feat(Scope): short description` + 中文 bullet body，**不加 Co-Authored-By**，小步提交。修 review 問題用 `fix(Scope): ...`
- **TDD**：先寫測試（RED）再實作（GREEN）。e2e 測試模式照抄 `server/tests/test_sync_api.py`：
  - 檔頭必加 `pytestmark = pytest.mark.asyncio(loop_scope="session")`（漏掉會炸 "attached to a different loop"）
  - `client` fixture 上有 `client.app` 可覆寫 `app.state.*`（如 `moodle_verifier`）；DB 直查用 `build_session_factory(client.app.state.engine)`
  - 測試中的 per-field timestamp 一律用**過去**時間（未來時間會被 clamp 成抵達時間，語意改變）
- **測試 DB**：Docker 容器 `tigerduck-test-pg`（postgres:17-alpine，tigerduck/tigerduck@localhost:5432）。若不存在：`docker run -d --name tigerduck-test-pg -e POSTGRES_USER=tigerduck -e POSTGRES_PASSWORD=tigerduck -e POSTGRES_DB=tigerduck -p 5432:5432 postgres:17-alpine`。若 DROP DATABASE 卡住，先 `pg_terminate_backend` 清連線
- **跑測試**：`uv run pytest server/tests/ -q`。**注意：shell 經 rtk wrapper，exit code 不可靠，`&&` 串接不會因測試失敗中斷——必須看 pytest 輸出文字確認**
- **Lint**：`uv run ruff check server/<新檔案>`（既有檔案有歷史 lint 問題，別動）。用 `HTTP_422_UNPROCESSABLE_CONTENT`（不是 `_ENTITY`，已棄用）
- **Migration 工作流程**：在測試容器建 scratch DB `tigerduck_mig` → `TIGERDUCK_DATABASE_URL=postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck_mig uv run alembic upgrade head` → `alembic revision --autogenerate -m "..."` → **手動刪掉 autogenerate 誤抓的 `ix_devices_class_enabled` drift（upgrade 的 drop_index 與 downgrade 的 create_index 兩行）** → 驗證 up/down/up roundtrip → drop scratch DB。新 model 檔要在 `server/models.py` 底部 `import server.xxx.models  # noqa` 註冊到 Base.metadata
- **ORM 慣例**：照 `server/auth/models.py` / `server/sync/models.py`——StrEnum + String 欄位 + CHECK constraint（不用 PG ENUM）、TIMESTAMPTZ、partial index 用 `Index(..., postgresql_where=sa.text(...))`
- **交易陷阱**：route 的 `SessionDep` 在例外時 rollback——**需要在回錯誤前存活的寫入（如標記失敗狀態）必須先 `await session.commit()` 再 raise**（參考 `server/auth/service.py` 的 refresh reuse 處理）。背景 job 用 `session_scope(session_factory)` 自行管理交易
- **設定**：加在 `server/config.py` 的 `Settings`（env prefix `TIGERDUCK_`），測試用值加進 `server/tests/conftest.py` 的 `test_settings`
- **每完成一個任務後跑全套測試**，最後用 code-reviewer agent 對 `git diff feat/user-sync-phase2...HEAD -- server/` 做一輪審查並修 HIGH/MEDIUM

## 完成後

全套測試綠 + migration roundtrip 驗證 + ruff 乾淨後，總結變更並詢問使用者：推送開 PR、本地合併、或繼續 Phase 4。
