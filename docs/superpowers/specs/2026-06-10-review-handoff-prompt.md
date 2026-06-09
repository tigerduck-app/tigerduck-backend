# Security & Technical Review Handoff Prompt

## 你的角色

你是資安與後端架構專家，負責審閱 TigerDuck 後端的新資料庫架構設計。以下三份 spec 文件已經過多輪設計討論和迭代修正，你需要做最終的安全與技術審查。

---

## 專案背景

TigerDuck 是一款台灣科技大學（NTUST）校園助手 App。目前支援 iOS、macOS、watchOS，計畫擴展到 Windows Desktop、iPadOS、wearOS。

### 現有後端

- **技術棧**：Python 3.13 + FastAPI + SQLAlchemy async + PostgreSQL 17 + Alembic + APScheduler
- **現有功能**：公告推播管線（scrape → LLM 分類 → 訂閱匹配 → APNs/FCM 發送）、裝置註冊、Live Activity、排程推播
- **現有認證**：`X-Push-Token` shared secret（寫入端點），讀取端點（公告列表）為公開
- **現有資料庫**：8 張表（device_registrations、scheduled_pushes、live_activity_update_tokens、device_lists、device_list_members、custom_push_dispatches、bulletins、bulletin_subscriptions、bulletin_dispatches）
- **部署**：Docker Compose（backend + postgres + portal），nginx-proxy-manager 反向代理

### 現有前端

- **平台**：iOS 18+（主要）、macOS 14+、watchOS
- **資料來源**：NTUST SSO（選課系統）、Moodle（作業/成績）、學校 ICS 行事曆、TigerDuck 後端（公告/推播）
- **本地儲存**：Keychain（帳密、token）、UserDefaults（設定）、SwiftData（課程/作業模型）、JSON 檔案（各種 cache）
- **使用者客製化（目前只存本地，不跨裝置同步）**：
  - 作業完成狀態（`isLocallyCompleted`）、忽略/封存（`isArchived`）
  - 課程自訂名稱、課表顏色
  - 首頁排版（HomeSection array）
  - 偏好設定（主題、語言、通知偏好等）

---

## 新架構目標

1. **使用者帳號系統**：建立 `users` 表，取代目前匿名的 `device_id` + `user_id` 字串
2. **跨裝置同步**：使用者客製化設定（作業狀態、課表顏色、首頁排版、偏好）可在所有裝置間同步
3. **後端代理校務資料**：後端儲存加密的 NTUST 帳密和 Moodle token，週期性替使用者抓取課程/作業資料，App 不再直接打學校 API（pull-to-refresh 也改為觸發後端立即同步）
4. **多平台支援**：iOS、iPadOS、macOS、watchOS、wearOS、Windows Desktop、Android

---

## 關鍵設計決策（已確認）

以下是設計過程中與使用者確認的決策，請勿推翻：

### 認證
- **App 端處理 NTUST SSO 登入**，後端不做代理登入（NTUST SSO 對單一 IP 有限流）
- App 登入後將 student_id、password、moodle_token 傳給後端
- 後端做一次**輕量 Moodle API 驗證**（確認 token 有效且對應該學號），不走 SSO
- 後端發行 JWT access token (15min) + refresh token (90 days)
- 帳密使用 **AES-256-GCM envelope encryption** 儲存，解密金鑰在 env var 或 KMS

### 同步策略
- **合併式同步（merge-based per-field）**：課程覆寫、作業覆寫、公告狀態等，每個欄位有獨立的 `updated_at` timestamp，可合併非衝突的並行修改
- **Revision-based optimistic concurrency**：Settings documents（JSONB），用 `base_revision` 做樂觀鎖
- **Change log + incremental sync**：`user_change_log` 表，client 用 `since_revision` 增量拉取變更

### 資料更新頻率（使用者指定）
- 作業：每 8 小時 + 發通知前事件驅動觸發
- 行事曆：每 7 天 + admin 手動觸發
- 課程：只在特定期間（選課/退選）由 dashboard 設定，頻率 8 小時
- 成績：每 8 小時（disabled by default）

### 其他
- 公告訂閱從 device-level 改為 **user-level**
- 匿名裝置 + 已登入使用者**共存**（匿名可看公告/行事曆，登入解鎖同步/課表/作業）
- Pull-to-refresh **不直接打學校 API**，改為觸發後端 high-priority sync job
- 推播從 device-centric 改為 **user-centric**，先歸屬 user 再 fan-out 到裝置
- 推播拆為 `push_jobs`（logical notification）+ `push_deliveries`（per-token 結果）

---

## Spec 文件

請審閱以下三份文件（已在同目錄下）：

1. **`2026-06-10-backend-data-model.md`** — 22 張新表的完整 DDL，分 6 層：
   - Identity & Auth（users、external_accounts、external_account_credentials、auth_sessions、user_devices）
   - Push（device_push_tokens、push_jobs、push_deliveries）
   - Academic Data（user_courses、user_course_overrides、user_course_skipped_dates、user_assignments、user_assignment_overrides）
   - Settings（user_settings_documents）
   - Bulletins（user_bulletin_subscriptions、user_bulletin_states、bulletin_user_matches）
   - Sync Infrastructure（sync_policies、sync_jobs、sync_runs、user_sync_state、user_change_log）

2. **`2026-06-10-sync-and-push-architecture.md`** — 認證流程、同步策略、change log 機制、server-side sync job 執行流程、推播 pipeline、API 規格

3. **`2026-06-10-migration-plan.md`** — 4 階段遷移計畫：
   - Phase 1：帳號 + 裝置 + 認證基礎
   - Phase 2：跨裝置同步（含 user_courses/user_assignments 作為 snapshot）
   - Phase 3：後端校務同步（啟用 sync jobs）
   - Phase 4：推播改為 server-authoritative

---

## 審查重點

請針對以下面向進行審查：

### 資安
- [ ] Credential 加密方案是否完整（key rotation、AAD 設計、blob 結構）
- [ ] JWT + refresh token 機制是否安全（rotation、reuse detection、session revocation）
- [ ] Push token 儲存是否需要額外保護（目前明文存 `token_value`）
- [ ] Change log payload 是否有洩漏敏感資料的風險
- [ ] 登入流程中，App 傳 password 給後端的傳輸安全（HTTPS + 是否需要額外保護）
- [ ] Moodle token 驗證是否足以防止偽造帳號
- [ ] Soft delete 與 retention 是否符合資料保護要求
- [ ] Rate limiting 和 abuse prevention（登入嘗試、API 呼叫、sync job 觸發）

### 技術架構
- [ ] 22 張表的 FK 依賴是否在遷移順序中都合法
- [ ] Index 設計是否覆蓋主要查詢路徑
- [ ] JSONB 欄位是否有需要 GIN index 的場景
- [ ] `user_change_log` 在高使用量下的效能（BIGSERIAL 全域 sequence 是否會成為瓶頸）
- [ ] Sync job staggering 機制是否足以避免壓垮學校伺服器
- [ ] `FOR UPDATE SKIP LOCKED` 在單 worker 和未來多 worker 場景下是否正確
- [ ] Per-field timestamps 的合併邏輯在時鐘偏差（clock skew）下是否安全
- [ ] Retention job 對 `user_change_log` 的清理是否會影響正在進行的 sync

### 遷移風險
- [ ] 各 Phase 的 rollback 是否真的可行
- [ ] 雙寫（dual-write）期間的一致性保證
- [ ] 現有 `/v2` API 在新架構下是否會有意外行為變化
- [ ] Live Activity 遷移的風險（iOS 客戶端依賴現有表）

### 遺漏
- [ ] 是否有未定義但必要的表或欄位
- [ ] API 規格是否有未覆蓋的 edge case
- [ ] 是否需要額外的 monitoring / alerting 表或機制

---

## 輸出格式

請按以下格式回覆：

```
## 1. 關鍵問題（必須修正）
逐項列出，每項包含：問題描述、影響範圍、建議修正方案

## 2. 建議改善（非必要但推薦）
逐項列出

## 3. 確認無誤的部分
列出你認為設計正確且不需要調整的部分

## 4. 整體評價
一段話總結
```
