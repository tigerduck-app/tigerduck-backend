// JSON shapes returned by the portal's /api/* endpoints. These mirror
// the FastAPI route return values (and, for the announcement page, the
// upstream backend schemas it proxies). Keep them in sync if you change
// either side.

export type EnvInfo = {
  env: "development" | "production" | string;
  apns_env: string;
  log_level: string;
  llm_base_url: string;
  skip_llm_probe: boolean;
  backend_public_url: string;
  portal_public_url: string;
  host_lan_ips: string[];
  fcm_config: FcmConfig;
  apns_config: ApnsConfig;
  // null when the request reached the portal directly (dev, LAN).
  access: AccessSession | null;
};

// The Cloudflare Access user the request came through as, and the
// Cloudflare-served path that signs them out.
export type AccessSession = {
  email: string;
  logout_url: string;
};

export type FilePresence = {
  present: boolean;
  path: string;
  size_bytes?: number;
};

export type ContainerInfo = {
  name: string;
  state: string;
  health?: string | null;
  started_at?: string;
  restart_count?: number;
  image?: string;
  detail?: string;
};

export type PostgresHealth = {
  reachable: boolean;
  alembic_head?: string | null;
  rows?: Record<string, number | string>;
  detail?: string;
};

export type LlmHealth = {
  reachable: boolean;
  status_code?: number;
  latency_ms?: number;
  url?: string;
  detail?: string;
};

export type BackendVersion = {
  ok: boolean;
  version?: string;
  api_base_path?: string;
  detail?: string;
};

// `state` discriminator:
//   ok        — env value present, file readable, contents agree
//   mismatch  — both env + file present but values differ (FCM only)
//   missing   — at least one side configured, the other absent
//   disabled  — nothing configured at all (intentional / recording stub)
export type FcmConfig = {
  state: "ok" | "mismatch" | "missing" | "disabled";
  project_id?: string;
  json_project_id?: string;
  detail?: string;
};

export type ApnsConfig = {
  state: "ok" | "missing" | "disabled";
  apns_env: string;
  team_id?: string;
  key_id?: string;
  detail?: string;
};

export type StatusPayload = {
  // /api/status repeats the env fields but not the per-request session.
  env: Omit<EnvInfo, "access">;
  containers: ContainerInfo[];
  postgres: PostgresHealth;
  llm: LlmHealth;
  backend_version: BackendVersion;
  secrets: {
    apns_key: FilePresence;
    fcm_credentials: FilePresence;
  };
  fcm_config: FcmConfig;
  apns_config: ApnsConfig;
};

export type LogsTab = {
  id: string;
  label: string;
  kind: "raw" | "filter";
  container: string;
  needles?: string[] | null;
};

export type LogLevel = "error" | "warning" | "info" | "debug";

export type LogLine = {
  /** Docker's receive time, to the millisecond. */
  ts: string | null;
  level: LogLevel | null;
  logger: string;
  event: string;
  kv: [string, string][];
  raw: string;
};

export type LogsPayload = {
  ok: boolean;
  lines: LogLine[];
  detail?: string;
};

export type AccountState = "signed_in" | "needs_sign_in" | "failing";

export type UserStats = {
  users: { total: number; new_7d: number; weekly: { week: string; users: number }[] };
  active: { last_7d: number; daily: { day: string; users: number }[] };
  devices: {
    versions: { platform: string; app_version: string; devices: number }[];
    features: Record<
      | "class_reminders"
      | "bulletin_push"
      | "live_activity"
      | "assignment_reminders"
      | "cloud_sync"
      | "course_name_sync",
      number
    >;
    daily: { day: string; devices: number }[];
  };
  anonymous_devices: number;
  accounts: Record<AccountState, number>;
  pushes: {
    channels: { channel: string; sent: number; failed: number }[];
    daily: { day: string; sent: number; failed: number }[];
  };
};

export type UserRow = {
  id: string;
  student_id: string | null;
  display_name: string | null;
  created_at: string;
  platforms: string[];
  last_active_at: string | null;
  /** Null without a device. */
  cloud_sync: boolean | null;
  account: AccountState | null;
};

export type UserDevice = {
  id: string;
  device_name: string | null;
  device_model: string | null;
  platform: string;
  os_version: string | null;
  app_version: string | null;
  last_seen_at: string | null;
  created_at: string;
  cloud_sync_enabled: boolean;
  bulletin_push_enabled: boolean;
  sync_live_activity: boolean;
  push_token: { provider: string; environment: string | null } | null;
};

export type UserPush = {
  id: string;
  at: string;
  channel: string;
  scenario: string;
  title: string;
  device_id: string | null;
  status: "sent" | "failed" | "cancelled";
};

export type UserDetail = {
  id: string;
  student_id: string | null;
  display_name: string | null;
  locale: string | null;
  created_at: string;
  account: AccountState | null;
  class_reminders: boolean;
  devices: UserDevice[];
  term: string | null;
  week_start: string;
  courses: {
    id: number;
    name: string;
    classroom: string | null;
    schedule: { day?: number; periods?: string[] }[];
    skipped: string[];
  }[];
  /** Up to 500, newest first. */
  pushes: UserPush[];
  /** Every push of the last 7 days, by status. */
  push_counts: Record<UserPush["status"], number>;
  last_sync: { at: string; device_id: string } | null;
};

export type DeviceInfo = {
  device_id: string;
  user_id: string;
  bundle_id: string;
  apns_env?: string | null;
  has_pts_token: boolean;
  has_device_token: boolean;
  active_live_activities: number;
  updated_at: string;
};

export type DeviceRow = {
  device_id: string;
  user_id: string;
  client_device_id: string;
  student_id: string | null;
  display_name: string | null;
  platform: string;
  device_class: string;
  bundle_id: string;
  apns_env: string;
  server_push_enabled: boolean;
  has_pts_token: boolean;
  has_device_token: boolean;
  has_fcm_token: boolean;
  app_version: string | null;
  os_version: string | null;
  cloud_sync_enabled: boolean | null;
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
};

export type DevicesPayload = {
  items: DeviceRow[];
  total: number;
};

// Backend's GET /v2/bulletins/taxonomy shape: each entry has an `id`
// (the enum value) and a human-readable `label`. The taxonomy endpoint
// itself does NOT publish importance — that's a fixed Literal in the
// admin create schema, so the UI hardcodes the option list.
export type TaxonomyEntry = {
  id: string;
  label: string;
};

export type Taxonomy = {
  orgs: TaxonomyEntry[];
  tags: TaxonomyEntry[];
  default_tags: string[];
};

export type Importance = "low" | "normal" | "high";

export type BulletinSummary = {
  id: number;
  external_id: string;
  source: string;
  source_url: string;
  title: string;
  title_clean: string | null;
  summary: string | null;
  canonical_org: string | null;
  content_tags: string[];
  importance: Importance | null;
  posted_at: string | null;
  is_deleted: boolean;
};

export type BulletinDetail = BulletinSummary & {
  body_clean: string | null;
  body_md: string | null;
  raw_publisher: string | null;
};

export type BulletinList = {
  items: BulletinSummary[];
  next_cursor: number | null;
};

export type CustomPushTargetClass =
  | "iphone"
  | "ipad"
  | "mac"
  | "android"
  | "android_tablet";

export type CustomPushTargetFilter = {
  target_classes: CustomPushTargetClass[];
  user_id?: string;
  device_id?: string;
  list_id?: number;
  course_code?: string;
};

export type DeviceList = {
  id: number;
  name: string;
  description: string | null;
  member_count: number;
  created_at: string;
  updated_at: string;
};

export type DeviceListMemberRow = {
  device_id: string;
  user_id: string;
  student_id: string | null;
  display_name: string | null;
  platform: string;
  device_class: string;
  added_at: string;
};

export type DeviceListMembersResponse = {
  list_id: number;
  list_name: string;
  items: DeviceListMemberRow[];
  total: number;
};

export type AddMembersResponse = {
  added: number;
  already_present: number;
  unknown: number;
};

export type CustomPushCourse = { code: string; name: string; students: number };

export type CustomPushPreviewResponse = {
  matched: Record<string, number>;
  /** The part of `matched` that no account is signed in on. */
  anonymous: Record<string, number>;
  /** The current term's code, null while none is published. */
  term: string | null;
  /** The filter's course in `term`; null when nobody takes it or none was given. */
  course: CustomPushCourse | null;
};

export type CustomPushRequest = CustomPushTargetFilter & {
  title: string;
  body: string;
  keeps_record: boolean;
  force_ring: boolean;
};

export type CustomPushSendResponse = {
  request_id: string;
  kind: "record" | "popup";
  matched: number;
  queued: number;
};

export type CustomPushRecentItem = {
  id: string;
  kind: "record" | "popup";
  title: string;
  target_classes: string[];
  total: number;
  sent_at: string | null;
  is_queueing: boolean;
};

export type LinkRow = {
  name: string;
  target_url: string;
  enabled: boolean;
  expectations: Record<string, unknown>[];
  check_interval_minutes: number;
  last_check_at: string | null;
  last_check_ok: boolean | null;
  last_check_detail: string | null;
  alert_state: "ok" | "failing";
  updated_at: string;
  updated_by: string;
};

export type LinkTestResult = {
  ok: boolean;
  detail: string;
};
