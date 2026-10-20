import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError, post, query, remote } from "./api";
import { platformContext, type PlatformContext } from "./platform";
import type { BridgeState, DashboardInfo, HostMetrics, OllamaStatus, Overview, StatusPayload } from "./types";

/* ---------------- Polling ---------------- */

export function usePoll<T>(fetcher: (signal: AbortSignal) => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const tick = useRef(0);

  const refresh = useCallback(async () => {
    const controller = new AbortController();
    const id = ++tick.current;
    try {
      const value = await fetcherRef.current(controller.signal);
      if (id === tick.current) {
        setData(value);
        setError(null);
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError" && id === tick.current) {
        setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
      }
    } finally {
      if (id === tick.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const loop = async () => {
      if (stopped) return;
      if (document.visibilityState === "visible") await refresh();
      if (!stopped) timer = window.setTimeout(loop, intervalMs);
    };
    setLoading(true);
    loop();
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps]);

  return { data, error, loading, refresh, setData };
}

export function useLocal<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(`modelmatrix.${key}`);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(`modelmatrix.${key}`, JSON.stringify(value));
    } catch {
      /* storage unavailable */
    }
  }, [key, value]);
  return [value, setValue] as const;
}

/* ---------------- Toasts ---------------- */

export interface Toast {
  id: number;
  kind: "ok" | "err" | "info";
  text: string;
}

/* ---------------- App context ---------------- */

export type Page = "overview" | "servers" | "clients" | "models" | "requests" | "settings";
export type Range = "1h" | "24h" | "7d" | "30d";
export type PendingAction = "ACTIVATING" | "STARTING" | "STOPPING" | "RESTARTING" | "BENCHMARKING";

interface Settings {
  theme: "dark" | "light";
  mode: "standard" | "advanced";
  range: Range;
  host: string;
  tier: "all" | "local" | "cloud";
  refreshMs: number;
  subnavCollapsed: boolean;
}

const defaultSettings: Settings = {
  theme: "dark",
  mode: "standard",
  range: "24h",
  host: "all",
  tier: "all",
  refreshMs: 2500,
  subnavCollapsed: false,
};

interface AppContextValue {
  settings: Settings;
  update: (patch: Partial<Settings>) => void;
  page: Page;
  setPage: (page: Page, section?: string) => void;
  info?: DashboardInfo;
  status?: StatusPayload;
  statusError: ApiError | null;
  refreshStatus: () => Promise<void>;
  overview?: Overview;
  overviewError: ApiError | null;
  refreshOverview: () => Promise<void>;
  metrics?: HostMetrics;
  bridge?: BridgeState;
  refreshBridge: () => Promise<void>;
  ollama?: OllamaStatus;
  refreshOllama: () => Promise<void>;
  pending: Record<string, PendingAction>;
  profileAction: (profile: string, action: "start" | "stop" | "restart" | "switch", gateway?: string) => Promise<void>;
  globalAction: (action: "stop-all" | "reopen-last", gateway?: string) => Promise<void>;
  startBenchmark: (profiles: string[] | null, suite?: string, options?: { allow_concurrent?: boolean; keep_running?: boolean }, gateway?: string) => Promise<void>;
  bridgeCommand: (type: string, payload?: Record<string, unknown>) => Promise<any>;
  toasts: Toast[];
  toast: (text: string, kind?: Toast["kind"]) => void;
  dismiss: (id: number) => void;
  lastActive?: string;
  online: boolean;
}

const AppContext = createContext<AppContextValue | null>(null);

export function useApp() {
  const value = useContext(AppContext);
  if (!value) throw new Error("useApp outside provider");
  return value;
}

/** Server-owned platform metadata with safe defaults for the legacy controller. */
export function usePlatform(): PlatformContext {
  const { info } = useApp();
  return useMemo(() => platformContext(info), [info]);
}

const pendingLabel: Record<string, PendingAction> = {
  start: "STARTING",
  stop: "STOPPING",
  restart: "RESTARTING",
  switch: "ACTIVATING",
};

export function AppProvider({ children }: { children: ReactNode }) {
  const [stored, setStored] = useLocal<Settings>("settings", defaultSettings);
  const settings = { ...defaultSettings, ...stored };
  const update = useCallback((patch: Partial<Settings>) => setStored((s) => ({ ...defaultSettings, ...s, ...patch })), [setStored]);
  const [page, setPageState] = useState<Page>(() => (location.hash.replace("#/", "").split(/[/?]/)[0] as Page) || "overview");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [pending, setPending] = useState<Record<string, PendingAction>>({});
  const [lastActiveLocal, setLastActiveLocal] = useLocal<string | undefined>("lastActive", undefined);
  const toastId = useRef(0);

  useEffect(() => {
    document.documentElement.dataset.theme = settings.theme;
  }, [settings.theme]);

  useEffect(() => {
    const onHash = () => {
      const next = location.hash.replace("#/", "").split(/[/?]/)[0] as Page;
      if (next) setPageState(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const setPage = useCallback((next: Page, section?: string) => {
    setPageState(next);
    const hash = `#/${next}${section ? `/${section}` : ""}`;
    if (location.hash !== hash) history.replaceState(null, "", hash);
    window.setTimeout(() => {
      if (section) document.getElementById(section)?.scrollIntoView({ behavior: "smooth", block: "start" });
      else document.querySelector(".content")?.scrollTo({ top: 0, behavior: "smooth" });
    }, 40);
  }, []);

  const dismiss = useCallback((id: number) => setToasts((list) => list.filter((t) => t.id !== id)), []);
  const toast = useCallback(
    (text: string, kind: Toast["kind"] = "info") => {
      const id = ++toastId.current;
      setToasts((list) => [...list.slice(-4), { id, kind, text }]);
      window.setTimeout(() => dismiss(id), kind === "err" ? 7000 : 3800);
    },
    [dismiss],
  );

  const info = usePoll<DashboardInfo>((signal) => api("/api/dashboard/info", { signal }), 30000);
  const status = usePoll<StatusPayload>((signal) => api("/api/status", { signal }), Math.max(2000, settings.refreshMs));
  const overview = usePoll<Overview>(
    (signal) => api(`/api/usage/overview${query({ range: settings.range, host: settings.host, tier: settings.tier })}`, { signal }),
    settings.refreshMs,
    [settings.range, settings.host, settings.tier],
  );
  const metrics = usePoll<HostMetrics>((signal) => api("/api/host/metrics", { signal }), 3000);
  const bridge = usePoll<BridgeState>((signal) => api("/api/bridge/state", { signal }), 5000);
  const ollama = usePoll<OllamaStatus>((signal) => api("/api/ollama/status", { signal }), 10000);

  const base = (gateway?: string) => (path: string) => (gateway ? remote(gateway, path) : path);

  const profileAction = useCallback(
    async (profile: string, action: "start" | "stop" | "restart" | "switch", gateway?: string) => {
      const key = gateway ? `${gateway}:${profile}` : profile;
      setPending((p) => ({ ...p, [key]: pendingLabel[action] }));
      try {
        await post(base(gateway)(`/api/${action}`), { profile });
        if (action === "switch" && !gateway) setLastActiveLocal(profile);
        const verb = { start: "Started", stop: "Stopped", restart: "Restarted", switch: "Activated" }[action];
        toast(`${verb} ${profile}`, "ok");
      } catch (e) {
        toast(`${action} ${profile} failed: ${(e as Error).message}`, "err");
      } finally {
        setPending((p) => {
          const next = { ...p };
          delete next[key];
          return next;
        });
        status.refresh();
        overview.refresh();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [toast],
  );

  const lastActive = (bridge.data?.app?.last_active_profile as string | undefined) || lastActiveLocal;

  const globalAction = useCallback(
    async (action: "stop-all" | "reopen-last", gateway?: string) => {
      if (action === "reopen-last") {
        if (!lastActive) {
          toast("No previously active profile yet", "info");
          return;
        }
        return profileAction(lastActive, "switch", gateway);
      }
      setPending((p) => ({ ...p, __all: "STOPPING" }));
      try {
        await post(base(gateway)("/api/stop-all"), {});
        toast("Stopped all models", "ok");
      } catch (e) {
        toast(`Stop all failed: ${(e as Error).message}`, "err");
      } finally {
        setPending((p) => {
          const next = { ...p };
          delete next.__all;
          return next;
        });
        status.refresh();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [lastActive, profileAction, toast],
  );

  const startBenchmark = useCallback(
    async (profiles: string[] | null, suite = "quick", options: { allow_concurrent?: boolean; keep_running?: boolean } = {}, gateway?: string) => {
      try {
        const body: Record<string, unknown> = { suite, ...options };
        if (profiles && profiles.length) body.profiles = profiles;
        await post(base(gateway)("/api/benchmark/start"), body);
        toast(profiles?.length ? `Benchmark started for ${profiles.join(", ")}` : "Benchmark started for all profiles", "ok");
      } catch (e) {
        toast(`Benchmark failed to start: ${(e as Error).message}`, "err");
      } finally {
        status.refresh();
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [toast],
  );

  const bridgeCommand = useCallback(
    async (type: string, payload: Record<string, unknown> = {}) => {
      const command = await post<{ id: string; status: string }>("/api/bridge/commands", { type, payload });
      // Wait for the menu bar app to report back (it polls every couple of seconds).
      const deadline = Date.now() + 130_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 900));
        const current = await api<{ status: string; message?: string; result?: unknown }>(`/api/bridge/commands/${command.id}`);
        if (current.status === "done") {
          bridge.refresh();
          return current;
        }
        if (current.status === "failed") throw new ApiError(500, current.message || "The menu bar app could not complete this action");
      }
      throw new ApiError(504, "Timed out waiting for the menu bar app");
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const value: AppContextValue = useMemo(
    () => ({
      settings,
      update,
      page,
      setPage,
      info: info.data,
      status: status.data,
      statusError: status.error,
      refreshStatus: status.refresh,
      overview: overview.data,
      overviewError: overview.error,
      refreshOverview: overview.refresh,
      metrics: metrics.data,
      bridge: bridge.data,
      refreshBridge: bridge.refresh,
      ollama: ollama.data,
      refreshOllama: ollama.refresh,
      pending,
      profileAction,
      globalAction,
      startBenchmark,
      bridgeCommand,
      toasts,
      toast,
      dismiss,
      lastActive,
      online: !status.error || status.error.status !== 0,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [stored, page, info.data, status.data, status.error, overview.data, overview.error, metrics.data, bridge.data, ollama.data, pending, toasts, lastActive],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
