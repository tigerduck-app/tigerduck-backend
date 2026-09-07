// Shapes returned by the sync/admin endpoints, shared across the tabs.



export type MoodleStatus = {
  suspended_until: string | null;
  job_counts: {
    active: number;
    running: number;
    failed: number;
    disabled: number;
  };
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
  recent_runs: {
    id: number;
    student_id: string;
    job_type: string;
    status: string;
    started_at: string | null;
    finished_at: string | null;
    fetched_count: number | null;
    changed_count: number | null;
  }[];
};

export type GlobalSyncJob = {
  id: number;
  student_id: string;
  job_type: string;
  status: string;
  priority: number;
  run_after: string | null;
  last_success_at: string | null;
  last_error: string | null;
  attempts: number;
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
  meta: Record<string, unknown> | null;
};

export type SyncOverride = {
  moodle_assignment_id: number;
  title: string | null;
  local_status: string;
  updated_at: string;
};

export type SyncCourse = {
  id: number;
  moodle_id: string | null;
  course_no: string;
  course_name: string;
  course_name_en: string | null;
  client_course_no: string;
  source: string;
  color_hex: string | null;
  custom_names: Record<string, string> | null;
  default_palette_index: number;
  default_color_light: string;
  default_color_dark: string;
  updated_by_device_id?: string | null;
  updated_at?: string | null;
  color_hex_device_id?: string | null;
  semester?: string;
};

export type SyncTombstone = {
  course_key: string;
  course_no: string;
  semester: string;
  deleted_at: string;
  deleted_by_device_id: string | null;
};

export type SyncAssignment = {
  id: number;
  moodle_assignment_id: number;
  course_no: string;
  course_name: string;
  /** Derived server-side from the Moodle course name; null if unparseable. */
  semester?: string | null;
  /** `course_no` when the client sent one, else read out of the course name. */
  client_course_no?: string | null;
  title: string;
  due_at: string | null;
  moodle_url: string | null;
  provider_is_submitted: boolean;
  provider_grade: string | null;
};

export type SyncCoursesResponse = {
  /** The term the table opens on, not the only one present. */
  semester: string;
  /** Every term the user has course rows or tombstones for, newest first. */
  semesters?: string[];
  palette_light: string[];
  palette_dark: string[];
  courses: SyncCourse[];
  tombstones?: SyncTombstone[];
  assignments?: SyncAssignment[];
};

export type SyncDevice = {
  id: string;
  client_device_id: string;
  platform: string;
  app_version: string | null;
  os_version: string | null;
  last_seen_at: string | null;
  last_login_at: string | null;
  created_at: string | null;
  sync_courses: boolean | null;
  sync_course_colors: boolean | null;
  sync_course_names: boolean | null;
  sync_assignments: boolean | null;
  cloud_sync_enabled: boolean | null;
};

export type PushJobRow = {
  id: number;
  scenario: string;
  status: string;
  attempts: number;
  max_attempts: number;
  fire_at: string;
  sent_at: string | null;
  last_error: string | null;
  dedupe_key: string;
  created_at: string;
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
};

export type SyncEventsResponse = {
  student_id: string;
  found: boolean;
  jobs?: SyncJob[];
  runs?: SyncRun[];
  overrides?: SyncOverride[];
  devices?: SyncDevice[];
  push_jobs?: PushJobRow[];
  push_deliveries?: PushDeliveryRow[];
  topology?: {
    revision: number;
    course_count: number;
    tombstone_count: number;
    courses_reset_at: string | null;
  };
  poll_status?: Record<string, "foreground" | "background">;
};

export type LogEntry = {
  id: number;
  ts: string;
  level: string;
  source: string;
  message: string;
  detail: Record<string, unknown> | null;
  device_label: string | null;
  platform: string | null;
};

export type LogsResponse = {
  entries: LogEntry[];
  latest_id: number;
};

export type TopologyNode =
  | { kind: "backend" }
  | { kind: "device"; device: SyncDevice };
