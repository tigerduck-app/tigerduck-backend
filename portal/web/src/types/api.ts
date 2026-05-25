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
  env: EnvInfo;
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

export type LogsPayload = {
  ok: boolean;
  text: string;
  detail?: string;
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
  platform: string;
  device_class: string;
  bundle_id: string;
  apns_env: string;
  server_push_enabled: boolean;
  has_pts_token: boolean;
  has_device_token: boolean;
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

export type CustomPushTargetClass = "iphone" | "ipad" | "android";

export type CustomPushTargetFilter = {
  target_classes: CustomPushTargetClass[];
  user_id?: string;
  device_id?: string;
};

export type CustomPushPreviewResponse = {
  matched: Record<string, number>;
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
