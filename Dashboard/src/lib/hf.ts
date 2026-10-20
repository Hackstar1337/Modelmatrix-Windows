// Hugging Face models, downloads and engines (controller routes under /api/hf).
import { api, post } from "./api";
import { routes } from "./routes";

/* ---------------- Check ---------------- */

export interface HFFile {
  path: string;
  size: number;
  sha256?: string;
}

export interface HFFit {
  context: number;
  need_bytes: number;
  cache_bytes: number;
  /** "good" (comfortable), "tight" (little to spare) or "too_big". */
  level: "good" | "tight" | "too_big" | string;
}

export interface HFVariant {
  label: string;
  files: HFFile[];
  total_bytes: number;
  download_bytes: number;
  bits_per_weight?: number;
  quality: string;
  full_precision: boolean;
  supported: boolean;
  unsupported_reason?: string;
  fit?: HFFit;
  longest_good_context?: number;
  fits_on_disk?: boolean;
  recommended: boolean;
}

export interface HFInstallOption {
  /** "homebrew", "official" (ModelMatrix can do it after you approve) or "manual" (run it yourself). */
  method: string;
  summary: string;
  command?: string;
  source?: string;
  location: string;
  removal: string;
}

export type HFEngineId = "llama.cpp" | "mlx-lm" | "ollama";

export interface HFEngineStatus {
  id: HFEngineId;
  name: string;
  applicable: boolean;
  installed: boolean;
  path?: string;
  version?: string;
  installed_by?: string;
  running?: boolean;
  note?: string;
  install?: HFInstallOption;
  managed?: boolean;
  /** What ModelMatrix can do for it: install, update, uninstall. */
  actions?: string[];
}

export interface HFMachine {
  kind: string;
  os: string;
  chip?: string;
  architecture: string;
  apple_silicon: boolean;
  rosetta: boolean;
  accelerator: string;
  memory_bytes: number;
  budget_bytes: number;
  budget_source: string;
  free_disk_bytes?: number;
  models_folder: string;
  host?: string;
  host_name?: string;
  engines_folder?: string;
  available_memory_bytes?: number;
}

export interface HFVerdict {
  code: string;
  title: string;
  detail: string;
  runnable: boolean;
}

export interface HFAlternative {
  repo: string;
  format: string;
  downloads?: number;
  likes?: number;
  gated: boolean;
  same_model?: boolean;
}

export interface HFCheckReport {
  schema_version: number;
  input: string;
  kind: "model" | "search" | string;
  repo?: {
    id: string;
    url: string;
    revision: string;
    sha?: string;
    license?: string;
    gated: boolean;
    login_required: boolean;
    downloads?: number;
    likes?: number;
    library?: string;
    pipeline_tag?: string;
    base_model?: string;
    base_relation?: string;
    last_modified?: string;
  };
  format?: { id: string; name: string; summary: string };
  task?: { kind: string; description: string; chat: boolean; vision: boolean };
  verdict: HFVerdict;
  machine: HFMachine;
  engine?: {
    needed?: HFEngineId;
    needed_name?: string;
    ready: boolean;
    backup?: HFEngineId;
    engines: HFEngineStatus[];
    installer_available: boolean;
  };
  model?: {
    name?: string;
    architecture?: string;
    parameters?: number;
    max_context?: number;
    default_context: number;
    context: number;
    layout_source: string;
    cache_bytes_at_context: number;
  };
  variants: HFVariant[];
  projector?: HFFile;
  recommendation?: { label: string; context: number; need_bytes: number; download_bytes: number; level: string; reason: string };
  alternatives: HFAlternative[];
  search_results: HFAlternative[];
  notes: string[];
  download_available: boolean;
  download_folder?: string;
  checked_at: string;
}

/* ---------------- Downloads and models ---------------- */

export type HFDownloadState = "queued" | "downloading" | "waiting" | "verifying" | "paused" | "completed" | "failed" | "cancelled";
export type HFProfileState = "creating" | "needs_engine" | "created" | "needs_memory" | "starting" | "ready" | "start_failed";

export interface HFDownloadFile {
  path: string;
  size: number;
  sha256?: string;
  bytes_done: number;
  done: boolean;
  reused?: string;
}

export interface HFDownload {
  id: string;
  kind: "files" | "ollama" | string;
  repo: string;
  revision: string;
  label: string;
  format: string;
  engine: string;
  display_name: string;
  folder: string;
  files: HFDownloadFile[];
  ollama_model?: string;
  context: number;
  parameters?: number;
  vision: boolean;
  start_after: boolean;
  host?: string;
  host_name?: string;
  gated?: boolean;
  state: HFDownloadState;
  waiting_for?: string;
  bytes_done: number;
  bytes_total: number;
  speed_bytes_per_second: number;
  eta_seconds?: number;
  message?: string;
  error?: string;
  attempts: number;
  warnings: string[];
  created_at: string;
  updated_at: string;
  completed_at?: string;
  profile?: string;
  profile_state?: HFProfileState;
  profile_message?: string;
  ready_url?: string;
  ready_model?: string;
  memory_need_bytes?: number;
  profile_log?: string[];
}

export interface HFDownloadedModel {
  id: string;
  repo: string;
  label: string;
  display_name: string;
  format: string;
  engine: string;
  kind: string;
  files: string[];
  folder: string;
  bytes_on_disk: number;
  missing_files: number;
  completed_at?: string;
  profile?: string;
  profile_state?: HFProfileState;
  profile_message?: string;
  ready_url?: string;
  ready_model?: string;
  host?: string;
  host_name?: string;
}

export interface HFSettings {
  models_folder: string;
  models_folder_path: string;
  free_disk_bytes?: number;
  endpoint: string | null;
  endpoint_in_use: string;
  speed_limit_mbps: number;
  connections_per_file: number;
  keep_awake: boolean;
  start_after_download: boolean;
  token_saved: boolean;
  engine_paths: Record<string, string>;
}

/* ---------------- Engines ---------------- */

export interface HFEnginePlan {
  id: string;
  engine: HFEngineId;
  engine_name: string;
  action: "install" | "update" | "uninstall";
  method: "homebrew" | "official";
  version?: string;
  installed_version?: string;
  title: string;
  commands: string[];
  downloads: { name: string; url: string; size: number; sha256: string }[];
  download_size: string;
  location: string;
  needs_admin: boolean;
  removal?: string;
  notes: string[];
  created_at: string;
  expires_at: string;
}

export type HFEngineJobState = "running" | "succeeded" | "failed" | "cancelled";

export interface HFEngineJob {
  id: string;
  plan_id: string;
  engine: HFEngineId;
  action: string;
  method: string;
  title: string;
  state: HFEngineJobState;
  log_count: number;
  log: string[];
  error?: string;
  path?: string;
  version?: string;
  started_at: string;
  finished_at?: string;
  manual_command?: string;
}

export interface HFEngineList {
  engines: HFEngineStatus[];
  engines_folder: string;
  homebrew?: string;
  jobs: Omit<HFEngineJob, "log" | "log_count" | "plan_id">[];
  host?: string;
}

/* ---------------- Calls ---------------- */

/** `host` is a remote host's name, or undefined for this Mac. */
export const hf = {
  check: (name: string, host?: string, context?: number, signal?: AbortSignal) =>
    api<HFCheckReport>(routes.hf.check(name, host, context).path, { signal }),
  downloads: (signal?: AbortSignal) => api<{ downloads: HFDownload[]; unreachable?: string[] }>(routes.hf.downloads().path, { signal }),
  models: (signal?: AbortSignal) =>
    api<{ models: HFDownloadedModel[]; untracked: { folder: string; bytes_on_disk: number }[]; unreachable?: string[] }>(routes.hf.models().path, { signal }),
  download: (body: { name: string; version?: string; context?: number; host?: string; engine?: string }) =>
    post<{ download: HFDownload; existing?: boolean }>(routes.hf.download(body).path, routes.hf.download(body).body),
  action: (id: string, action: "pause" | "resume" | "cancel" | "start", host?: string, deleteFiles?: boolean) =>
    post<{ download: HFDownload }>(routes.hf.downloadAction(id, action, host, deleteFiles).path, routes.hf.downloadAction(id, action, host, deleteFiles).body),
  removeFromList: (id: string, host?: string) => api(routes.hf.removeDownload(id, host).path, { method: "DELETE" }),
  deleteModel: (id: string, deleteFiles: boolean, host?: string) =>
    post<{ removed: string; files_deleted: boolean; freed_bytes: number; profile_deleted: string | null; kept_in_ollama: boolean }>(routes.hf.deleteModel(id, deleteFiles, host).path, routes.hf.deleteModel(id, deleteFiles, host).body),
  settings: () => api<{ settings: HFSettings }>(routes.hf.settings().path),
  saveSettings: (patch: Partial<Omit<HFSettings, "token_saved" | "models_folder_path" | "endpoint_in_use" | "free_disk_bytes">>) =>
    api<{ settings: HFSettings }>(routes.hf.saveSettings(patch).path, { method: "PUT", body: routes.hf.saveSettings(patch).body }),
  saveToken: (token: string) => post<{ saved: boolean; user?: string }>(routes.hf.saveToken(token).path, routes.hf.saveToken(token).body),
  deleteToken: () => api<{ saved: boolean }>(routes.hf.deleteToken().path, { method: "DELETE" }),
  engines: (host?: string, refresh = false, signal?: AbortSignal) => api<HFEngineList>(routes.hf.engines(host, refresh).path, { signal }),
  plan: (engine: HFEngineId, action: string, host?: string, method?: string) =>
    post<{ plan: HFEnginePlan }>(routes.hf.enginePlan(engine, action, host, method).path, routes.hf.enginePlan(engine, action, host, method).body),
  run: (planId: string) => post<{ job: HFEngineJob }>(routes.hf.engineRun(planId).path, routes.hf.engineRun(planId).body),
  job: (id: string, since: number) => api<{ job: HFEngineJob; lines?: string[]; next?: number }>(routes.hf.engineJob(id, since).path),
  cancelJob: (id: string) => post<{ job: HFEngineJob }>(routes.hf.cancelEngineJob(id).path, routes.hf.cancelEngineJob(id).body),
};

/* ---------------- Words ---------------- */

/** Sizes in GB/MB the way macOS shows memory (1 GB = 1024³ bytes). */
export function size(value: number | null | undefined): string {
  if (!value || value <= 0) return "0 MB";
  const gb = value / 1024 ** 3;
  if (gb >= 1) return `${gb >= 100 ? gb.toFixed(0) : gb.toFixed(1)} GB`;
  return `${Math.max(1, Math.round(value / 1024 ** 2))} MB`;
}

export function contextLabel(tokens: number | undefined): string {
  if (!tokens) return "-";
  return tokens >= 1024 && tokens % 1024 === 0 ? `${tokens / 1024}K` : String(tokens);
}

export function fitWords(level: string | undefined): { label: string; cls: string } {
  switch (level) {
    case "good":
      return { label: "Fits well", cls: "green" };
    case "tight":
      return { label: "Tight fit", cls: "amber" };
    case "too_big":
      return { label: "Too big", cls: "red" };
    default:
      return { label: "Unknown", cls: "" };
  }
}

export function stateWords(job: HFDownload): { label: string; cls: string } {
  if (job.state === "completed") {
    switch (job.profile_state) {
      case "ready":
        return { label: "READY", cls: "green" };
      case "starting":
      case "creating":
        return { label: "STARTING", cls: "blue" };
      case "needs_engine":
        return { label: "NEEDS ENGINE", cls: "amber" };
      case "needs_memory":
        return { label: "NEEDS MEMORY", cls: "amber" };
      case "start_failed":
        return { label: "START FAILED", cls: "red" };
      default:
        return { label: "DOWNLOADED", cls: "green" };
    }
  }
  const map: Record<HFDownloadState, { label: string; cls: string }> = {
    queued: { label: "QUEUED", cls: "" },
    downloading: { label: "DOWNLOADING", cls: "blue" },
    waiting: { label: "WAITING", cls: "amber" },
    verifying: { label: "VERIFYING", cls: "blue" },
    paused: { label: "PAUSED", cls: "" },
    completed: { label: "DONE", cls: "green" },
    failed: { label: "FAILED", cls: "red" },
    cancelled: { label: "CANCELLED", cls: "" },
  };
  return map[job.state] ?? { label: job.state.toUpperCase(), cls: "" };
}

export function eta(secondsLeft: number | undefined): string {
  if (secondsLeft === undefined || secondsLeft < 0) return "";
  if (secondsLeft < 60) return `${secondsLeft}s left`;
  const minutes = Math.round(secondsLeft / 60);
  if (minutes < 60) return `${minutes} min left`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min left`;
}

export function isActive(job: HFDownload) {
  return job.state === "queued" || job.state === "downloading" || job.state === "waiting" || job.state === "verifying";
}

export function targetName(host?: string) {
  return host ? host : "this device";
}
