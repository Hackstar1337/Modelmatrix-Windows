// Shapes returned by the controller API (only the fields the UI uses).

export interface ProfileStatus {
  profile: string;
  display_name: string;
  runtime: string;
  runtime_label?: string;
  runtime_tags?: string[];
  running: boolean;
  ready: boolean;
  pid?: number | null;
  rss_mb?: number | null;
  base_url?: string;
  host?: string;
  port?: string;
  request_model?: string;
  server_model_id?: string;
  server_ids?: string[];
  launch_mode?: string;
  log_path?: string;
  missing_artifacts?: string[];
  source?: string;
  command?: string;
  conflict?: string;
  conflicts?: string[];
  [key: string]: unknown;
}

export interface BenchmarkRow {
  profile?: string;
  runtime?: string;
  ttft_ms?: number;
  decode_tokens_per_sec?: number;
  e2e_tokens_per_sec?: number;
  rss_mb?: number;
  prefill_cases?: { label: string; prompt_est_tokens?: number; ttft_ms?: number; decode_tokens_per_sec?: number }[];
}

export interface BenchmarkStatus {
  running: boolean;
  pid?: number;
  log_path?: string;
  latest?: {
    generated_at?: string;
    suite?: string;
    profiles: string[];
    rows: BenchmarkRow[];
    json_path?: string;
    markdown_path?: string;
  };
}

export interface Integration {
  id: string;
  label?: string;
  name?: string;
  enabled?: boolean;
  status?: string;
  detail?: string;
  [key: string]: unknown;
}

export interface StatusPayload {
  statuses: ProfileStatus[];
  benchmark: BenchmarkStatus;
  integrations: Integration[];
  profiles_dir: string;
  controller_root: string;
}

export interface UsageSummary {
  range: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  errors: number;
  local_requests: number;
  cloud_requests: number;
  local_completion_tokens: number;
  cloud_completion_tokens: number;
  inference_ms: number;
  estimated_rows: number;
  clients: number;
  models: number;
  avg_tps?: number | null;
  avg_ttft_ms?: number | null;
  avg_duration_ms?: number | null;
  avg_prompt_tokens?: number;
  p50_ttft_ms?: number | null;
  p95_ttft_ms?: number | null;
  p50_tps?: number | null;
  peak_tps?: number | null;
  previous: { requests: number; tokens: number; avg_tps: number | null; avg_ttft_ms: number | null };
}

export interface UsageTotals {
  requests: number;
  tokens: number;
  inference_ms: number;
  clients: number;
  local_requests: number;
  cloud_requests: number;
  requests_24h: number;
  tokens_24h: number;
  active_clients: number;
  requests_per_minute: number;
  avg_tps?: number | null;
  first_request?: number | null;
}

export interface Bucket {
  t: number;
  requests: number;
  local: number;
  cloud: number;
  prompt_tokens: number;
  completion_tokens: number;
  errors: number;
  avg_tps?: number | null;
  avg_ttft_ms?: number | null;
}

export interface ModelUsage {
  model: string;
  alias?: string;
  profile?: string;
  runtime?: string;
  tier?: string;
  host?: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  avg_tps?: number;
  avg_ttft_ms?: number;
  last_used?: number;
  share: number;
}

export interface ClientUsage {
  client_id: string;
  name?: string;
  detected?: string;
  process?: string;
  custom_name?: string;
  requests: number;
  tokens: number;
  avg_tps?: number;
  last_seen?: number;
  first_seen?: number;
  top_model?: string;
}

export interface RequestRow {
  id: number;
  started_at: number;
  duration_ms?: number;
  ttft_ms?: number;
  host: string;
  profile?: string;
  runtime?: string;
  endpoint?: string;
  method?: string;
  model?: string;
  model_requested?: string;
  model_served?: string;
  client_id?: string;
  client_label?: string;
  client_name?: string;
  client_process?: string;
  status?: number;
  stream?: boolean;
  prompt_tokens?: number;
  completion_tokens?: number;
  tokens_estimated?: boolean;
  tokens_per_second?: number;
  error?: string;
  tier?: string;
}

export interface InflightRow {
  id: string;
  started_at: number;
  endpoint?: string;
  client_label?: string;
  client_id?: string;
  client_process?: string;
  stream?: boolean;
  host?: string;
  chunks?: number;
  first_token_at?: number;
  model?: string;
  profile?: string;
  runtime?: string;
  tier?: string;
}

export interface RelayStatus {
  enabled: boolean;
  listening: boolean;
  port: number;
  url: string;
  ollama_url: string;
  retention_days: number;
  requests_recorded: number;
  database: string;
  error?: string;
  capture?: CaptureState[];
}

export interface CaptureState {
  profile: string;
  display_name: string;
  supported: boolean;
  enabled: boolean;
  port?: number;
  internal_port?: number;
  listening?: boolean;
  reason?: string;
}

export interface Overview {
  summary: UsageSummary;
  totals: UsageTotals;
  host_stats: { host: string; requests: number; avg_tps?: number; avg_ttft_ms?: number; last_request?: number }[];
  timeseries: Bucket[];
  models: ModelUsage[];
  clients: ClientUsage[];
  recent: RequestRow[];
  in_flight: InflightRow[];
  last_id: number;
  hosts: string[];
  relay: RelayStatus;
  generated_at: number;
}

export interface HostMetrics {
  host?: string;
  chip?: string;
  platform?: string;
  os_version?: string;
  cpu_percent?: number | null;
  cpu_cores?: number;
  load_average?: number[];
  memory?: { used_mb?: number; total_mb?: number; percent?: number } | null;
  gpus?: { index?: number; name?: string; util_percent?: number | null; vram_used_mb?: number; vram_total_mb?: number; temperature_c?: number; power_w?: number }[];
  temperature_c?: number | null;
  power_w?: number | null;
  uptime_seconds?: number;
  collected_at?: string;
}

export interface Gateway {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  has_token?: boolean;
  base_url?: string;
  ssh_target?: string;
  status?: string;
  agent_version?: string;
  [key: string]: unknown;
}

export interface BridgeState {
  app_connected: boolean;
  gateways: Gateway[];
  app: { version?: string; last_active_profile?: string; theme?: string; [key: string]: unknown };
  last_heartbeat?: number;
}

export interface OllamaStatus {
  reachable: boolean;
  base_url: string;
  version?: string;
  installed: { name: string; model?: string; size?: number; details?: { parameter_size?: string; quantization_level?: string; family?: string } }[];
  loaded: { name: string; model?: string; size?: number; size_vram?: number; expires_at?: string }[];
}

export interface DashboardInfo {
  name: string;
  version: string;
  controller_url: string;
  token_required: boolean;
  profiles_dir: string;
  controller_root: string;
  relay?: RelayStatus;
  platform?: PlatformInfo;
  capabilities?: DashboardCapabilities;
}

export interface PlatformInfo {
  os: string;
  display_name: string;
  local_host_label: string;
  credential_store: string;
}

export interface DashboardCapabilities {
  open_folders: boolean;
  quit_apps: boolean;
  purge_file_cache: boolean;
  keep_awake: boolean;
  remote_gateways: boolean;
  capture: boolean;
  connect_apps: boolean;
  huggingface: boolean;
}

export interface ProfileDocument {
  profile: string;
  file: string;
  format: string;
  values: Record<string, string>;
  text: string;
  running: boolean;
}

export interface RuntimeInfo {
  id: string;
  label: string;
  tags: string[];
  launch_mode: string;
}

export interface ProfileTemplate {
  id: string;
  label: string;
  values: Record<string, string>;
}

/* ---------- Optimize (free memory) ---------- */

export interface MemorySnapshot {
  total_bytes: number;
  used_bytes: number;
  app_bytes: number;
  wired_bytes: number;
  compressed_bytes: number;
  cached_bytes: number;
  free_bytes: number;
  swap_used_bytes: number;
  swap_total_bytes: number;
  pressure: "normal" | "warning" | "critical";
}

export interface OptimizeItem {
  id: string;
  kind: "ollama_model" | "model_server" | "file_cache";
  title: string;
  detail: string;
  bytes: number;
  in_use: boolean;
  needs_password: boolean;
  presets: ("recommended" | "maximum")[];
  last_used_at?: number;
}

export interface AppMemory {
  name: string;
  path?: string;
  bytes: number;
  processes: number;
  pid: number;
  quittable: boolean;
  /** Why it cannot be quit: "command-line tool", "part of macOS" or "background app". */
  reason?: string;
}

/** One app's outcome from the menu bar app's app.quit_apps command. */
export interface QuitAppResult {
  path: string;
  name: string;
  status: "quit" | "still_open" | "refused";
  ok: boolean;
  message: string;
}

export interface OptimizePlan {
  memory?: MemorySnapshot;
  items: OptimizeItem[];
  apps: AppMemory[];
  presets: { recommended: string[]; maximum: string[] };
  estimates: { recommended: number; maximum: number };
  thresholds: { in_use_seconds: number; idle_seconds: number };
  generated_at: number;
}

export interface OptimizeStepResult {
  id: string;
  ok: boolean;
  message: string;
  bytes?: number;
  skipped?: boolean;
}

export interface OptimizeRunResult {
  results: OptimizeStepResult[];
  before?: MemorySnapshot;
  after?: MemorySnapshot;
  freed_bytes?: number;
}

export interface OptimizePurgeResult {
  ok: boolean;
  canceled: boolean;
  message: string;
  before?: MemorySnapshot;
  after?: MemorySnapshot;
  freed_bytes?: number;
}
