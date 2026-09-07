# Phase 4 接續實作提示詞（Session Handoff）

> 用途：開新 session 時直接貼上（或請 agent 讀取本檔），接續實作 TigerDuck 後端 Phase 4。

---

## 你的任務

在 `/Users/xinshou/IdeaProjects/tigerduck-backend`（Python 3.13 + FastAPI + SQLAlchemy async + PostgreSQL 17 + Alembic + APScheduler）實作 migration plan 的 **Phase 4：Server-Authoritative Push**（最後一個 Phase，分 4a–4d 四個子步驟）。

從 `feat/user-sync-phase3` 開新分支 `feat/user-sync-phase4`。先用 superpowers:writing-plans 寫計畫存到 `docs/superpowers/plans/`，再以 TDD 逐任務實作，**每完成一個小任務就 commit**。Phase 4 範圍大（pipeline + 4 個推播來源），計畫時可決定本 session 只做 pipeline + 4a，其餘子步驟留待後續 session（並在計畫中明記切分點）。

## Spec 文件（先讀）

- `docs/superpowers/specs/2026-06-10-backend-data-model.md` — §2 有 `device_push_tokens` / `push_jobs` / `push_deliveries` 完整 DDL 與 stale lock 規則（processing 超過 5 分鐘 → 回 pending、attempts++、backoff）
- `docs/superpowers/specs/2026-06-10-sync-and-push-architecture.md` — §5 兩階段投遞（Materialize/Deliver）、push 來源表、bulletin dispatch 流程、與既有 push 的相容策略
- `docs/superpowers/specs/2026-06-10-migration-plan.md` — Phase 4 章節（4a 作業提醒、4b 課程提醒、4c 公告、4d Live Activity）與「Existing Table Disposition」表
- 已完成的計畫可參考：`docs/superpowers/plans/2026-06-10-phase3-server-sync.md`（任務拆法與測試風格的範本）

## 目前狀態（已完成）

分支 `feat/user-sync-phase3`（13 commits ahead of `feat/user-sync-phase2`，兩個分支皆未推送），**334 個測試全數通過**。Alembic head：`e0f7f827758f`。

### Phase 1（identity/auth，已含）
- 表：`users`、`external_accounts`、`external_account_credentials`、`auth_sessions`、`user_devices`、`device_push_tokens`、`push_jobs`、`push_deliveries`（後兩張已建表，**Phase 4 啟用的就是它們**）
- `server/auth/`：crypto（`CredentialCipher`）、tokens、rate_limit、moodle（verifier）、service（login/refresh/logout）、dependencies（`CurrentAuthDep`）
- **push token 註冊已存在**：`POST /v3/devices/register`（`server/routes/user_devices.py`）登入裝置註冊時一併 upsert `device_push_tokens`（provider/token_kind/token_hash/scope_key，部分唯一索引 `ux_push_token_active`）；`DELETE /v3/devices/{id}` 已會處理 token
- `ux_push_jobs_dedupe_active` 唯一索引**涵蓋 sent/partial_failed**（審查修正 1.2）：同 dedupe_key 已送過就不會重建；「內容變更要再通知」必須用新 dedupe_key（如把 due_at 編進 key）

### Phase 2（client sync，已含）
- 表：`user_courses`（含 `schedule_json`，4b 課程提醒的資料源）、`user_assignments`、`user_assignment_overrides`（4a 排除條件）、`user_settings_documents`（namespace `notification` 內含 `reminder_offsets_hours` 等設定）、`user_bulletin_subscriptions`、`user_bulletin_states`、`bulletin_user_matches`（4c 用，`push_job_id`/`pushed_at` 欄位已留好）、`user_sync_state`、`user_change_log`
- `server/sync/changelog.py`：`lock_sync_state()` + `append_change(..., locked_state=)`（per-user row lock 保證 revision 提交順序）— push 相關表**不寫 changelog**（spec 明定）

### Phase 3（server-side academic sync，本次完成）
- 表：`sync_policies` / `sync_jobs` / `sync_runs`；migration `e0f7f827758f` 內含 policy seed
- `server/syncjobs/`：
  - `executor.py` — `SyncWorker` dataclass + `run_sync_tick()`：stale 回收（attempts++，超限轉 failed）→ claim（`pg_advisory_xact_lock` 序列化 + `FOR UPDATE SKIP LOCKED` + 全域 running 上限）→ 逐筆執行；`_execute_job` 上鎖後驗證 `status=='running' and locked_by==worker_id` 防重複執行；失敗記錄用獨立 fresh session（`_record_failure`）
  - `credentials.py` — **密碼鐵律持久化**：`refresh_moodle_token_durably()` 三段交易（先 commit `SSO_ATTEMPT_MARKER` → 單次 SSO → 結果獨立 commit）；`load_credential_blob` 看到未清除的 marker 直接 `CredentialInvalid`。`mark_credentials_invalid()` 已會寫一筆 reauth push_job（channel `system`、scenario `reauth_required`、dedupe `system:account:{external_account_id}:reauth_required`）— **Phase 4 的投遞 pipeline 跑起來後這筆會自然送出，不用另外做**
  - `moodle_client.py` — Protocol + injectable transport；錯誤分類（SsoAuthFailed＝認證類絕不重試 vs 網路類 backoff）
  - `assignments.py` — `apply_fetched_assignments()`：權威鏡像 upsert + changelog；**不寫 provider_is_submitted**（Phase 3 不抓 submission status，4a 提醒排除邏輯要注意這點：目前該欄位只反映 client 上傳的快照）
  - `provisioning.py` — login 時 `ensure_sync_jobs()`（`HANDLED_JOB_TYPES = ('moodle_assignments',)`）
- 路由：`server/routes/sync_jobs.py` — `POST /v3/sync-jobs/run-now`（cooldown 存 `sync_jobs.cursor` JSONB）+ `GET/PATCH /v3/admin/sync-policies`（X-Push-Token shared secret）
- scheduler：`build_scheduler(..., sync_worker=)` 註冊 `sync_jobs_tick`（30s）；lifespan 建 worker 並 seed default policies

### 既有 push 基礎設施（v2，勿破壞）
- `server/push/`：`apns_client.py`、`fcm_client.py`、`router.py`（`PushRouter`，`build_router` 在無憑證時自動用 recording stub）、`payload.py`、`custom_push_dispatcher.py`
- `server/scheduler/dispatcher.py`（`dispatch_due_pushes`，Live Activity/PTS）、`server/bulletins/jobs.py`（匿名公告推播）、`server/bulletins/matcher.py`（4c 要重用的 matcher 邏輯）
- 遷移期間雙軌：匿名裝置走 `device_registrations` + `bulletin_dispatches` + `scheduled_pushes`；登入使用者走新的 `push_jobs` + `push_deliveries` + `device_push_tokens`

## Phase 4 範圍（migration plan + spec §5）

1. **兩階段投遞 pipeline**（新 scheduler job，掛入 `server/scheduler/runtime.py`）：
   - Materialize：撈 `push_jobs WHERE status='pending' AND fire_at<=now AND available_at<=now`（建議同樣用 SKIP LOCKED + 鎖序列化，參考 syncjobs executor）→ `status='processing'` → 查該 user 的 active `device_push_tokens`（`device_id` 非 NULL 時只對該裝置）→ 建 `push_deliveries`（唯一索引 `ux_push_delivery_job_token` 防重）
   - Deliver：逐筆送 APNs/FCM（重用 `PushRouter`）→ 更新 delivery 狀態；410 BadDeviceToken → `device_push_tokens.status='invalidated'`
   - 聚合 job 狀態：全 sent/skipped（至少一 sent）→ `sent`；混合 → `partial_failed`；全失敗 → `failed`；無 active token → `failed` + `last_error='no_active_tokens'`
   - Stale processing 回收：超過 5 分鐘 → pending、attempts++、backoff（data-model spec §2 註記）
2. **4a 作業提醒**：sync 成功後（或獨立 scheduler job）依 `user_assignments`（due_at 將至、未刪除）×`user_assignment_overrides` 排除（`provider_is_submitted OR local_status IN ('locally_completed','ignored','archived')`）× `notification` settings document 的 `assignments.enabled` / `reminder_offsets_hours` 產生 `push_jobs`；dedupe key 形如 `assignment:moodle:{id}:reminder_24h`（內容變更需含 due_at 之類版本因子，見上面 1.2 註記）
3. **4b 課程提醒**：從 `user_courses.schedule_json` 算上課時間（排除 `user_course_skipped_dates`、`user_course_overrides.is_hidden`），產生 `course` channel push_jobs；登入使用者停用 `scheduled_pushes` 路徑
4. **4c 公告推播**：bulletin dispatch 對登入使用者改走 `user_bulletin_subscriptions` matcher → `bulletin_user_matches`（INSERT ON CONFLICT DO NOTHING）→ 建 push_jobs → 回填 `match.push_job_id`/`pushed_at`；**不可動 `bulletins.notified_at`**（保留給匿名流程）
5. **4d Live Activity**：`device_push_tokens(token_kind='live_activity_update')` 接手，`live_activity_update_tokens` 保留為讀取 fallback
6. **審查 1.8（4c 必須處理）**：Phase 2~4c 期間公告雙軌重複——同一台裝置既在 `device_registrations`（匿名管道）又是登入裝置時會收到兩次公告推播；需要在 `device_registrations` 加 linked-user 標記（或等價機制）讓匿名管道跳過已登入綁定的裝置

### 已知開放事項（不在 Phase 4 範圍，勿擴 scope）
- 審查 1.6：帳號刪除端點與憑證即時清除（未實作）
- Phase 3 遺留：server sync 不抓 Moodle submission status（4a 的排除目前只能靠 client 上傳的 `provider_is_submitted` 與 local_status；若要補抓，在計畫中另立任務評估，不要混進 pipeline 任務）
- `ntust_courses`/`calendar`/`grades` fetcher 未實作（policy 已 seed，`HANDLED_JOB_TYPES` 只有 moodle_assignments）

## 程式碼慣例與環境（重要）

- **Commit 格式**：`feat(Scope): short description` + 中文 bullet body，**不加 Co-Authored-By**，小步提交。修 review 問題用 `fix(Scope): ...`
- **TDD**：先寫測試（RED）再實作（GREEN）。e2e 測試模式照抄 `server/tests/test_sync_jobs_api.py`：
  - 檔頭必加 `pytestmark = pytest.mark.asyncio(loop_scope="session")`（漏掉會炸 "attached to a different loop"）
  - `client` fixture 上有 `client.app` 可覆寫 `app.state.*`；DB 直查用 `build_session_factory(client.app.state.engine)`
  - **ASGITransport 不會執行 lifespan**（conftest 舊註解曾誤導，已修正）——lifespan 做的事（如 policy seeding）若 e2e 需要，要在 conftest `client` fixture 內鏡像（現有 `ensure_default_policies` 就是這樣做的，新增 lifespan 行為時記得比照）
  - 測試中的 per-field timestamp 一律用**過去**時間（未來時間會被 clamp）
- **背景 job 測試模式**照抄 `server/tests/test_syncjobs_executor.py`：worker dataclass + stub 注入 + `build_session_factory(prepared_engine)`；新增 scheduler tick 的 interval 設定時**必須**同步在 conftest `test_settings` 設 99999 中和（如 `sync_job_tick_seconds=99999`）
- **測試 DB**：Docker 容器 `tigerduck-test-pg`（postgres:17-alpine，tigerduck/tigerduck@localhost:5432）。若不存在：`docker run -d --name tigerduck-test-pg -e POSTGRES_USER=tigerduck -e POSTGRES_PASSWORD=tigerduck -e POSTGRES_DB=tigerduck -p 5432:5432 postgres:17-alpine`
- **跑測試**：`uv run pytest server/tests/ -q`。**shell 經 rtk wrapper，exit code 不可靠，`&&` 串接不會因失敗中斷——必須看 pytest/ruff 輸出文字確認**（本次就踩過一次：ruff 報錯但 commit 照樣過）
- **Lint**：`uv run ruff check server/<新檔案>`（既有檔案有歷史 lint 問題，別動）。用 `HTTP_422_UNPROCESSABLE_CONTENT`（不是 `_ENTITY`）
- **Migration 工作流程**（Phase 4 若需要新表/欄位，如 device_registrations 的 linked-user 標記）：在測試容器建 scratch DB `tigerduck_mig` → `TIGERDUCK_DATABASE_URL=postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck_mig uv run alembic upgrade head` → `alembic revision --autogenerate -m "..."` → **手動刪掉 autogenerate 誤抓的 `ix_devices_class_enabled` drift（upgrade 的 drop_index 與 downgrade 的 create_index 兩行）** → 驗證 up/down/up roundtrip → drop scratch DB。新 model 檔要在 `server/models.py` 底部 `import server.xxx.models  # noqa` 註冊
- **ORM 慣例**：StrEnum + String 欄位 + CHECK constraint（不用 PG ENUM）、TIMESTAMPTZ、partial index 用 `Index(..., postgresql_where=sa.text(...))`；`metadata` 是 SQLAlchemy 保留字（`SyncRun.metadata_json` 的處理方式可參考）
- **交易陷阱**：route 的 `SessionDep` 在例外時 rollback——需要在回錯誤前存活的寫入必須先 `await session.commit()` 再 raise。背景 job 用 `session_scope(session_factory)` 自行管理交易；**失敗記錄一律開 fresh session**（工作 session 可能已 poisoned，參考 `_record_failure`）；跨 worker 的計數+認領要用 `pg_advisory_xact_lock` 原子化（參考 `_claim_due_jobs`）
- **設定**：加在 `server/config.py` 的 `Settings`（env prefix `TIGERDUCK_`），測試用值加進 conftest `test_settings`
- **每完成一個任務後跑全套測試**，最後用 code-reviewer agent 對 `git diff feat/user-sync-phase3...HEAD -- server/` 做一輪審查並修 HIGH/MEDIUM（對審查意見保持技術判斷，不合理的記錄理由後可不採納——本次 M2 即為一例）

## 完成後

全套測試綠 +（若有 migration）roundtrip 驗證 + ruff 乾淨後，總結變更並詢問使用者：推送開 PR、本地合併、或繼續剩餘子步驟／收尾開放事項。
