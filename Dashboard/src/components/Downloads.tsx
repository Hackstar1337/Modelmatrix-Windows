import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Download, Loader2, Pause, Play, Plus, RotateCcw, Trash2, X } from "lucide-react";
import { eta, hf, isActive, size, stateWords, type HFDownload, type HFDownloadedModel } from "../lib/hf";
import { useApp, usePlatform, usePoll } from "../lib/state";
import { DOWNLOADS_CHANGED, openHuggingFace } from "./HuggingFace";
import { CopyButton, Empty, Modal, Notice, SectionCard } from "./ui";
import { ConnectAppsLauncher } from "./ConnectApps";

/* ---------------- Shared poller ---------------- */

interface HFData {
  downloads: HFDownload[];
  models: HFDownloadedModel[];
  untracked: { folder: string; bytes_on_disk: number }[];
  unreachable: string[];
  loaded: boolean;
  error?: string;
  refresh: () => void;
}

const HFDataContext = createContext<HFData | null>(null);

export function useHFData(): HFData {
  const value = useContext(HFDataContext);
  if (!value) throw new Error("useHFData outside HFDataProvider");
  return value;
}

/** Polls downloads and downloaded models once for the whole dashboard: fast while something downloads. */
export function HFDataProvider({ children }: { children: ReactNode }) {
  const { page } = useApp();
  const [active, setActive] = useState(false);
  const interval = active ? 1500 : page === "servers" ? 4000 : 8000;
  const downloads = usePoll((signal) => hf.downloads(signal), interval);
  const models = usePoll((signal) => hf.models(signal), page === "servers" ? 6000 : 20000);

  const list = downloads.data?.downloads ?? [];
  const anyActive = list.some(isActive);
  useEffect(() => setActive(anyActive), [anyActive]);

  // A finished download changes the models list right away.
  const finishedKey = list
    .filter((d) => d.state === "completed")
    .map((d) => `${d.id}:${d.profile_state ?? ""}`)
    .join(",");
  useEffect(() => {
    if (downloads.data) models.refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedKey]);

  useEffect(() => {
    const onChange = () => {
      downloads.refresh();
      models.refresh();
    };
    window.addEventListener(DOWNLOADS_CHANGED, onChange);
    return () => window.removeEventListener(DOWNLOADS_CHANGED, onChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const value = useMemo<HFData>(
    () => ({
      downloads: list,
      models: models.data?.models ?? [],
      untracked: models.data?.untracked ?? [],
      unreachable: [...new Set([...(downloads.data?.unreachable ?? []), ...(models.data?.unreachable ?? [])])],
      loaded: !!downloads.data,
      error: downloads.error?.message,
      refresh: () => window.dispatchEvent(new Event(DOWNLOADS_CHANGED)),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [downloads.data, downloads.error, models.data],
  );
  return <HFDataContext.Provider value={value}>{children}</HFDataContext.Provider>;
}

/* ---------------- Header indicator ---------------- */

export function DownloadsIndicator() {
  const { downloads } = useHFData();
  const { setPage } = useApp();
  const active = downloads.filter(isActive);
  const failed = downloads.filter((d) => d.state === "failed");
  if (!active.length && !failed.length) return null;
  const done = active.reduce((sum, d) => sum + d.bytes_done, 0);
  const total = active.reduce((sum, d) => sum + d.bytes_total, 0);
  const percent = total ? Math.floor((done / total) * 100) : 0;
  return (
    <button
      className={`btn sm no-print ${failed.length && !active.length ? "danger" : ""}`}
      onClick={() => setPage("servers", "downloads")}
      title={active.map((d) => `${d.display_name}: ${stateWords(d).label.toLowerCase()}`).join("\n") || "A download failed"}
    >
      {active.length ? <Loader2 size={14} className="spin" /> : <Download size={14} />}
      {active.length ? `${active.length} downloading · ${percent}%` : `${failed.length} download${failed.length > 1 ? "s" : ""} failed`}
    </button>
  );
}

/* ---------------- Downloads list ---------------- */

const order: Record<string, number> = { downloading: 0, verifying: 0, waiting: 1, queued: 2, paused: 3, failed: 4, completed: 5, cancelled: 6 };

export function DownloadsSection() {
  const { downloads, unreachable, loaded, error } = useHFData();
  const [cancelling, setCancelling] = useState<HFDownload | null>(null);
  const sorted = [...downloads].sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || b.created_at.localeCompare(a.created_at));
  const active = downloads.filter(isActive).length;
  return (
    <SectionCard
      id="downloads"
      title="Downloads"
      meta={active ? `${active} active` : `${downloads.length} total`}
      actions={
        <button className="btn sm primary" onClick={() => openHuggingFace()}>
          <Plus size={14} /> Add from Hugging Face
        </button>
      }
    >
      {unreachable.length > 0 && <Notice kind="warn">{unreachable.join(", ")} couldn't be reached, so its downloads aren't shown.</Notice>}
      {error && !loaded && <Notice kind="bad">{error}</Notice>}
      {loaded && !sorted.length && (
        <Empty title="No downloads yet">Check a model on Hugging Face first; ModelMatrix tells you which version fits before anything downloads.</Empty>
      )}
      <div className="stack" style={{ gap: 10 }}>
        {sorted.map((job) => (
          <DownloadRow key={`${job.host ?? ""}:${job.id}`} job={job} onCancel={() => setCancelling(job)} />
        ))}
      </div>
      {cancelling && <CancelDialog job={cancelling} onClose={() => setCancelling(null)} />}
    </SectionCard>
  );
}

function progressText(job: HFDownload): string {
  const of = job.bytes_total ? `${size(job.bytes_done)} of ${size(job.bytes_total)}` : size(job.bytes_done);
  switch (job.state) {
    case "downloading":
      return [of, job.speed_bytes_per_second ? `${size(job.speed_bytes_per_second)}/s` : "", eta(job.eta_seconds)].filter(Boolean).join(" · ");
    case "waiting":
      return job.message ?? `Waiting for ${job.waiting_for ?? "a moment"}`;
    case "queued":
      return job.message ?? "Waiting for the download before it to finish";
    case "verifying":
      return job.message ?? "Checking every file against its published SHA-256";
    case "paused":
      return `${of} · paused, resumes where it stopped`;
    case "completed":
      return `${size(job.bytes_total)} · ${job.message ?? "Downloaded and verified"}`;
    default:
      return job.message ?? "";
  }
}

function DownloadRow({ job, onCancel }: { job: HFDownload; onCancel: () => void }) {
  const { toast } = useApp();
  const { refresh } = useHFData();
  const [busy, setBusy] = useState<string>();
  const [showLog, setShowLog] = useState(false);
  const words = stateWords(job);
  const percent = job.bytes_total ? Math.min(100, (job.bytes_done / job.bytes_total) * 100) : 0;
  const act = async (action: "pause" | "resume" | "start") => {
    setBusy(action);
    try {
      await hf.action(job.id, action, job.host_name);
      refresh();
    } catch (e) {
      toast(`${action === "start" ? "Start" : action === "pause" ? "Pause" : "Resume"} failed: ${(e as Error).message}`, "err");
    } finally {
      setBusy(undefined);
    }
  };
  const remove = async () => {
    setBusy("remove");
    try {
      await hf.removeFromList(job.id, job.host_name);
      refresh();
    } catch (e) {
      toast(`Couldn't remove: ${(e as Error).message}`, "err");
    } finally {
      setBusy(undefined);
    }
  };
  const canStart = job.state === "completed" && !!job.profile_state && ["needs_engine", "created", "needs_memory", "start_failed"].includes(job.profile_state);
  const icon = (name: string, node: ReactNode) => (busy === name ? <Loader2 size={13} className="spin" /> : node);
  return (
    <div className="hf-dl">
      <div className="between" style={{ alignItems: "flex-start", gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <b>{job.display_name}</b>
            <span className={`badge ${words.cls}`}>{words.label}</span>
            {job.host_name && <span className="badge">on {job.host_name}</span>}
          </div>
          <div className="muted mono hf-small">
            {job.repo} · {job.engine}
            {job.profile ? ` · profile ${job.profile}` : ""}
          </div>
        </div>
        <div className="row" style={{ flexShrink: 0 }}>
          {(job.state === "downloading" || job.state === "waiting" || job.state === "queued") && (
            <button className="btn sm" disabled={!!busy} onClick={() => act("pause")}>
              {icon("pause", <Pause size={13} />)} Pause
            </button>
          )}
          {(job.state === "paused" || job.state === "failed") && (
            <button className="btn sm" disabled={!!busy} onClick={() => act("resume")}>
              {icon("resume", job.state === "failed" ? <RotateCcw size={13} /> : <Play size={13} />)} {job.state === "failed" ? "Retry" : "Resume"}
            </button>
          )}
          {canStart && (
            <button className="btn sm primary" disabled={!!busy} onClick={() => act("start")}>
              {icon("start", <Play size={13} />)} Start and test
            </button>
          )}
          {(isActive(job) || job.state === "paused") && (
            <button className="btn sm ghost" disabled={!!busy} onClick={onCancel}>
              <X size={13} /> Cancel
            </button>
          )}
          {(job.state === "failed" || job.state === "cancelled") && (
            <button className="btn sm ghost" disabled={!!busy} onClick={remove} title="Remove from this list">
              {icon("remove", <Trash2 size={13} />)}
            </button>
          )}
        </div>
      </div>
      {job.state !== "completed" && job.state !== "cancelled" && (
        <span className="meter-track hf-bar">
          <i style={{ width: `${percent}%` }} />
        </span>
      )}
      <div className="muted hf-small">{progressText(job)}</div>
      {job.state === "completed" && job.profile_message && <div className="hf-small">{job.profile_message}</div>}
      {job.state === "completed" && job.profile_state === "ready" && job.ready_url && (
        <div className="code">
          <span>
            {job.ready_url}
            {job.ready_model ? `  ·  model "${job.ready_model}"` : ""}
          </span>
          <CopyButton text={job.ready_url} label="address" />
        </div>
      )}
      {job.error && <div className="red hf-small">{job.error}</div>}
      {job.warnings.map((w) => (
        <div key={w} className="hf-small hf-warn">
          {w}
        </div>
      ))}
      {!!job.profile_log?.length && (
        <div>
          <button className="btn ghost sm" onClick={() => setShowLog((v) => !v)}>
            {showLog ? "Hide" : "Show"} start log
          </button>
          {showLog && <pre className="hf-log">{job.profile_log.join("\n")}</pre>}
        </div>
      )}
    </div>
  );
}

function CancelDialog({ job, onClose }: { job: HFDownload; onClose: () => void }) {
  const { toast } = useApp();
  const { refresh } = useHFData();
  const [deleteFiles, setDeleteFiles] = useState(true);
  const [busy, setBusy] = useState(false);
  return (
    <Modal
      small
      title={`Cancel ${job.display_name}?`}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose} disabled={busy}>
            Keep downloading
          </button>
          <button
            className="btn danger"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await hf.action(job.id, "cancel", job.host_name, deleteFiles);
                toast(`Cancelled ${job.display_name}`, "ok");
                refresh();
                onClose();
              } catch (e) {
                toast(`Couldn't cancel: ${(e as Error).message}`, "err");
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy && <Loader2 size={14} className="spin" />} Cancel download
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="muted">
          {size(job.bytes_done)} of {size(job.bytes_total)} is downloaded{job.host_name ? ` on ${job.host_name}` : ""}.
        </div>
        {job.kind !== "ollama" && (
          <label className="row">
            <input type="checkbox" className="check" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
            Also delete the part already downloaded
          </label>
        )}
      </div>
    </Modal>
  );
}

/* ---------------- Downloaded models ---------------- */

export function DownloadedModelsSection() {
  const { models, untracked } = useHFData();
  const { localHostLabel } = usePlatform();
  const [deleting, setDeleting] = useState<HFDownloadedModel | null>(null);
  const [starting, setStarting] = useState<string>();
  const { toast } = useApp();
  const { refresh } = useHFData();
  const total = models.reduce((sum, m) => sum + m.bytes_on_disk, 0);
  const start = async (m: HFDownloadedModel) => {
    setStarting(m.id);
    try {
      await hf.action(m.id, "start", m.host_name);
      toast(`Starting ${m.profile ?? m.display_name}; it is tested with a short message`, "ok");
      refresh();
    } catch (e) {
      toast(`Start failed: ${(e as Error).message}`, "err");
    } finally {
      setStarting(undefined);
    }
  };
  return (
    <SectionCard id="downloaded-models" title="Downloaded models" meta={`${models.length} · ${size(total)}`}>
      {!models.length && <Empty title="No downloaded models yet">Models you download from Hugging Face appear here with their profile and size.</Empty>}
      {models.length > 0 && (
        <div className="hf-table-wrap">
          <table className="tbl compact">
            <thead>
              <tr>
                <th>Model</th>
                <th>Where</th>
                <th>Engine</th>
                <th className="num">On disk</th>
                <th>Profile</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <tr key={`${m.host ?? ""}:${m.id}`}>
                  <td>
                    <b>{m.display_name}</b>
                    <div className="muted mono hf-small">{m.repo}</div>
                    {m.missing_files > 0 && <div className="red hf-small">{m.missing_files} file(s) missing from the folder</div>}
                  </td>
                  <td>{m.host_name ?? localHostLabel}</td>
                  <td className="mono">{m.engine}</td>
                  <td className="num mono">{m.kind === "ollama" ? "in Ollama" : size(m.bytes_on_disk)}</td>
                  <td>
                    <span className="mono">{m.profile ?? "-"}</span>
                    {m.profile_message && <div className="muted hf-small">{m.profile_message}</div>}
                  </td>
                  <td className="num">
                    <div className="row" style={{ justifyContent: "flex-end" }}>
                      {m.profile && <ConnectAppsLauncher profileKey={m.profile} host={m.host} />}
                      {m.profile_state !== "ready" && m.profile_state !== "starting" && (
                        <button className="btn sm" disabled={starting === m.id} onClick={() => start(m)}>
                          {starting === m.id ? <Loader2 size={13} className="spin" /> : <Play size={13} />} Start
                        </button>
                      )}
                      <button
                        className="btn sm ghost"
                        onClick={() => setDeleting(m)}
                        title="Delete this model"
                        aria-label={`Delete ${m.display_name} on ${m.host_name ?? localHostLabel}`}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {untracked.length > 0 && (
        <div className="muted hf-small" style={{ marginTop: 10 }}>
          Folders in the models folder that no download owns (left alone):{" "}
          {untracked.map((u) => (
            <span key={u.folder} className="mono">
              {u.folder} ({size(u.bytes_on_disk)}){" "}
            </span>
          ))}
        </div>
      )}
      {deleting && <DeleteModelDialog model={deleting} onClose={() => setDeleting(null)} />}
    </SectionCard>
  );
}

/** Deletes a downloaded model's profile (stopping it first), and its files when asked. */
export function DeleteModelDialog({ model, onClose, onDeleted }: { model: HFDownloadedModel; onClose: () => void; onDeleted?: () => void }) {
  const { toast, refreshStatus } = useApp();
  const { refresh } = useHFData();
  const [deleteFiles, setDeleteFiles] = useState(true);
  const [busy, setBusy] = useState(false);
  const ollama = model.kind === "ollama";
  return (
    <Modal
      small
      title={`Delete ${model.display_name}?`}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn danger"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                const result = await hf.deleteModel(model.id, deleteFiles, model.host_name);
                const freed = result.freed_bytes ? `, freed ${size(result.freed_bytes)}` : "";
                const kept = result.kept_in_ollama ? " (kept in Ollama because it was there before)" : "";
                toast(`Deleted ${model.display_name}${freed}${kept}`, "ok");
                refresh();
                refreshStatus();
                onDeleted?.();
                onClose();
              } catch (e) {
                toast(`Delete failed: ${(e as Error).message}`, "err");
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy && <Loader2 size={14} className="spin" />} Delete
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="muted">
          {model.profile ? `The profile ${model.profile} is stopped and removed` : "It is removed from the list"}
          {model.host_name ? ` on ${model.host_name}` : ""}.
        </div>
        <label className="row">
          <input type="checkbox" className="check" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
          {ollama ? "Also remove it from Ollama" : `Also delete its files (${size(model.bytes_on_disk)})`}
        </label>
        {ollama && <div className="muted hf-small">A model Ollama already had before ModelMatrix downloaded it is never removed from Ollama.</div>}
        {!ollama && deleteFiles && <div className="muted mono hf-small">{model.folder}</div>}
      </div>
    </Modal>
  );
}
