# Phase 4 Part 2 (4b/4c/4d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Phase 4 — server-computed course reminders (4b), user-level bulletin dispatch with anonymous dual-track fix (4c, review 1.8), and Live Activity token takeover via device_push_tokens (4d).

**Architecture:** All three sources reuse the part-1 delivery pipeline (`server/push/pipeline.py`) by writing `push_jobs` rows. 4b mirrors `server/push/reminders.py` (scan + dedupe-key-with-epoch + cancellation pass). 4c adds a per-bulletin run marker table (because `bulletins.notified_at` is reserved for the anonymous flow) and a `linked_user_id` marker on `device_registrations` so the anonymous matcher skips logged-in devices. 4d makes the existing Live Activity end-push dispatcher prefer the freshest v3 `device_push_tokens` row (scope_key = `{scenario}:{source_id}`), falling back to `live_activity_update_tokens.update_token_hex`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL 17, Alembic, APScheduler, pytest.

**Branch:** `feat/user-sync-phase4` (continues from part 1). Review base for this session: commit at HEAD before Task 1 (record with `git rev-parse HEAD`).

---

## Design decisions (locked in)

1. **schedule_json format** (client-defined, observed in tests): `[{"day": 1, "periods": [3, 4]}]`. `day` is ISO weekday (1=Mon..7=Sun); entries with day outside 1–7 are skipped. `periods` entries are normalized via `str(p)`; unknown periods are ignored. Class start = earliest known period start in the entry.
2. **NTUST period table** lives in `Settings.course_period_start_times: dict[str, str]` (period → "HH:MM", Asia/Taipei): 1=08:10, 2=09:10, 3=10:20, 4=11:20, 5=12:20, 6=13:20, 7=14:20, 8=15:30, 9=16:30, 10=17:30, A=18:25, B=19:20, C=20:15, D=21:10.
3. **Semester guard**: only courses in the user's lexicographically-max semester (format "1141") among active enrolled courses generate reminders — prevents stale semesters from firing weekly forever.
4. **Course reminder dedupe key**: `course:{user_course_id}:reminder_{offset:g}m:{occurrence_epoch}` — occurrence epoch makes schedule changes mint new keys; the cancellation pass kills stale pending keys (same pattern/race-guards as assignment reminders: SKIP LOCKED, past-fire_at-but-valid keys not cancelled).
5. **Exclusions for 4b**: course deleted, not 'enrolled', `UserCourseOverride.is_hidden`, occurrence date in `user_course_skipped_dates` (deleted_at IS NULL), `notification` doc `courses.enabled == false`. Offsets from `courses.reminder_offsets_minutes`, default `[10.0]`.
6. **4c cursor**: new table `bulletin_user_match_runs(bulletin_id PK→bulletins CASCADE, matched_at)` — one row once user-matching ran for a bulletin. Bounded scan: processed, not deleted, NOT EXISTS in runs. `bulletins` table itself stays untouched.
7. **4c push job**: dedupe_key `bulletin:{bulletin_id}` (per-user via index), channel `bulletin`, fire_at=now, payload from `title_clean or title` / `summary or ...`, extras bulletin_id/source_url/canonical_org. Backfill `bulletin_user_matches.push_job_id` + `pushed_at` from the second pass over `pushed_at IS NULL`.
8. **Review 1.8 fix**: `device_registrations.linked_user_id UUID NULL` (FK users ON DELETE SET NULL). Set on v3 device register (by client_device_id), re-derived on v2 register (newest active user_device with that client_device_id), cleared on v3 device delete. Migration backfills from existing user_devices. `match_device_ids` adds `linked_user_id IS NULL`.
9. **4d**: dispatcher's Live Activity end push resolves the send token: if the activity's device_registration has `linked_user_id`, take the newest active `device_push_tokens` row (token_kind='live_activity_update', scope_key=`{scenario}:{source_id}`, joined through that user's devices); else/missing → old `update_token_hex`. No new endpoint — v3 `/v3/devices/register` already accepts live_activity_update tokens with scope_key.
10. **New settings** (+ conftest neutralization at 99999 for intervals): `course_reminder_scan_interval_seconds=600`, `course_reminder_window_hours=48`, `course_reminder_default_offsets_minutes=[10.0]`, `course_reminder_timezone="Asia/Taipei"`, `course_period_start_times`, `bulletin_user_dispatch_interval_seconds=60`, `bulletin_user_dispatch_batch_size=10`.

---

### Task 1: 4b — pure occurrence computation

**Files:** Create `server/push/course_reminders.py`, `server/tests/test_course_reminders.py`. Modify `server/config.py`.

- [ ] Step 1: Add settings (decision 10, course ones only) to `server/config.py`.
- [ ] Step 2: Write failing tests for `course_occurrences(schedule_json, window_start, window_end, tz, period_times)`: returns UTC datetimes for each (entry, date) in window; multi-period entry uses earliest period; unknown period/day skipped; empty schedule → [].
- [ ] Step 3: Implement pure function (zoneinfo, iterate dates in window, match ISO weekday).
- [ ] Step 4: Run tests → PASS. Ruff new files.
- [ ] Step 5: Commit `feat(Push): add course occurrence computation from schedule_json`.

### Task 2: 4b — scan_course_reminders

**Files:** Modify `server/push/course_reminders.py`, `server/tests/test_course_reminders.py`, `server/tests/conftest.py`.

- [ ] Step 1: Failing tests: creates jobs for default offset within window; respects custom offsets + disabled via `notification` doc `courses` section; semester guard (old-semester course ignored); hidden override excluded; skipped date excluded; schedule change cancels old pending + creates new; imminent valid job not cancelled; idempotent re-scan.
- [ ] Step 2: Implement `scan_course_reminders(session_factory, settings)` mirroring `scan_assignment_reminders` (valid_keys before past-offset skip; pending query `channel='course'` with SKIP LOCKED; pg_insert ON CONFLICT DO NOTHING; payload without URLs; custom_name from override).
- [ ] Step 3: Tests green; full suite green; ruff clean.
- [ ] Step 4: Commit `feat(Push): add course start reminders from schedule_json`.

### Task 3: 4b — scheduler wiring

**Files:** Modify `server/scheduler/runtime.py`, `server/tests/conftest.py` (interval 99999), scheduler test file if present.

- [ ] Step 1: Register `course_reminder_scan` job under `push_worker is not None` block.
- [ ] Step 2: Full suite green. Commit `feat(Push): schedule course reminder scan`.

### Task 4: 4c/4d — migration (linked_user_id + bulletin_user_match_runs)

**Files:** Modify `server/models.py` (DeviceRegistration.linked_user_id), `server/sync/models.py` (BulletinUserMatchRun). Create migration.

- [ ] Step 1: Model tests (insert run row; set linked_user_id) — RED against current schema.
- [ ] Step 2: Add columns/models. `linked_user_id: UUID NULL` FK users.id ON DELETE SET NULL.
- [ ] Step 3: Autogenerate on scratch DB `tigerduck_mig`; strip `ix_devices_class_enabled` drift lines; add backfill `UPDATE device_registrations dr SET linked_user_id = ud.user_id FROM user_devices ud WHERE ud.client_device_id = dr.device_id AND ud.deleted_at IS NULL`; verify up/down/up roundtrip; drop scratch.
- [ ] Step 4: Tests green. Commit `feat(Push): add linked-user marker and bulletin user match runs`.

### Task 5: 4c — linked marker maintenance

**Files:** Modify `server/routes/user_devices.py`, `server/routes/devices.py`. Tests in `server/tests/test_devices_v3.py` (or existing equivalents) + v2 device test file.

- [ ] Step 1: Failing tests: v3 register stamps marker on matching device_registrations row; v2 register (after v3) re-derives marker; v3 delete clears marker (only when owned by caller).
- [ ] Step 2: Implement (plain UPDATEs; v2 lookup newest active user_device by client_device_id).
- [ ] Step 3: Green; commit `feat(Push): maintain linked-user marker on device registrations`.

### Task 6: 4c — anonymous matcher skips linked devices

**Files:** Modify `server/bulletins/matcher.py` + its test file.

- [ ] Step 1: Failing test: subscribed device with linked_user_id set is not matched.
- [ ] Step 2: Add `DeviceRegistration.linked_user_id.is_(None)` to `match_device_ids` where-clause.
- [ ] Step 3: Green; commit `fix(Bulletins): skip linked devices in anonymous dispatch (review 1.8)`.

### Task 7: 4c — user-level bulletin dispatch

**Files:** Create `server/bulletins/user_dispatch.py`, `server/tests/test_bulletin_user_dispatch.py`. Modify `server/config.py`, `server/scheduler/runtime.py`, `server/tests/conftest.py`.

- [ ] Step 1: Failing tests: matched user gets push_job + match row with push_job_id/pushed_at backfilled; non-matching subscription → match absent; disabled/deleted subscription ignored; run marker prevents re-match; `bulletins.notified_at` untouched; crash-idempotency (match exists, pushed_at NULL → second run creates job); dedupe conflict path (job already exists → pushed_at still stamped).
- [ ] Step 2: Implement `dispatch_user_bulletins(session_factory, settings)`:
  - Pass A: select bulletins processed/not-deleted with NOT EXISTS runs row (SKIP LOCKED, limit batch) → load enabled `user_bulletin_subscriptions` (deleted_at IS NULL) → reuse `Rule`/`rule_hits` → insert `bulletin_user_matches` ON CONFLICT DO NOTHING (match_reason={"subscription_id": first_hit}) → insert run row.
  - Pass B: matches `pushed_at IS NULL` (SKIP LOCKED) join bulletin → pg_insert PushJob ON CONFLICT DO NOTHING RETURNING id; on conflict select existing active job id; stamp match.push_job_id/pushed_at.
- [ ] Step 3: Scheduler job `bulletin_user_dispatch` + settings + conftest 99999.
- [ ] Step 4: Full suite green; ruff. Commit `feat(Bulletins): user-level bulletin dispatch via push_jobs`.

### Task 8: 4d — Live Activity token preference

**Files:** Modify `server/scheduler/dispatcher.py`; tests in existing dispatcher test file.

- [ ] Step 1: Failing tests: linked device with newer v3 live_activity_update token (matching scope_key) → end push uses v3 token_value; no v3 token / unlinked → falls back to update_token_hex; invalidated v3 token ignored.
- [ ] Step 2: Implement `_resolve_update_token(session, token, device)` (query DevicePushToken join UserDevice on linked_user_id, scope_key == f"{token.scenario}:{token.source_id}", status active, order created_at desc, limit 1) and use in the activity loop.
- [ ] Step 3: Green; commit `feat(Push): prefer v3 live activity update tokens with v2 fallback`.

### Task 9: review + fixes

- [ ] Step 1: Full suite + ruff on all new/modified files.
- [ ] Step 2: code-reviewer agent over `git diff <session-base>...HEAD -- server/`; fix HIGH/MEDIUM (with judgment; document rejections).
- [ ] Step 3: Commit fixes; summarize.
