// Shapes the /api/moodle routes answer with (portal/app/routes/moodle/).

export type MoodleStatus = {
  suspended_until: string | null;
  job_counts: {
    /** Pending jobs; the queue calls them active. */
    active: number;
    running: number;
    failed: number;
    disabled: number;
    pending_with_error: number;
    waiting_to_sign_in: number;
  };
  /** The Moodle assignments policy's interval; null before a policy exists. */
  interval_seconds: number | null;
};

export type SyncRunRow = {
  id: number;
  student_id: string;
  job_type: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  fetched_count: number | null;
  changed_count: number | null;
};

export type SyncStatsData = {
  summary: {
    succeeded_24h: number;
    failed_24h: number;
    running_now: number;
    total_24h: number;
    avg_duration_s: number;
    total_fetched: number;
  };
  /** The last 24 hours, oldest first, the current one last. */
  hourly: { hour: string; succeeded: number; failed: number }[];
  recent_runs: SyncRunRow[];
};

export type GlobalSyncJob = {
  id: number;
  student_id: string;
  job_type: string;
  status: string;
  attempts: number;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  run_after: string | null;
};

export type StudentMatch = {
  student_id: string;
  display_name: string | null;
  devices: number;
  last_seen_at: string | null;
};

export type ScheduleEntry = { day?: unknown; periods?: unknown };

export type CourseChange = {
  course_no: string;
  kind: "cancelled" | "room" | "start" | "end" | "note";
  mode: "day" | "range";
  start_date: string;
  end_date: string;
  excluded_weekdays: number[];
  periods: string[] | null;
  room: string | null;
  start_time: string | null;
  end_time: string | null;
  note_zh: string | null;
  note_en: string | null;
  /** The days from today on it reaches: in the term, not a holiday, on the course's meetings. */
  class_days: string[];
};

export type TimetableResponse = {
  /** Every term with courses, and the current one, newest first. */
  terms: string[];
  term: string | null;
  current_term: string | null;
  courses: {
    course_no: string | null;
    name: string;
    classroom: string | null;
    color: string;
    schedule: ScheduleEntry[];
  }[];
  /** The current term's changes from today on; empty for another term. */
  changes: CourseChange[];
};

export type SyncJob = {
  id: number;
  job_type: string;
  job_status: string;
  attempts: number;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  run_after: string | null;
};

export type SyncRun = {
  id: number;
  job_type: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  fetched_count: number | null;
  changed_count: number | null;
  error: string | null;
};

export type SyncOverride = {
  moodle_assignment_id: number;
  title: string | null;
  local_status: string;
  updated_at: string;
};

export type SyncDevice = {
  id: string;
  client_device_id: string;
  platform: string;
  device_class: string | null;
  device_name: string | null;
  device_model: string | null;
  app_version: string | null;
  os_version: string | null;
  locale: string | null;
  last_seen_at: string | null;
  last_login_at: string | null;
  created_at: string | null;
  deleted_at: string | null;
  /** 同步課程資訊, the master every synced row below sits under. */
  cloud_sync_enabled: boolean | null;
  sync_courses: boolean | null;
  sync_course_colors: boolean | null;
  sync_course_names: boolean | null;
  sync_assignments: boolean | null;
  sync_assignment_reminders: boolean | null;
  sync_live_activity: boolean | null;
  server_push_enabled: boolean | null;
  bulletin_push_enabled: boolean | null;
  /** Holds an active standard push token. */
  has_push_token: boolean;
};

/** A push a device's own write caused. */
export type SourceJob = {
  id: number;
  scenario: string;
  status: string;
  attempts: number;
  max_attempts: number;
  fire_at: string;
  sent_at: string | null;
  last_error: string | null;
  source_device_id: string | null;
};

export type PushDeliveryRow = {
  id: number;
  push_job_id: number;
  device_id: string | null;
  provider: string;
  status: string;
  attempts: number;
  max_attempts: number;
  failure_code: string | null;
  failure_message: string | null;
  sent_at: string | null;
  created_at: string;
  channel: string;
  scenario: string;
  kind: string | null;
};

/** A pending push job, with the devices the push pipeline would send it to. */
export type QueuedJob = {
  id: number;
  channel: string;
  scenario: string;
  kind: string | null;
  status: string;
  fire_at: string;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  title: string;
  body: string;
  /** `token_ready` false: planned for the device, but it will not arrive. */
  recipients: { device_id: string; token_ready: boolean }[];
};

export type SyncEventsResponse = {
  student_id: string;
  found: boolean;
  student?: { display_name: string | null; credential_status: string | null };
  jobs?: SyncJob[];
  runs?: SyncRun[];
  overrides?: SyncOverride[];
  devices?: SyncDevice[];
  push_jobs?: SourceJob[];
  push_deliveries?: PushDeliveryRow[];
  queued_jobs?: QueuedJob[];
  /** Every pending push Clear queue cancels; `queued_jobs` lists the first 300. */
  queued_total?: number;
  topology?: {
    revision: number;
    course_count: number;
    tombstone_count: number;
    courses_reset_at: string | null;
  };
};

export type SyncCourse = {
  id: number;
  course_no: string;
  course_name: string;
  course_name_en: string | null;
  client_course_no: string;
  color_hex: string | null;
  custom_names: Record<string, string> | null;
  default_palette_index: number;
  default_color_light: string;
  default_color_dark: string;
  updated_by_device_id: string | null;
  semester: string;
};

export type SyncTombstone = {
  course_key: string;
  course_no: string | null;
  semester: string;
  deleted_at: string;
  deleted_by_device_id: string | null;
};

export type SyncAssignment = {
  id: number;
  moodle_assignment_id: number;
  course_no: string;
  client_course_no: string | null;
  title: string;
  due_at: string | null;
  provider_is_submitted: boolean;
};

export type SyncCoursesResponse = {
  semester: string;
  palette_light: string[];
  palette_dark: string[];
  courses: SyncCourse[];
  tombstones?: SyncTombstone[];
  assignments?: SyncAssignment[];
  holiday_overrides?: {
    holiday_id: number;
    name_zh: string;
    name_en: string;
    start_date: string;
    end_date: string;
    notify: boolean;
    updated_at: string | null;
  }[];
  settings_documents?: {
    namespace: string;
    schema_version: number;
    revision: number;
    document: Record<string, unknown>;
    updated_by_device_id: string | null;
    updated_at: string | null;
  }[];
  /** Per device; never synced. */
  bulletin_subscriptions?: {
    id: number;
    device_id: string;
    name: string | null;
    orgs: string[];
    tags: string[];
    mode: string;
    enabled: boolean;
    updated_at: string | null;
  }[];
  course_skipped_dates?: {
    id: number;
    skipped_on: string;
    reason: string | null;
    created_by_device_id: string | null;
    course_no: string | null;
  }[];
};

export type LogEntry = {
  id: number;
  ts: string;
  level: string;
  source: string;
  message: string;
  device_label: string | null;
  platform: string | null;
};
