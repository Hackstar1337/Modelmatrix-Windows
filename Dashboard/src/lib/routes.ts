import type { HFEngineId } from "./hf";
import type { Page, Range } from "./state";

export type RouteMethod = "GET" | "POST" | "PUT" | "DELETE";
export interface RouteContract<TBody = undefined> {
  method: RouteMethod;
  path: string;
  body?: TBody;
}

export interface UsageFilters {
  range: Range | "all";
  host: string;
  tier: "all" | "local" | "cloud";
}

export const routes = {
  dashboard: {
    session: (method: Extract<RouteMethod, "GET" | "POST" | "DELETE"> = "GET", token?: string): RouteContract<{ token?: string }> => ({
      method,
      path: "/api/dashboard/session",
      ...(method === "POST" ? { body: token === undefined ? {} : { token } } : {}),
    }),
    info: (): RouteContract => ({ method: "GET", path: "/api/dashboard/info" }),
  },
  status: (): RouteContract => ({ method: "GET", path: "/api/status" }),
  overview: (filters: UsageFilters): RouteContract => ({ method: "GET", path: `/api/usage/overview${query({ ...filters })}` }),
  metrics: (): RouteContract => ({ method: "GET", path: "/api/host/metrics" }),
  bridge: {
    state: (): RouteContract => ({ method: "GET", path: "/api/bridge/state" }),
    command: (type: string, payload: Record<string, unknown> = {}): RouteContract<{ type: string; payload: Record<string, unknown> }> => ({
      method: "POST",
      path: "/api/bridge/commands",
      body: { type, payload },
    }),
    result: (id: string, ok: boolean, message?: string, result?: unknown): RouteContract<Record<string, unknown>> => ({
      method: "POST",
      path: "/api/bridge/commands/result",
      body: { id, ok, ...(message === undefined ? {} : { message }), ...(result === undefined ? {} : { result }) },
    }),
    commandStatus: (id: string): RouteContract => ({ method: "GET", path: `/api/bridge/commands/${encodeURIComponent(id)}` }),
  },
  ollama: (): RouteContract => ({ method: "GET", path: "/api/ollama/status" }),
  profile: {
    runtimes: (): RouteContract => ({ method: "GET", path: "/api/profiles/runtimes" }),
    document: (profile: string): RouteContract => ({ method: "GET", path: `/api/profiles/document${query({ profile })}` }),
    save: (body: Record<string, unknown>): RouteContract<Record<string, unknown>> => ({ method: "POST", path: "/api/profiles/save", body }),
    delete: (profile: string): RouteContract<{ profile: string }> => ({ method: "POST", path: "/api/profiles/delete", body: { profile } }),
    duplicate: (profile: string, newProfile: string): RouteContract<{ profile: string; new_profile: string }> => ({ method: "POST", path: "/api/profiles/duplicate", body: { profile, new_profile: newProfile } }),
    capture: (profile: string, enabled: boolean): RouteContract<{ profile: string; enabled: boolean }> => ({ method: "POST", path: "/api/profiles/capture", body: { profile, enabled } }),
  },
  action: (action: "start" | "stop" | "restart" | "switch", profile: string): RouteContract<{ profile: string }> => ({ method: "POST", path: `/api/${action}`, body: { profile } }),
  stopAll: (): RouteContract<Record<string, never>> => ({ method: "POST", path: "/api/stop-all", body: {} }),
  benchmark: (profiles: string[] | null, suite = "quick", options: { allow_concurrent?: boolean; keep_running?: boolean } = {}): RouteContract<Record<string, unknown>> => ({
    method: "POST",
    path: "/api/benchmark/start",
    body: { suite, ...options, ...(profiles && profiles.length ? { profiles } : {}) },
  }),
  usage: {
    settings: (): RouteContract => ({ method: "GET", path: "/api/usage/settings" }),
    saveSettings: (retentionDays: number): RouteContract<{ retention_days: number }> => ({ method: "POST", path: "/api/usage/settings", body: { retention_days: retentionDays } }),
    clients: (filters: UsageFilters): RouteContract => ({ method: "GET", path: `/api/usage/clients${query({ ...filters })}` }),
    renameClient: (clientId: string, name: string): RouteContract<{ client_id: string; name: string }> => ({ method: "POST", path: "/api/usage/clients/rename", body: { client_id: clientId, name } }),
    requests: (filters: UsageFilters, search: string, limit: number, offset: number): RouteContract => ({ method: "GET", path: `/api/usage/requests${query({ ...filters, q: search, limit, offset })}` }),
    exportCsv: (filters: UsageFilters): RouteContract => ({ method: "GET", path: `/api/usage/export.csv${query({ ...filters })}` }),
    reset: (): RouteContract<{ confirm: true }> => ({ method: "POST", path: "/api/usage/reset", body: { confirm: true } }),
  },
  config: {
    profilesDir: (profilesDir: string): RouteContract<{ profiles_dir: string }> => ({ method: "POST", path: "/api/config/profiles-dir", body: { profiles_dir: profilesDir } }),
  },
  doctor: (): RouteContract => ({ method: "GET", path: "/api/doctor" }),
  integrations: (integration: string, action = "sync"): RouteContract<{ integration: string; action: string }> => ({ method: "POST", path: "/api/integrations/run", body: { integration, action } }),
  remote: (gateway: string, route: RouteContract): RouteContract => ({ ...route, path: `/api/remote/${encodeURIComponent(gateway)}${route.path}` }),
  optimize: {
    plan: (): RouteContract => ({ method: "GET", path: "/api/optimize/plan" }),
    run: (items: string[]): RouteContract<{ items: string[] }> => ({ method: "POST", path: "/api/optimize/run", body: { items } }),
    purge: (): RouteContract<Record<string, never>> => ({ method: "POST", path: "/api/optimize/purge", body: {} }),
  },
  hf: {
    check: (name: string, host?: string, context?: number): RouteContract => ({ method: "GET", path: `/api/hf/check${query({ name, host, context })}` }),
    downloads: (): RouteContract => ({ method: "GET", path: "/api/hf/downloads?host=all" }),
    models: (): RouteContract => ({ method: "GET", path: "/api/hf/models?host=all" }),
    download: (body: { name: string; version?: string; context?: number; host?: string; engine?: string }): RouteContract<typeof body> => ({ method: "POST", path: "/api/hf/downloads", body }),
    downloadAction: (id: string, action: "pause" | "resume" | "cancel" | "start", host?: string, deleteFiles?: boolean): RouteContract<Record<string, unknown>> => ({ method: "POST", path: `/api/hf/downloads/${encodeURIComponent(id)}/${action}`, body: { ...(host === undefined ? {} : { host }), ...(deleteFiles === undefined ? {} : { delete_files: deleteFiles }) } }),
    removeDownload: (id: string, host?: string): RouteContract => ({ method: "DELETE", path: `/api/hf/downloads/${encodeURIComponent(id)}${query({ host })}` }),
    deleteModel: (id: string, deleteFiles: boolean, host?: string): RouteContract<{ id: string; delete_files: boolean; host?: string }> => ({ method: "POST", path: "/api/hf/models/delete", body: { id, delete_files: deleteFiles, ...(host === undefined ? {} : { host }) } }),
    settings: (): RouteContract => ({ method: "GET", path: "/api/hf/settings" }),
    saveSettings: (body: Record<string, unknown>): RouteContract<Record<string, unknown>> => ({ method: "PUT", path: "/api/hf/settings", body }),
    saveToken: (token: string): RouteContract<{ token: string }> => ({ method: "POST", path: "/api/hf/token", body: { token } }),
    deleteToken: (): RouteContract => ({ method: "DELETE", path: "/api/hf/token" }),
    engines: (host?: string, refresh = false): RouteContract => ({ method: "GET", path: `/api/hf/engines${query({ host, refresh: refresh ? 1 : undefined })}` }),
    enginePlan: (engine: HFEngineId, action: string, host?: string, method?: string): RouteContract<Record<string, unknown>> => ({ method: "POST", path: "/api/hf/engines/plan", body: { engine, action, ...(host === undefined ? {} : { host }), ...(method === undefined ? {} : { method }) } }),
    engineRun: (planId: string): RouteContract<{ plan_id: string }> => ({ method: "POST", path: "/api/hf/engines/run", body: { plan_id: planId } }),
    engineJob: (id: string, since: number): RouteContract => ({ method: "GET", path: `/api/hf/engines/jobs/${encodeURIComponent(id)}${query({ since })}` }),
    cancelEngineJob: (id: string): RouteContract<Record<string, never>> => ({ method: "POST", path: `/api/hf/engines/jobs/${encodeURIComponent(id)}/cancel`, body: {} }),
  },
  connect: {
    profiles: (): RouteContract => ({ method: "GET", path: "/api/connect/profiles" }),
    targets: (): RouteContract => ({ method: "GET", path: "/api/connect/targets" }),
    details: (profile: string, connection?: string): RouteContract => ({ method: "GET", path: `/api/connect/details${query({ profile, connection })}` }),
    list: (): RouteContract => ({ method: "GET", path: "/api/connect/list" }),
    plan: (profile: string, target: string, makeDefault = false): RouteContract<{ profile: string; target: string; make_default: boolean }> => ({ method: "POST", path: "/api/connect/plan", body: { profile, target, make_default: makeDefault } }),
    apply: (planId: string): RouteContract<{ plan_id: string; approved: true }> => ({ method: "POST", path: "/api/connect/apply", body: { plan_id: planId, approved: true } }),
    verify: (id: string): RouteContract<{ id: string }> => ({ method: "POST", path: "/api/connect/verify", body: { id } }),
    disconnect: (id: string): RouteContract<{ id: string; remove_config: true; approved: true }> => ({ method: "POST", path: "/api/connect/disconnect", body: { id, remove_config: true, approved: true } }),
    undo: (id: string): RouteContract<{ id: string; approved: true }> => ({ method: "POST", path: "/api/connect/undo", body: { id, approved: true } }),
    secret: (id: string): RouteContract<{ id: string; approved: true }> => ({ method: "POST", path: "/api/connect/key", body: { id, approved: true } }),
  },
} as const;

export function query(params: Record<string, string | number | undefined | null>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function pageHash(page: Page, section?: string) {
  return `#/${page}${section ? `/${section}` : ""}`;
}
