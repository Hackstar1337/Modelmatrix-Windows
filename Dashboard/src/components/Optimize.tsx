import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, KeyRound, Loader2, Lock, RefreshCw, Sparkles, XCircle } from "lucide-react";
import { api, post } from "../lib/api";
import { ago, bytes } from "../lib/format";
import { useApp, usePlatform } from "../lib/state";
import type {
  AppMemory,
  MemorySnapshot,
  OptimizeItem,
  OptimizePlan,
  OptimizePurgeResult,
  OptimizeRunResult,
  OptimizeStepResult,
  QuitAppResult,
} from "../lib/types";
import { Confirm, Modal, Notice } from "./ui";

const OPEN_EVENT = "modelmatrix:optimize";

/** Opens the Optimize window from anywhere in the dashboard. */
export function openOptimize() {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

/** Mounted once in the app shell; renders the window while it is open. */
export function OptimizeHost() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const show = () => setOpen(true);
    window.addEventListener(OPEN_EVENT, show);
    // Deep link: #/overview/optimize (used by the menu bar app and the CLI).
    if (/\/optimize\b/.test(window.location.hash)) setOpen(true);
    return () => window.removeEventListener(OPEN_EVENT, show);
  }, []);
  const close = () => {
    setOpen(false);
    if (/\/optimize\b/.test(window.location.hash)) history.replaceState(null, "", window.location.hash.replace(/\/optimize\b.*/, ""));
  };
  return open ? <OptimizeModal onClose={close} /> : null;
}

type Preset = "maximum" | "recommended";
type Phase = "idle" | "running" | "quitting" | "purging" | "done";

interface Outcome {
  before?: MemorySnapshot;
  after?: MemorySnapshot;
  freed: number;
  steps: (OptimizeStepResult & { title: string })[];
}

function freedBetween(before?: MemorySnapshot, after?: MemorySnapshot) {
  if (!before || !after) return 0;
  return Math.max(0, before.used_bytes - after.used_bytes, after.free_bytes - before.free_bytes);
}

/**
 * Whether `appName` is (probably) the browser showing this page. Quitting it
 * mid-run would cut the run short, so it is closed last, after the results.
 */
export function isViewerBrowser(appName: string, userAgent = navigator.userAgent) {
  const name = appName.toLowerCase();
  if (/Electron\//.test(userAgent)) return false;
  if (/Edg\//.test(userAgent)) return name.includes("edge");
  if (/OPR\//.test(userAgent)) return name.includes("opera");
  if (/Vivaldi/.test(userAgent)) return name.includes("vivaldi");
  if (/Firefox\//.test(userAgent)) return /firefox|zen|librewolf|floorp|waterfox/.test(name);
  if (/Chrome\//.test(userAgent)) return /chrome|chromium|brave|^arc$|^dia$|comet|thorium/.test(name);
  if (/Safari\//.test(userAgent)) return /safari|orion/.test(name);
  return false;
}

function listNames(apps: AppMemory[]) {
  const names = apps.map((a) => a.name);
  return names.length > 4 ? `${names.slice(0, 4).join(", ")} and ${names.length - 4} more` : names.join(", ");
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function OptimizeModal({ onClose }: { onClose: () => void }) {
  const { toast, refreshStatus, refreshOllama, bridge, bridgeCommand } = useApp();
  const { capabilities, platform, bridgeLabel } = usePlatform();
  const [plan, setPlan] = useState<OptimizePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preset, setPreset] = useState<Preset>("maximum");
  const [selected, setSelected] = useState<Set<string> | null>(null);
  // Bundle paths of the other apps picked to quit. Presets never pick apps.
  const [selectedApps, setSelectedApps] = useState<Set<string>>(new Set());
  const [phase, setPhase] = useState<Phase>("idle");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [confirming, setConfirming] = useState(false);
  const appConnected = !!bridge?.app_connected && capabilities.quit_apps;

  const load = useCallback(async () => {
    try {
      const next = await api<OptimizePlan>("/api/optimize/plan");
      setPlan(next);
      setError(null);
      // Keep the user's ticks; drop items that are gone; preselect on first load.
      setSelected((current) => {
        const ids = new Set(next.items.map((i) => i.id));
        if (!current) return new Set(next.presets.maximum.filter((id) => capabilities.purge_file_cache || next.items.find((item) => item.id === id)?.kind !== "file_cache"));
        return new Set([...current].filter((id) => ids.has(id)));
      });
      setSelectedApps((current) => {
        const paths = new Set(next.apps.filter((a) => a.quittable && a.path).map((a) => a.path as string));
        return new Set([...current].filter((path) => paths.has(path)));
      });
    } catch (e) {
      setError((e as Error).message);
    }
  }, [capabilities.purge_file_cache]);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (phase !== "idle") return;
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [load, phase]);

  const choosePreset = (next: Preset) => {
    setPreset(next);
    if (plan) setSelected(new Set(plan.presets[next]));
  };
  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current ?? []);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  const toggleApp = (path: string) =>
    setSelectedApps((current) => {
      const next = new Set(current);
      next.has(path) ? next.delete(path) : next.add(path);
      return next;
    });

  const chosen = useMemo(() => (plan?.items ?? []).filter((i) => selected?.has(i.id)), [plan, selected]);
  const quittableApps = useMemo(() => (plan?.apps ?? []).filter((a) => capabilities.quit_apps && a.quittable && a.path), [capabilities.quit_apps, plan]);
  const chosenApps = useMemo(() => quittableApps.filter((a) => selectedApps.has(a.path as string)), [quittableApps, selectedApps]);
  const allAppsSelected = quittableApps.length > 0 && chosenApps.length === quittableApps.length;
  const toggleAllApps = () => setSelectedApps(allAppsSelected ? new Set() : new Set(quittableApps.map((a) => a.path as string)));
  const estimate = [...chosen, ...chosenApps].reduce((sum, i) => sum + i.bytes, 0);
  const wantsPurge = capabilities.purge_file_cache && chosen.some((i) => i.kind === "file_cache");
  const hasWork = chosen.length > 0 || chosenApps.length > 0;
  const viewerApps = chosenApps.filter((a) => isViewerBrowser(a.name));

  const quitApps = async (apps: AppMemory[]) => {
    const reply = await bridgeCommand("app.quit_apps", { apps: apps.map((a) => ({ path: a.path, name: a.name })) });
    const results = (Array.isArray(reply.result) ? reply.result : []) as QuitAppResult[];
    const sizes = Object.fromEntries(apps.map((a) => [a.path, a.bytes]));
    setSelectedApps((current) => {
      const next = new Set(current);
      results.filter((r) => r.ok).forEach((r) => next.delete(r.path));
      return next;
    });
    return results.map((r) => ({
      id: `app:${r.path}`,
      ok: r.ok,
      skipped: r.status === "still_open",
      message: r.message,
      bytes: r.ok ? sizes[r.path] : undefined,
      title: `Quit ${r.name}`,
    }));
  };

  const run = async () => {
    if (!plan || !hasWork) return;
    setConfirming(false);
    const titles = Object.fromEntries(plan.items.map((i) => [i.id, i.title]));
    const steps: Outcome["steps"] = [];
    let before: MemorySnapshot | undefined = plan.memory;
    let after: MemorySnapshot | undefined;
    const lastApps = viewerApps;
    const appsNow = chosenApps.filter((a) => !lastApps.includes(a));
    try {
      const ids = chosen.filter((i) => i.kind !== "file_cache").map((i) => i.id);
      if (ids.length) {
        setPhase("running");
        const result = await post<OptimizeRunResult>("/api/optimize/run", { items: ids });
        before = result.before ?? before;
        after = result.after;
        result.results.filter((r) => !r.skipped).forEach((r) => steps.push({ ...r, title: titles[r.id] ?? r.id }));
      }
      if (appsNow.length) {
        setPhase("quitting");
        steps.push(...(await quitApps(appsNow)));
      }
      if (wantsPurge) {
        setPhase("purging");
        const purge = await post<OptimizePurgeResult>("/api/optimize/purge");
        before = before ?? purge.before;
        after = purge.after ?? after;
        steps.push({
          id: "system:file-cache",
          ok: purge.ok,
          skipped: purge.canceled,
          message: purge.message,
          title: titles["system:file-cache"] ?? "Clear file cache",
        });
      }
      if (appsNow.length) {
        // Closed apps hand memory back over a second or two.
        await sleep(1500);
        after = (await api<OptimizePlan>("/api/optimize/plan").catch(() => null))?.memory ?? after;
      }
      lastApps.forEach((a) =>
        steps.push({
          id: `app:${a.path}`,
          ok: true,
          skipped: true,
          message: "Closes last, because this page is open in it.",
          title: `Quit ${a.name}`,
        }),
      );
      const freed = freedBetween(before, after);
      setOutcome({ before, after, freed, steps });
      setPhase("done");
      toast(freed > 0 ? `Optimize freed ${bytes(freed)}` : "Optimize finished", "ok");
      if (lastApps.length) {
        toast(`Closing ${listNames(lastApps)} in a few seconds`, "ok");
        await sleep(3500);
        await quitApps(lastApps).catch(() => undefined);
      }
    } catch (e) {
      setPhase("idle");
      toast(`Optimize failed: ${(e as Error).message}`, "err");
    } finally {
      refreshStatus();
      refreshOllama();
      load();
    }
  };

  const start = () => (chosenApps.length ? setConfirming(true) : run());

  const busy = phase === "running" || phase === "quitting" || phase === "purging";
  const runLabel =
    phase === "running" ? "Freeing memory…" : phase === "quitting" ? "Closing apps…" : phase === "purging" ? "Waiting for password…" : "Optimize now";
  const footer =
    phase === "done" ? (
      <>
        <button className="btn ghost" onClick={() => { setOutcome(null); setPhase("idle"); }}>
          <RefreshCw size={14} /> Optimize again
        </button>
        <button className="btn primary" onClick={onClose}>
          Done
        </button>
      </>
    ) : (
      <>
        <span className="opt-estimate">
          {hasWork ? (
            <>
              Up to <b>{bytes(estimate)}</b>
              {chosen.length > 0 && <> from {chosen.length} item{chosen.length === 1 ? "" : "s"}</>}
              {chosenApps.length > 0 && <span className="muted"> · quits {chosenApps.length} app{chosenApps.length === 1 ? "" : "s"}</span>}
              {wantsPurge && <span className="muted"> · asks for your password</span>}
            </>
          ) : (
            <span className="muted">Nothing selected</span>
          )}
        </span>
        <button className="btn ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn primary" onClick={start} disabled={busy || !hasWork}>
          {busy ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />}
          {runLabel}
        </button>
      </>
    );

  return (
    <Modal
      title={
        <span className="row" style={{ gap: 8 }}>
          <Sparkles size={18} className="accent-text" /> Optimize memory
        </span>
      }
      onClose={busy ? () => undefined : onClose}
      footer={footer}
    >
      {error && !plan && <Notice kind="bad">{error}</Notice>}
      {!plan && !error && (
        <div className="muted row" style={{ gap: 8 }}>
          <Loader2 size={14} className="spin" /> Measuring memory…
        </div>
      )}
      {plan?.memory && <MemoryBar memory={outcome?.after ?? plan.memory} />}
      {phase === "purging" && (
        <Notice kind="info">
          <KeyRound size={14} style={{ verticalAlign: -2 }} /> The {platform.display_name} controller is applying its protected cache policy. Cancel the system prompt to skip this step.
        </Notice>
      )}
      {phase === "quitting" && (
        <Notice kind="info">Closing the selected apps. If one asks to save your work, answer it and it will finish closing.</Notice>
      )}
      {outcome ? (
        <OutcomeView outcome={outcome} />
      ) : (
        plan && (
          <>
            <div className="opt-presets">
              <div className="seg" role="tablist" aria-label="Optimize preset">
                {(["maximum", "recommended"] as const).map((name) => (
                  <button key={name} className={preset === name ? "on" : ""} onClick={() => choosePreset(name)} disabled={busy} role="tab" aria-selected={preset === name}>
                    {name === "maximum" ? "Maximum" : "Recommended"} · {bytes(plan.estimates[name]) === "-" ? "0 B" : bytes(plan.estimates[name])}
                  </button>
                ))}
              </div>
              <span className="feed-small">
                {preset === "maximum"
                  ? "Everything not answering a request right now, plus the file cache."
                  : `Unused Ollama models and model servers idle for ${Math.round(plan.thresholds.idle_seconds / 60)}+ minutes. No password needed.`}
              </span>
            </div>
            <div className="opt-list">
              {plan.items.map((item) => (
                <ItemRow key={item.id} item={item} checked={!!selected?.has(item.id)} disabled={busy || (item.kind === "file_cache" && !capabilities.purge_file_cache)} onToggle={() => toggle(item.id)} />
              ))}
              {plan.items.some((item) => item.kind === "file_cache") && !capabilities.purge_file_cache && (
                <Notice kind="info">File-cache purge is unavailable on {platform.display_name}. Model and server cleanup remains available; this dashboard will not claim to clear system cache.</Notice>
              )}
              {plan.items.length === 1 && (
                <div className="feed-small" style={{ padding: "6px 2px" }}>
                  No models are loaded right now. Clearing the file cache is the only thing left to free.
                </div>
              )}
            </div>
            {plan.apps.length > 0 && (
              <AppsList
                apps={plan.apps}
                selectedApps={selectedApps}
                chosenCount={chosenApps.length}
                quittableCount={quittableApps.length}
                quittableBytes={quittableApps.reduce((sum, a) => sum + a.bytes, 0)}
                allSelected={allAppsSelected}
                enabled={appConnected && !busy}
                appConnected={appConnected}
                bridgeLabel={bridgeLabel}
                onToggle={toggleApp}
                onToggleAll={toggleAllApps}
              />
            )}
          </>
        )
      )}
      {confirming && (
        <Confirm
          title={`Quit ${chosenApps.length} app${chosenApps.length === 1 ? "" : "s"} and optimize?`}
          message={
            <>
              {listNames(chosenApps)} will close through the {bridgeLabel.toLowerCase()}; unsaved-work prompts may remain open.
              {viewerApps.length > 0 && <> This page is open in {listNames(viewerApps)}, so it closes last, after the results are shown.</>}
            </>
          }
          confirmLabel="Quit & optimize"
          danger
          onConfirm={run}
          onClose={() => setConfirming(false)}
        />
      )}
    </Modal>
  );
}

function AppsList(props: {
  apps: AppMemory[];
  selectedApps: Set<string>;
  chosenCount: number;
  quittableCount: number;
  quittableBytes: number;
  allSelected: boolean;
  enabled: boolean;
  appConnected: boolean;
  bridgeLabel: string;
  onToggle: (path: string) => void;
  onToggleAll: () => void;
}) {
  const { apps, selectedApps, chosenCount, quittableCount, quittableBytes, allSelected, enabled, appConnected, bridgeLabel, onToggle, onToggleAll } = props;
  return (
    <>
      <div className="opt-h3-row">
        <h3 className="opt-h3">Other apps using memory</h3>
        {chosenCount > 0 && <span className="feed-small">{chosenCount} selected</span>}
      </div>
      <div className="opt-list">
        {quittableCount > 0 && (
          <label className={`opt-item opt-all ${chosenCount ? "on" : ""}`} title={appConnected ? undefined : `Needs the ModelMatrix ${bridgeLabel.toLowerCase()}`}>
            <input
              type="checkbox"
              className="check"
              checked={allSelected}
              ref={(el) => {
                if (el) el.indeterminate = chosenCount > 0 && !allSelected;
              }}
              disabled={!enabled}
              onChange={onToggleAll}
            />
            <div className="opt-main">
              <div className="opt-title">{allSelected ? "Clear selection" : `Select all apps that can quit (${quittableCount})`}</div>
            </div>
            <span className="opt-size mono">{bytes(quittableBytes)}</span>
          </label>
        )}
        {apps.map((app) => {
          const quittable = app.quittable && !!app.path;
          const checked = quittable && selectedApps.has(app.path as string);
          const meta = (
            <div className="opt-main">
              <div className="opt-title">{app.name}</div>
              <div className="feed-small">
                {app.processes} process{app.processes === 1 ? "" : "es"}
                {!quittable && ` · ${app.reason ?? "system or command-line process"}`}
              </div>
            </div>
          );
          return quittable ? (
            <label className={`opt-item ${checked ? "on" : ""}`} key={app.path} title={appConnected ? undefined : `Needs the ModelMatrix ${bridgeLabel.toLowerCase()}`}>
              <input type="checkbox" className="check" checked={checked} disabled={!enabled} onChange={() => onToggle(app.path as string)} />
              {meta}
              <span className="opt-size mono">{bytes(app.bytes)}</span>
            </label>
          ) : (
            <div className="opt-item opt-locked" key={app.path ?? app.name}>
              <span className="opt-icon">
                <Lock size={13} />
              </span>
              {meta}
              <span className="opt-size mono">{bytes(app.bytes)}</span>
            </div>
          );
        })}
      </div>
      <div className="feed-small" style={{ marginTop: 6 }}>
        {appConnected
          ? `Optimize never picks apps for you. Selected apps close through the ${bridgeLabel.toLowerCase()}, so unsaved-work prompts can still appear.`
          : `Closing apps needs the ModelMatrix ${bridgeLabel.toLowerCase()} and its quit-app capability.`}
      </div>
    </>
  );
}

function ItemRow({ item, checked, disabled, onToggle }: { item: OptimizeItem; checked: boolean; disabled: boolean; onToggle: () => void }) {
  const kindLabel = item.kind === "ollama_model" ? "Ollama" : item.kind === "model_server" ? "Model server" : "System cache";
  return (
    <label className={`opt-item ${checked ? "on" : ""}`}>
      <input type="checkbox" className="check" checked={checked} disabled={disabled} onChange={onToggle} />
      <div className="opt-main">
        <div className="opt-title">
          <span className="ellipsis">{item.title}</span>
          <span className="badge">{kindLabel}</span>
          {item.in_use && <span className="badge amber">In use</span>}
          {item.needs_password && <span className="badge">Password</span>}
        </div>
        <div className="feed-small">
          {item.detail}
          {item.last_used_at ? ` Last request ${ago(item.last_used_at)}.` : ""}
        </div>
      </div>
      <span className="opt-size mono">{item.kind === "file_cache" ? `≤ ${bytes(item.bytes)}` : bytes(item.bytes)}</span>
    </label>
  );
}

const SEGMENTS: { key: keyof MemorySnapshot; label: string; className: string }[] = [
  { key: "app_bytes", label: "App", className: "app" },
  { key: "wired_bytes", label: "Wired", className: "wired" },
  { key: "compressed_bytes", label: "Compressed", className: "compressed" },
  { key: "cached_bytes", label: "Cached files", className: "cached" },
  { key: "free_bytes", label: "Free", className: "free" },
];

function MemoryBar({ memory }: { memory: MemorySnapshot }) {
  const total = memory.total_bytes || 1;
  return (
    <div className="opt-memory">
      <div className="opt-memory-head">
        <span>
          <b className="mono">{bytes(memory.used_bytes)}</b> <span className="muted">used of {bytes(memory.total_bytes)}</span>
        </span>
        <span className={`badge ${memory.pressure === "normal" ? "green" : memory.pressure === "warning" ? "amber" : "red"}`}>
          Pressure: {memory.pressure}
        </span>
      </div>
      <div className="opt-bar" aria-hidden>
        {SEGMENTS.map((s) => (
          <i key={s.key} className={s.className} style={{ width: `${((memory[s.key] as number) / total) * 100}%` }} />
        ))}
      </div>
      <div className="legend opt-legend">
        {SEGMENTS.map((s) => (
          <span key={s.key}>
            <i className={`opt-dot ${s.className}`} /> {s.label} <b className="mono">{bytes(memory[s.key] as number)}</b>
          </span>
        ))}
        {memory.swap_used_bytes > 0 && (
          <span>
            Swap <b className="mono">{bytes(memory.swap_used_bytes)}</b>
          </span>
        )}
      </div>
    </div>
  );
}

function OutcomeView({ outcome }: { outcome: Outcome }) {
  const { before, after } = outcome;
  return (
    <div className="opt-outcome">
      <div className="opt-tiles">
        <div className="tile">
          <div className="tile-label">Freed</div>
          <div className="tile-value accent">{outcome.freed > 0 ? bytes(outcome.freed) : "0 B"}</div>
        </div>
        <div className="tile">
          <div className="tile-label">Memory used</div>
          <div className="tile-value sm mono">
            {bytes(before?.used_bytes)} → {bytes(after?.used_bytes)}
          </div>
        </div>
        <div className="tile">
          <div className="tile-label">Free</div>
          <div className="tile-value sm mono">
            {bytes(before?.free_bytes)} → {bytes(after?.free_bytes)}
          </div>
        </div>
      </div>
      <div className="opt-list">
        {outcome.steps.map((step) => (
          <div className="opt-item" key={step.id}>
            <span className={`opt-icon ${step.ok ? "ok" : step.skipped ? "" : "bad"}`}>
              {step.ok ? <CheckCircle2 size={15} /> : step.skipped ? <AlertTriangle size={15} /> : <XCircle size={15} />}
            </span>
            <div className="opt-main">
              <div className="opt-title">{step.title}</div>
              <div className="feed-small">{step.message}</div>
            </div>
            {step.ok && step.bytes ? <span className="opt-size mono">{bytes(step.bytes)}</span> : null}
          </div>
        ))}
      </div>
      {outcome.freed === 0 && (
        <div className="feed-small" style={{ marginTop: 8 }}>
          The operating system may return memory gradually, so the free figure can keep rising for a few seconds.
        </div>
      )}
    </div>
  );
}
