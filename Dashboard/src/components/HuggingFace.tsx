import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Download, ExternalLink, Loader2, Lock, RefreshCw, Search, ShieldCheck, Wrench, XCircle } from "lucide-react";
import { compact } from "../lib/format";
import { contextLabel, fitWords, hf, size, type HFCheckReport, type HFEngineId, type HFEngineJob, type HFEnginePlan, type HFVariant } from "../lib/hf";
import { useApp, usePlatform } from "../lib/state";
import type { Gateway } from "../lib/types";
import { CopyButton, Modal, Notice } from "./ui";

const OPEN_EVENT = "modelmatrix:huggingface";
export const DOWNLOADS_CHANGED = "modelmatrix:downloads-changed";

/** Plain words for how a plan installs or removes an engine. */
function planMethodText(plan: HFEnginePlan): string {
  if (plan.method === "homebrew") return "With Homebrew, the way you would install it yourself";
  if (plan.action === "uninstall") return "Deletes the copy ModelMatrix installed";
  if (plan.engine === "mlx-lm") return "A private Python made by uv (checked against its published SHA-256). Your own Python is not changed";
  if (plan.engine === "ollama") return "The official Ollama app download";
  return "Official build, checked against its published SHA-256";
}

/** Opens "Add from Hugging Face" from anywhere, optionally with a model name and host filled in. */
export function openHuggingFace(detail: { name?: string; host?: string } = {}) {
  window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail }));
}

/** Remote hosts the controller can reach directly (from the menu bar app's host list). */
export function useRemoteHosts(): Gateway[] {
  const { bridge } = useApp();
  return useMemo(() => (bridge?.gateways ?? []).filter((g) => g.enabled && !!g.base_url), [bridge]);
}

/** Mounted once in the app shell; renders the dialog while it is open. */
export function HuggingFaceHost() {
  const [open, setOpen] = useState<{ name?: string; host?: string } | null>(null);
  useEffect(() => {
    const show = (e: Event) => setOpen((e as CustomEvent).detail ?? {});
    window.addEventListener(OPEN_EVENT, show);
    // Deep link: #/servers/huggingface?name=owner/model&host=name (menu bar app and CLI).
    const match = window.location.hash.match(/\/huggingface\b(?:\?(.*))?/);
    if (match) {
      const params = new URLSearchParams(match[1] ?? "");
      setOpen({ name: params.get("name") ?? undefined, host: params.get("host") ?? undefined });
    }
    return () => window.removeEventListener(OPEN_EVENT, show);
  }, []);
  const close = () => {
    setOpen(null);
    if (/\/huggingface\b/.test(window.location.hash)) history.replaceState(null, "", window.location.hash.replace(/\/huggingface\b.*/, ""));
  };
  return open ? <AddModelModal initial={open} onClose={close} /> : null;
}

const CONTEXTS = [4096, 8192, 16384, 32768, 65536, 131072];
const LOW_QUALITY_BITS = 3.5;

function defaultVariantLabel(report: HFCheckReport): string | undefined {
  const recommendation = report.recommendation?.label;
  if (recommendation && report.variants.some((v) => v.label === recommendation && v.supported)) return recommendation;
  return (
    report.variants.find((v) => v.supported && v.fit?.level !== "too_big")?.label ??
    report.variants.find((v) => v.supported)?.label
  );
}

function needsDownloadConfirmation(variant?: HFVariant): boolean {
  return variant?.fit?.level === "too_big" || (variant?.bits_per_weight !== undefined && variant.bits_per_weight < LOW_QUALITY_BITS);
}

function kindDescription(task: NonNullable<HFCheckReport["task"]>): string {
  const description = task.description.trim();
  return task.vision && !/\b(?:image|images|vision)\b/i.test(description) ? `${description} that can also read images` : description;
}

function AddModelModal({ initial, onClose }: { initial: { name?: string; host?: string }; onClose: () => void }) {
  const { toast, setPage } = useApp();
  const { localHostLabel } = usePlatform();
  const hosts = useRemoteHosts();
  const [host, setHost] = useState(initial.host ?? "");
  const [name, setName] = useState(initial.name ?? "");
  const [context, setContext] = useState<number | undefined>(undefined);
  const [report, setReport] = useState<HFCheckReport>();
  const [error, setError] = useState<string>();
  const [checking, setChecking] = useState(false);
  const [selected, setSelected] = useState<string>();
  const [engineChoice, setEngineChoice] = useState<string>();
  const [installing, setInstalling] = useState<{ engine: HFEngineId; name: string } | null>(null);
  const [confirmDownload, setConfirmDownload] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const abort = useRef<AbortController | null>(null);

  const check = useCallback(async (value: string, ctx: number | undefined, target: string) => {
    const text = value.trim();
    if (!text) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setChecking(true);
    setError(undefined);
    try {
      const result = await hf.check(text, target || undefined, ctx, controller.signal);
      setReport(result);
      setSelected(defaultVariantLabel(result));
      setEngineChoice(undefined);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setReport(undefined);
      setError((e as Error).message);
    } finally {
      if (abort.current === controller) setChecking(false);
    }
  }, []);

  useEffect(() => {
    if (initial.name) check(initial.name, undefined, initial.host ?? "");
    return () => abort.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const recheck = (overrides: { name?: string; context?: number | null; host?: string } = {}) => {
    const nextName = overrides.name ?? (report?.kind === "model" && report.repo ? report.repo.id : name);
    const nextContext = overrides.context === null ? undefined : (overrides.context ?? context);
    const nextHost = overrides.host ?? host;
    if (overrides.name) setName(overrides.name);
    check(nextName, nextContext, nextHost);
  };

  const variant = report?.variants.find((v) => v.label === selected);
  const loginNeeded = !!report?.repo?.login_required;
  const canDownload = !!report && report.kind === "model" && report.download_available && !!variant && variant.supported && !loginNeeded;
  const memoryWarning = variant?.fit?.level === "too_big";
  const qualityWarning = variant?.bits_per_weight !== undefined && variant.bits_per_weight < LOW_QUALITY_BITS;
  const where = host || localHostLabel;

  const startDownload = async () => {
    if (!report?.repo || !variant) return;
    setDownloading(true);
    try {
      const result = await hf.download({ name: report.repo.id, version: variant.label, context, host: host || undefined, engine: engineChoice });
      toast(result.existing ? `${result.download.display_name} is already in Downloads` : `Downloading ${result.download.display_name} to ${where}`, "ok");
      window.dispatchEvent(new Event(DOWNLOADS_CHANGED));
      onClose();
      setPage("servers", "downloads");
    } catch (e) {
      toast(`Download didn't start: ${(e as Error).message}`, "err");
    } finally {
      setDownloading(false);
    }
  };

  const maxContext = report?.model?.max_context;
  const contextChoices = CONTEXTS.filter((c) => !maxContext || c <= maxContext);

  return (
    <>
      <Modal
        title="Add a model from Hugging Face"
        onClose={onClose}
        footer={
          <>
            <span className="muted hf-foot-note">Nothing is downloaded until you press Download.</span>
            <button className="btn ghost" onClick={onClose}>
              Close
            </button>
            <button
              className="btn primary"
              disabled={!canDownload || downloading || checking}
              title={!canDownload ? "Check a model and choose a version first" : undefined}
              onClick={() => (needsDownloadConfirmation(variant) ? setConfirmDownload(true) : startDownload())}
            >
              {downloading ? <Loader2 size={14} className="spin" /> : <Download size={14} />}
              {variant && canDownload ? `Download ${variant.label} (${size(variant.download_bytes)})` : "Download"}
            </button>
          </>
        }
      >
        <div className="stack">
          <div className="hf-target">
            <span className="label">Where to run it</span>
            <div className="seg sm">
              <button className={host === "" ? "on" : ""} onClick={() => (setHost(""), report && recheck({ host: "" }))}>
                {localHostLabel}
              </button>
              {hosts.map((g) => (
                <button key={g.id} className={host === g.name ? "on" : ""} onClick={() => (setHost(g.name), report && recheck({ host: g.name }))}>
                  {g.name}
                </button>
              ))}
            </div>
          </div>

          <form
            className="row hf-search"
            onSubmit={(e) => {
              e.preventDefault();
              setContext(undefined);
              check(name, undefined, host);
            }}
          >
            <input
              className="input mono"
              autoFocus
              placeholder="owner/model, a huggingface.co link, or words to search"
              value={name}
              onChange={(e) => setName(e.target.value)}
              aria-label="Hugging Face model"
            />
            <button className="btn primary" disabled={checking || !name.trim()}>
              {checking ? <Loader2 size={14} className="spin" /> : <Search size={14} />} Check
            </button>
          </form>

          {checking && !report && (
            <div className="muted row">
              <Loader2 size={14} className="spin" /> Checking on Hugging Face. Only the file list and model settings are read.
            </div>
          )}
          {error && <Notice kind="bad">{error}</Notice>}
          {report && (
            <div className={`stack ${checking ? "hf-dim" : ""}`}>
              <VerdictBanner report={report} />
              {report.kind === "search" ? (
                <AlternativeList title="Matching models" items={report.search_results} onPick={(repo) => recheck({ name: repo, context: null })} />
              ) : (
                <>
                  <Facts report={report} />
                  {loginNeeded && (
                    <Notice kind="warn">
                      This model needs a Hugging Face login. Accept its terms on{" "}
                      <a href={report.repo?.url} target="_blank" rel="noreferrer">
                        its page
                      </a>
                      , then save a Hugging Face token in{" "}
                      <a
                        href="#/settings/huggingface"
                        onClick={(e) => {
                          e.preventDefault();
                          onClose();
                          setPage("settings", "huggingface");
                        }}
                      >
                        Settings
                      </a>{" "}
                      and check again.
                    </Notice>
                  )}
                  <EngineBlock report={report} where={where} onInstall={(engine, engineName) => setInstalling({ engine, name: engineName })} onRecheck={() => recheck()} />
                  {report.engine?.backup && report.engine.engines.find((e) => e.id === report.engine?.backup)?.installed && report.variants.length > 0 && (
                    <div className="hf-target">
                      <span className="label">Run it with</span>
                      <div className="seg sm">
                        <button className={!engineChoice ? "on" : ""} onClick={() => setEngineChoice(undefined)}>
                          {report.engine.needed_name ?? report.engine.needed} (recommended)
                        </button>
                        <button className={engineChoice === report.engine.backup ? "on" : ""} onClick={() => setEngineChoice(report.engine?.backup)}>
                          {report.engine.engines.find((e) => e.id === report.engine?.backup)?.name}
                        </button>
                      </div>
                    </div>
                  )}
                  {report.variants.length > 0 && (
                    <Versions
                      report={report}
                      selected={selected}
                      onSelect={setSelected}
                      context={context}
                      contextChoices={contextChoices}
                      onContext={(value) => {
                        setContext(value);
                        recheck({ context: value ?? null });
                      }}
                    />
                  )}
                  {report.alternatives.length > 0 && (
                    <AlternativeList title="Ready-to-run versions of this model" items={report.alternatives} onPick={(repo) => recheck({ name: repo, context: null })} />
                  )}
                  {report.notes.length > 0 && (
                    <ul className="hf-notes muted">
                      {report.notes.map((n) => (
                        <li key={n}>{n}</li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </Modal>
      {installing && (
        <EngineInstallDialog
          engine={installing.engine}
          engineName={installing.name}
          action="install"
          host={host || undefined}
          onClose={() => setInstalling(null)}
          onDone={() => recheck()}
        />
      )}
      {confirmDownload && variant && report && (
        <Modal
          small
          title={memoryWarning ? "This version may not fit in memory" : "Review this version"}
          onClose={() => setConfirmDownload(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setConfirmDownload(false)}>
                Choose another
              </button>
              <button
                className="btn danger"
                onClick={() => {
                  setConfirmDownload(false);
                  startDownload();
                }}
              >
                Download anyway
              </button>
            </>
          }
        >
          <div className="muted" style={{ fontSize: 14 }}>
            {qualityWarning && (
              <p>
                <b>Quality suggestion.</b> {variant.label} is about {variant.bits_per_weight?.toFixed(1)} bits per weight. Lower-bit versions use less memory, but can lose response quality and detail.
              </p>
            )}
            {memoryWarning && variant.fit && (
              <p>
                <b>Memory suggestion.</b> This version needs about {size(variant.fit.need_bytes)} at {contextLabel(variant.fit.context)} context, while {where} can give a model about {size(report.machine.budget_bytes)}. It may fail to start or run very slowly.
              </p>
            )}
            <p>Download anyway uses this selected version explicitly. Nothing is installed.</p>
          </div>
        </Modal>
      )}
    </>
  );
}

function verdictTone(code: string): "ok" | "warn" | "bad" {
  if (code === "ready") return "ok";
  if (code === "not_supported" || code === "not_found") return "bad";
  return "warn";
}

function VerdictBanner({ report }: { report: HFCheckReport }) {
  const tone = verdictTone(report.verdict.code);
  const Icon = tone === "ok" ? CheckCircle2 : tone === "bad" ? XCircle : AlertTriangle;
  return (
    <div className={`hf-verdict ${tone}`}>
      <Icon size={20} />
      <div>
        <b>{report.verdict.title}</b>
        <div>{report.verdict.detail}</div>
      </div>
    </div>
  );
}

function Facts({ report }: { report: HFCheckReport }) {
  const { repo, format, task, model, machine } = report;
  const { localHostLabel } = usePlatform();
  return (
    <dl className="kv hf-facts">
      {repo && (
        <>
          <dt>Model</dt>
          <dd>
            <a href={repo.url} target="_blank" rel="noreferrer">
              {repo.id} <ExternalLink size={11} />
            </a>
            {repo.sha && <span className="muted"> · {repo.sha.slice(0, 7)}</span>}
            {repo.gated && (
              <span className="badge amber" style={{ marginLeft: 8 }}>
                <Lock size={10} /> login
              </span>
            )}
          </dd>
        </>
      )}
      {format && (
        <>
          <dt>Format</dt>
          <dd className="hf-plain">
            <b>{format.name}</b> <span className="muted">{format.summary}</span>
          </dd>
        </>
      )}
      {task && (
        <>
          <dt>Kind</dt>
          <dd className="hf-plain">
            {kindDescription(task)}
          </dd>
        </>
      )}
      {model && (model.parameters || model.max_context) && (
        <>
          <dt>Size</dt>
          <dd className="hf-plain">
            {model.parameters ? `${compact(model.parameters)} parameters` : "Parameters unknown"}
            {model.max_context ? ` · up to ${contextLabel(model.max_context)} context` : ""}
          </dd>
        </>
      )}
      {repo && (
        <>
          <dt>License</dt>
          <dd className="hf-plain">{repo.license ?? "Not stated"}</dd>
        </>
      )}
      <dt>{machine.host_name ? "Host" : localHostLabel}</dt>
      <dd className="hf-plain">
        {machine.host_name ? `${machine.host_name}: ` : ""}
        {machine.chip ?? machine.architecture}, {size(machine.memory_bytes)} memory, about {size(machine.budget_bytes)} usable for models
        {machine.accelerator === "cpu" ? " (processor only)" : ""}
        {machine.free_disk_bytes ? ` · ${size(machine.free_disk_bytes)} free disk` : ""}
      </dd>
      {report.download_folder && (
        <>
          <dt>Downloads to</dt>
          <dd>{report.download_folder}</dd>
        </>
      )}
    </dl>
  );
}

function EngineBlock({
  report,
  where,
  onInstall,
  onRecheck,
}: {
  report: HFCheckReport;
  where: string;
  onInstall: (engine: HFEngineId, name: string) => void;
  onRecheck: () => void;
}) {
  const summary = report.engine;
  if (!summary?.needed) return null;
  const needed = summary.engines.find((e) => e.id === summary.needed);
  if (!needed) return null;
  if (needed.installed) {
    return (
      <div className="hf-engine ok">
        <ShieldCheck size={16} />
        <div>
          <b>
            {needed.name} {needed.version ?? ""}
          </b>{" "}
          is installed on {where}
          {needed.installed_by ? ` (${needed.installed_by})` : ""}.<div className="muted mono hf-small">{needed.path}</div>
        </div>
      </div>
    );
  }
  const install = needed.install;
  const manual = !install || install.method === "manual" || !summary.installer_available;
  return (
    <div className="hf-engine warn">
      <Wrench size={16} />
      <div className="stack" style={{ gap: 8, flex: 1 }}>
        <div>
          <b>{needed.name}</b> is needed to run this model and isn't installed on {where} yet.
          {install && <div className="muted hf-small">{install.summary}</div>}
        </div>
        {manual && install?.command && (
          <div className="code">
            <span>{install.command}</span>
            <CopyButton text={install.command} label="command" />
          </div>
        )}
        <div className="row">
          {!manual && (
            <button className="btn primary sm" onClick={() => onInstall(needed.id, needed.name)}>
              <Download size={13} /> Install {needed.name}…
            </button>
          )}
          <button className="btn ghost sm" onClick={onRecheck}>
            <RefreshCw size={13} /> Check again
          </button>
          <span className="muted hf-small">You can download now; the model starts once {needed.name} is installed.</span>
        </div>
      </div>
    </div>
  );
}

function Versions({
  report,
  selected,
  onSelect,
  context,
  contextChoices,
  onContext,
}: {
  report: HFCheckReport;
  selected?: string;
  onSelect: (label: string) => void;
  context?: number;
  contextChoices: number[];
  onContext: (value: number | undefined) => void;
}) {
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="between">
        <b>Versions ({report.variants.length})</b>
        <label className="row muted hf-small">
          Context
          <select className="select" value={context ?? ""} onChange={(e) => onContext(e.target.value ? Number(e.target.value) : undefined)}>
            <option value="">Recommended</option>
            {contextChoices.map((c) => (
              <option key={c} value={c}>
                {contextLabel(c)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="hf-table-wrap">
        <table className="tbl compact hf-versions">
          <thead>
            <tr>
              <th />
              <th>Version</th>
              <th>Quality</th>
              <th className="num">Download</th>
              <th className="num">Memory need</th>
              <th>Fit</th>
              <th className="num" title="Longest chat memory (context) that fits well">Best context</th>
            </tr>
          </thead>
          <tbody>
            {report.variants.map((v) => (
              <VersionRow key={v.label} variant={v} checked={v.label === selected} onSelect={() => v.supported && onSelect(v.label)} />
            ))}
          </tbody>
        </table>
      </div>
      {report.recommendation && <div className="muted hf-small">Recommended: {report.recommendation.label}. {report.recommendation.reason} Memory figures are estimates.</div>}
    </div>
  );
}

function VersionRow({ variant: v, checked, onSelect }: { variant: HFVariant; checked: boolean; onSelect: () => void }) {
  const fit = fitWords(v.fit?.level);
  return (
    <tr className={`${!v.supported ? "hf-off" : ""} ${checked ? "hf-on" : ""}`} onClick={onSelect} title={v.unsupported_reason}>
      <td>
        <input type="radio" className="check" checked={checked} disabled={!v.supported} onChange={onSelect} aria-label={`Choose ${v.label}`} />
      </td>
      <td className="mono">
        {v.label}
        {v.recommended && <span className="badge green hf-tag">recommended</span>}
        {v.full_precision && <span className="badge hf-tag">full size</span>}
        {v.files.length > 1 && <span className="muted hf-small"> · {v.files.length} files</span>}
      </td>
      <td className="hf-wrap">
        {v.supported ? v.quality : <span className="muted">{v.unsupported_reason ?? "Not supported"}</span>}
        {v.bits_per_weight ? <span className="muted hf-small"> · {v.bits_per_weight.toFixed(1)} bits</span> : null}
      </td>
      <td className="num mono">
        {size(v.download_bytes)}
        {v.fits_on_disk === false && <div className="red hf-small">not enough disk</div>}
      </td>
      <td className="num mono">{v.fit ? `${size(v.fit.need_bytes)} @ ${contextLabel(v.fit.context)}` : "-"}</td>
      <td>{v.supported && v.fit ? <span className={`badge ${fit.cls}`}>{fit.label}</span> : "-"}</td>
      <td className="num mono">{contextLabel(v.longest_good_context)}</td>
    </tr>
  );
}

function AlternativeList({ title, items, onPick }: { title: string; items: HFCheckReport["alternatives"]; onPick: (repo: string) => void }) {
  if (!items.length) return <Notice>No matching models found on Hugging Face.</Notice>;
  return (
    <div className="stack" style={{ gap: 8 }}>
      <b>{title}</b>
      <table className="tbl compact">
        <tbody>
          {items.map((a) => (
            <tr key={a.repo}>
              <td className="mono">
                {a.repo}
                {a.gated && (
                  <span className="badge amber hf-tag">
                    <Lock size={10} /> login
                  </span>
                )}
                {a.same_model === false && <span className="badge hf-tag">modified version</span>}
              </td>
              <td>
                <span className="badge blue">{a.format.toUpperCase()}</span>
              </td>
              <td className="num muted">{a.downloads !== undefined ? `${compact(a.downloads)} downloads` : ""}</td>
              <td className="num">
                <button className="btn sm" onClick={() => onPick(a.repo)}>
                  Check
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------------- Engine install, update and removal ---------------- */

/**
 * Shows exactly what will happen, and only does it after "Install" (or Update/Remove) is pressed.
 * The controller's plan id is single-use and expires after 10 minutes.
 */
export function EngineInstallDialog({
  engine,
  engineName,
  action,
  host,
  onClose,
  onDone,
}: {
  engine: HFEngineId;
  engineName: string;
  action: "install" | "update" | "uninstall";
  host?: string;
  onClose: () => void;
  onDone?: () => void;
}) {
  const { toast } = useApp();
  const { localHostLabel } = usePlatform();
  const [plan, setPlan] = useState<HFEnginePlan>();
  const [error, setError] = useState<string>();
  const [job, setJob] = useState<HFEngineJob>();
  const [lines, setLines] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);
  const verb = action === "install" ? "Install" : action === "update" ? "Update" : "Remove";
  const where = host ?? localHostLabel;

  useEffect(() => {
    let cancelled = false;
    hf.plan(engine, action, host)
      .then((r) => !cancelled && setPlan(r.plan))
      .catch((e) => !cancelled && setError((e as Error).message));
    return () => {
      cancelled = true;
    };
  }, [engine, action, host]);

  const jobId = job?.id;
  const running = job?.state === "running";
  useEffect(() => {
    if (!jobId || !running) return;
    let stopped = false;
    let next = 0;
    const tick = async () => {
      try {
        const result = await hf.job(jobId, next);
        if (stopped) return;
        if (result.lines?.length) setLines((l) => [...l, ...(result.lines ?? [])].slice(-400));
        next = result.next ?? next;
        setJob(result.job);
        if (result.job.state !== "running") {
          if (result.job.state === "succeeded") {
            toast(`${engineName}: ${verb.toLowerCase()} finished on ${where}`, "ok");
            onDone?.();
          }
          return;
        }
      } catch (e) {
        if (!stopped) setError((e as Error).message);
      }
      if (!stopped) window.setTimeout(tick, 900);
    };
    tick();
    return () => {
      stopped = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, running]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [lines]);

  const approve = async () => {
    if (!plan) return;
    setBusy(true);
    setError(undefined);
    try {
      const result = await hf.run(plan.id);
      setLines([]);
      setJob(result.job);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      const result = await hf.cancelJob(job.id);
      setJob(result.job);
    } catch (e) {
      toast(`Couldn't stop: ${(e as Error).message}`, "err");
    }
  };

  const finished = job && job.state !== "running";
  return (
    <Modal
      title={`${verb} ${engineName} on ${where}`}
      onClose={running ? () => undefined : onClose}
      footer={
        !job ? (
          <>
            <button className="btn ghost" onClick={onClose}>
              {action === "uninstall" ? "Keep it" : "Don't install"}
            </button>
            <button className={`btn ${action === "uninstall" ? "danger" : "primary"}`} disabled={!plan || busy} onClick={approve}>
              {busy && <Loader2 size={14} className="spin" />}
              {verb}
            </button>
          </>
        ) : running ? (
          <button className="btn ghost" onClick={cancel}>
            Stop
          </button>
        ) : (
          <button className="btn primary" onClick={onClose}>
            Done
          </button>
        )
      }
    >
      <div className="stack">
        {!plan && !error && (
          <div className="muted row">
            <Loader2 size={14} className="spin" /> Preparing the plan. Nothing is installed yet.
          </div>
        )}
        {error && <Notice kind="bad">{error}</Notice>}
        {plan && !job && (
          <>
            <b>{plan.title}</b>
            <dl className="kv hf-facts">
              <dt>How</dt>
              <dd className="hf-plain">{planMethodText(plan)}</dd>
              {plan.downloads.length > 0 && (
                <>
                  <dt>Downloads</dt>
                  <dd>
                    {plan.downloads.map((d) => (
                      <div key={d.url}>
                        {d.name} <span className="muted">({size(d.size)})</span>
                      </div>
                    ))}
                  </dd>
                </>
              )}
              <dt>Download size</dt>
              <dd className="hf-plain">{plan.download_size}</dd>
              {plan.commands.length > 0 && (
                <>
                  <dt>Runs</dt>
                  <dd>
                    {plan.commands.map((c) => (
                      <div key={c}>{c}</div>
                    ))}
                  </dd>
                </>
              )}
              <dt>{action === "uninstall" ? "Removes" : "Installs to"}</dt>
              <dd>{plan.location}</dd>
              {plan.removal && action !== "uninstall" && (
                <>
                  <dt>To remove later</dt>
                  <dd>{plan.removal}</dd>
                </>
              )}
              <dt>Admin password</dt>
              <dd className="hf-plain">{plan.needs_admin ? "Needed" : "Not needed"}</dd>
            </dl>
            {plan.notes.length > 0 && (
              <ul className="hf-notes muted">
                {plan.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            )}
            <div className="muted hf-small">Nothing changes until you press {verb}. This plan expires in 10 minutes.</div>
          </>
        )}
        {job && (
          <>
            <div className="row">
              {running ? <Loader2 size={14} className="spin" /> : job.state === "succeeded" ? <CheckCircle2 size={16} className="green" /> : <XCircle size={16} className="red" />}
              <b>{running ? `${verb === "Remove" ? "Removing" : verb + "ing"} ${engineName}…` : job.state === "succeeded" ? "Finished" : job.state === "cancelled" ? "Stopped" : "Failed"}</b>
            </div>
            <pre className="hf-log" ref={logRef}>
              {lines.length ? lines.join("\n") : "Starting…"}
            </pre>
            {finished && job.error && <Notice kind="bad">{job.error}</Notice>}
            {finished && job.manual_command && (
              <div className="code">
                <span>{job.manual_command}</span>
                <CopyButton text={job.manual_command} label="command" />
              </div>
            )}
            {job.state === "succeeded" && job.path && <div className="muted mono hf-small">{job.path}</div>}
          </>
        )}
      </div>
    </Modal>
  );
}
