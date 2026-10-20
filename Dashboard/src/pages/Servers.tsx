import { useMemo, useState } from "react";
import {
  CopyPlus,
  Gauge,
  Link2,
  Loader2,
  Pause,
  Pencil,
  Play,
  Plus,
  Power,
  RefreshCw,
  RotateCcw,
  Search,
  Square,
  Trash2,
  UploadCloud,
  Zap,
  PackagePlus,
} from "lucide-react";
import { api, post, remote } from "../lib/api";
import { compact, num, seconds } from "../lib/format";
import { useApp, usePlatform, usePoll } from "../lib/state";
import type { BenchmarkStatus, Gateway, ProfileStatus, StatusPayload } from "../lib/types";
import { ProfileEditor } from "../components/ProfileEditor";
import { Confirm, CopyButton, Empty, Modal, Notice, SectionCard, StatusPill, Switch } from "../components/ui";
import { ConnectAppsLauncher, ConnectAppsSection } from "../components/ConnectApps";
import { DeleteModelDialog, DownloadedModelsSection, DownloadsSection, useHFData } from "../components/Downloads";
import { openHuggingFace } from "../components/HuggingFace";

/* ---------------- Profile card ---------------- */

function ProfileCard({
  status,
  gateway,
  selected,
  onSelect,
  onEdit,
  onDuplicate,
  onDelete,
  capture,
}: {
  status: ProfileStatus;
  gateway?: string;
  selected?: boolean;
  onSelect?: (on: boolean) => void;
  onEdit?: () => void;
  onDuplicate?: () => void;
  onDelete?: () => void;
  capture?: { enabled: boolean; supported: boolean; port?: number };
}) {
  const { pending, profileAction, startBenchmark, settings, overview, toast, refreshStatus } = useApp();
  const key = gateway ? `${gateway}:${status.profile}` : status.profile;
  const busy = pending[key];
  const advanced = settings.mode === "advanced";
  const relayUrl = overview?.relay?.url;
  const isActive = status.running && status.ready;
  return (
    <div className={`card profile ${isActive ? "active" : ""}`}>
      <div className="profile-head">
        <div className="row" style={{ alignItems: "flex-start", gap: 10, minWidth: 0 }}>
          {onSelect && <input type="checkbox" className="check" checked={!!selected} onChange={(e) => onSelect(e.target.checked)} aria-label={`Select ${status.profile}`} style={{ marginTop: 3 }} />}
          <div style={{ minWidth: 0 }}>
            <div className="profile-name">
              <span className="ellipsis">{status.display_name || status.profile}</span>
            </div>
            <div className="profile-id">{status.profile}</div>
          </div>
        </div>
        <StatusPill status={status} pending={busy} />
      </div>
      <div className="row wrap" style={{ gap: 6 }}>
        <span className="badge orange">{status.runtime_label || status.runtime}</span>
        {(status.runtime_tags ?? []).slice(0, advanced ? 6 : 2).map((tag) => (
          <span key={tag} className="badge">
            {tag}
          </span>
        ))}
        {capture?.enabled && <span className="badge green">CAPTURED :{capture.port}</span>}
      </div>
      <div className="profile-meta">
        <span>Model</span>
        <span>{status.request_model || "-"}</span>
        <span>Endpoint</span>
        <span title={status.base_url}>{status.base_url || "-"}</span>
        {status.running && (
          <>
            <span>Memory</span>
            <span>{status.rss_mb ? `${compact(status.rss_mb)} MB` : "-"}</span>
          </>
        )}
        {advanced && (
          <>
            <span>PID</span>
            <span>{status.pid ?? "-"}</span>
            <span>Launch</span>
            <span>{status.launch_mode || "-"}</span>
            <span>Server IDs</span>
            <span>{(status.server_ids ?? []).join(", ") || "-"}</span>
            {status.log_path && (
              <>
                <span>Log</span>
                <span title={status.log_path}>{status.log_path}</span>
              </>
            )}
          </>
        )}
      </div>
      {!!status.missing_artifacts?.length && <Notice kind="bad">Missing: {status.missing_artifacts.join(", ")}</Notice>}
      <div className="profile-actions">
        <button className="btn sm primary" disabled={!!busy || isActive} onClick={() => profileAction(status.profile, "switch", gateway)} title="Stop other models and run this one">
          <Zap size={13} /> Activate
        </button>
        {status.running ? (
          <button className="btn sm" disabled={!!busy} onClick={() => profileAction(status.profile, "stop", gateway)}>
            <Square size={12} /> Stop
          </button>
        ) : (
          <button className="btn sm" disabled={!!busy} onClick={() => profileAction(status.profile, "start", gateway)}>
            <Play size={12} /> Start
          </button>
        )}
        <button className="btn sm" disabled={!!busy || !status.running} onClick={() => profileAction(status.profile, "restart", gateway)}>
          <RotateCcw size={12} /> Restart
        </button>
        <button className="btn sm" disabled={!!busy} onClick={() => startBenchmark([status.profile], "quick", {}, gateway)}>
          <Gauge size={13} /> Bench
        </button>
        <ConnectAppsLauncher profileKey={key} host={gateway} />
        {status.base_url && <CopyButton text={status.base_url} label="URL" />}
        {!gateway && relayUrl && <CopyButton text={relayUrl} label="Relay" />}
        <span className="grow" />
        {capture && capture.supported && (
          <span className="row" style={{ gap: 6 }} title="Capture mode: record usage from apps that call this model's own port">
            <span className="feed-small">Capture</span>
            <Switch
              on={capture.enabled}
              label="Capture mode"
              onChange={async (on) => {
                try {
                  await post("/api/profiles/capture", { profile: status.profile, enabled: on });
                  toast(on ? `Capture on for ${status.profile} (restart it to apply)` : `Capture off for ${status.profile}`, "ok");
                  refreshStatus();
                } catch (e) {
                  toast((e as Error).message, "err");
                }
              }}
            />
          </span>
        )}
        {onEdit && (
          <button className="btn ghost sm icon" onClick={onEdit} title="Edit profile" aria-label="Edit profile">
            <Pencil size={14} />
          </button>
        )}
        {onDuplicate && (
          <button className="btn ghost sm icon" onClick={onDuplicate} title="Duplicate profile" aria-label="Duplicate profile">
            <CopyPlus size={14} />
          </button>
        )}
        {onDelete && (
          <button className="btn ghost sm icon" onClick={onDelete} title="Delete profile" aria-label="Delete profile" disabled={status.running}>
            <Trash2 size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

/* ---------------- Benchmarks ---------------- */

function BenchmarkPanel({ benchmark, selected, gateway }: { benchmark?: BenchmarkStatus; selected: string[]; gateway?: string }) {
  const { startBenchmark } = useApp();
  const [suite, setSuite] = useState("quick");
  const [concurrent, setConcurrent] = useState(false);
  const [keep, setKeep] = useState(false);
  const rows = benchmark?.latest?.rows ?? [];
  return (
    <SectionCard
      id={gateway ? undefined : "benchmarks"}
      title={
        <>
          Benchmarks {benchmark?.running && <Loader2 size={14} className="spin accent" />}
        </>
      }
      meta={benchmark?.running ? "running" : benchmark?.latest?.generated_at ? `last run ${new Date(benchmark.latest.generated_at).toLocaleString()}` : "no runs yet"}
    >
      <div className="row wrap" style={{ marginBottom: 14, gap: 14 }}>
        <div className="seg sm">
          {["quick", "context"].map((s) => (
            <button key={s} className={suite === s ? "on" : ""} onClick={() => setSuite(s)}>
              {s}
            </button>
          ))}
        </div>
        <label className="row feed-small" style={{ gap: 6 }}>
          <Switch on={concurrent} onChange={setConcurrent} label="Allow concurrent" /> run models side by side
        </label>
        <label className="row feed-small" style={{ gap: 6 }}>
          <Switch on={keep} onChange={setKeep} label="Keep running" /> keep models running after
        </label>
        <span className="grow" />
        <button className="btn" disabled={benchmark?.running || !selected.length} onClick={() => startBenchmark(selected, suite, { allow_concurrent: concurrent, keep_running: keep }, gateway)}>
          <Gauge size={14} /> Benchmark selected ({selected.length})
        </button>
        <button className="btn primary" disabled={benchmark?.running} onClick={() => startBenchmark(null, suite, { allow_concurrent: concurrent, keep_running: keep }, gateway)}>
          <Gauge size={14} /> Benchmark all
        </button>
      </div>
      {rows.length ? (
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Profile</th>
                <th>Runtime</th>
                <th className="num">TTFT</th>
                <th className="num">Decode t/s</th>
                <th className="num">End-to-end t/s</th>
                <th className="num">Memory</th>
                <th>Context cases</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.profile}>
                  <td className="mono">{row.profile}</td>
                  <td>{row.runtime}</td>
                  <td className="num">{seconds(row.ttft_ms)}</td>
                  <td className="num accent">{num(row.decode_tokens_per_sec)}</td>
                  <td className="num">{num(row.e2e_tokens_per_sec)}</td>
                  <td className="num">{row.rss_mb ? `${compact(row.rss_mb)} MB` : "-"}</td>
                  <td className="wrap feed-small">
                    {(row.prefill_cases ?? []).map((c) => `${c.label}: ${seconds(c.ttft_ms)} / ${num(c.decode_tokens_per_sec)} t/s`).join("  ·  ") || "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty title="No benchmark results yet">Run a benchmark to compare time-to-first-token and tokens per second across your models.</Empty>
      )}
      {benchmark?.log_path && <div className="feed-small" style={{ marginTop: 10 }}>Log: {benchmark.log_path}</div>}
      {benchmark?.latest?.markdown_path && <div className="feed-small">Report: {benchmark.latest.markdown_path}</div>}
    </SectionCard>
  );
}

/* ---------------- Remote hosts ---------------- */

function GatewayForm({ gateway, onClose }: { gateway?: Gateway; onClose: () => void }) {
  const { bridgeCommand, toast } = useApp();
  const { platform } = usePlatform();
  const [mode, setMode] = useState<"manual" | "pair">(gateway ? "manual" : "pair");
  const [form, setForm] = useState({
    name: gateway?.name ?? "",
    kind: gateway?.kind ?? "direct",
    base_url: (gateway?.base_url as string) ?? "",
    token: "",
    ssh_target: (gateway?.ssh_target as string) ?? "",
    link: "",
  });
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form, v: string) => setForm((f) => ({ ...f, [k]: v }));
  async function submit() {
    setBusy(true);
    try {
      if (mode === "pair") await bridgeCommand("gateway.pair", { link: form.link.trim() });
      else if (gateway) await bridgeCommand("gateway.update", { id: gateway.id, ...form });
      else await bridgeCommand("gateway.add", form);
      toast(gateway ? `Saved ${form.name}` : "Remote host added", "ok");
      onClose();
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={gateway ? `Edit ${gateway.name}` : "Add remote host"}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn primary" onClick={submit} disabled={busy}>
            {busy && <Loader2 size={14} className="spin" />} {gateway ? "Save" : mode === "pair" ? "Pair host" : "Add host"}
          </button>
        </>
      }
    >
      <div className="stack">
        {!gateway && (
          <div className="seg sm">
            <button className={mode === "pair" ? "on" : ""} onClick={() => setMode("pair")}>
              Pairing link
            </button>
            <button className={mode === "manual" ? "on" : ""} onClick={() => setMode("manual")}>
              Manual
            </button>
          </div>
        )}
        {mode === "pair" ? (
          <div className="field">
            <label>Pairing link</label>
            <input className="input mono" value={form.link} onChange={(e) => set("link", e.target.value)} placeholder="modelmatrix-gateway://pair?..." />
            <span className="hint">Run the ModelMatrix agent on the other machine; it prints a pairing link.</span>
          </div>
        ) : (
          <div className="form-grid">
            <div className="field">
              <label>Name</label>
              <input className="input" value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="GPU box" />
            </div>
            <div className="field">
              <label>Connection</label>
              <select className="select" style={{ width: "100%" }} value={form.kind} onChange={(e) => set("kind", e.target.value)}>
                <option value="direct">Direct (agent URL)</option>
                <option value="ssh">SSH tunnel</option>
              </select>
            </div>
            {form.kind === "ssh" ? (
              <div className="field full">
                <label>SSH target</label>
                <input className="input mono" value={form.ssh_target} onChange={(e) => set("ssh_target", e.target.value)} placeholder="user@gpu-box.local" />
                <span className="hint">Uses your SSH keys. The app opens the tunnel and can install the agent for you.</span>
              </div>
            ) : (
              <div className="field full">
                <label>Agent URL</label>
                <input className="input mono" value={form.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder="http://192.168.1.20:8877" />
              </div>
            )}
            <div className="field full">
              <label>Agent token</label>
              <input className="input mono" type="password" value={form.token} onChange={(e) => set("token", e.target.value)} placeholder={gateway?.has_token ? "unchanged" : "optional"} />
              <span className="hint">Stored in the {platform.credential_store} by the desktop bridge.</span>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

function RemoteHost({ gateway }: { gateway: Gateway }) {
  const { bridgeCommand, toast, globalAction } = useApp();
  const { platform } = usePlatform();
  const status = usePoll<StatusPayload>((signal) => api(remote(gateway.id, "/api/status"), { signal }), 5000, [gateway.id]);
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (type: string, label: string) => {
    setBusy(type);
    try {
      await bridgeCommand(type, { id: gateway.id });
      toast(`${label}: ${gateway.name}`, "ok");
      status.refresh();
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setBusy(null);
    }
  };
  const statuses = status.data?.statuses ?? [];
  return (
    <div className="card card-pad" style={{ marginBottom: 16 }}>
      <div className="card-head">
        <div className="card-title" style={{ textTransform: "none", letterSpacing: 0 }}>
          <span className={`dot ${!gateway.enabled ? "warn" : status.error && !status.data ? "off" : "on"}`} />
          <span style={{ fontFamily: "Inter", fontSize: 16 }}>{gateway.name}</span>
          <span className="badge">{gateway.kind === "ssh" ? "SSH" : "DIRECT"}</span>
          {gateway.agent_version && <span className="badge">agent {String(gateway.agent_version)}</span>}
        </div>
        <div className="btn-row">
          <button className="btn sm" onClick={() => run("gateway.refresh", "Refreshed")} disabled={!!busy}>
            <RefreshCw size={13} className={busy === "gateway.refresh" ? "spin" : ""} /> Refresh
          </button>
          <button className="btn sm" onClick={() => run("gateway.toggle", gateway.enabled ? "Paused" : "Resumed")} disabled={!!busy}>
            {gateway.enabled ? <Pause size={13} /> : <Play size={13} />} {gateway.enabled ? "Pause" : "Resume"}
          </button>
          {gateway.kind === "ssh" && (
            <button className="btn sm" onClick={() => run("gateway.install_agent", "Agent installed")} disabled={!!busy}>
              <UploadCloud size={13} className={busy === "gateway.install_agent" ? "spin" : ""} /> Install agent
            </button>
          )}
          <button className="btn sm" onClick={() => run("gateway.update_agent", "Agent updated")} disabled={!!busy}>
            <UploadCloud size={13} className={busy === "gateway.update_agent" ? "spin" : ""} /> Update agent
          </button>
          <button className="btn sm" onClick={() => globalAction("stop-all", gateway.id)} disabled={!statuses.some((s) => s.running)}>
            <Power size={13} /> Stop all
          </button>
          <button className="btn ghost sm icon" onClick={() => setEditing(true)} aria-label="Edit host">
            <Pencil size={14} />
          </button>
          <button className="btn ghost sm icon" onClick={() => setRemoving(true)} aria-label="Remove host">
            <Trash2 size={14} />
          </button>
        </div>
      </div>
      {status.error && !status.data ? (
        <Notice kind="bad">Could not reach {gateway.name}: {status.error.message}</Notice>
      ) : statuses.length ? (
        <div className="profiles">
          {statuses.map((s) => (
            <ProfileCard key={s.profile} status={s} gateway={gateway.id} />
          ))}
        </div>
      ) : (
        <Empty title={status.loading ? "Loading…" : "No models on this host"} />
      )}
      {status.data?.benchmark && (status.data.benchmark.running || status.data.benchmark.latest) && (
        <div style={{ marginTop: 16 }}>
          <BenchmarkPanel benchmark={status.data.benchmark} selected={[]} gateway={gateway.id} />
        </div>
      )}
      {editing && <GatewayForm gateway={gateway} onClose={() => setEditing(false)} />}
      {removing && (
        <Confirm
          title={`Remove ${gateway.name}?`}
          message={`The host is removed from ModelMatrix and its token deleted from the ${platform.credential_store}. Models on that machine keep running.`}
          danger
          confirmLabel="Remove host"
          onClose={() => setRemoving(false)}
          onConfirm={async () => {
            try {
              await bridgeCommand("gateway.remove", { id: gateway.id });
              toast(`Removed ${gateway.name}`, "ok");
            } catch (e) {
              toast((e as Error).message, "err");
            }
          }}
        />
      )}
    </div>
  );
}

/* ---------------- Page ---------------- */

export function ServersPage({ editorRequest, clearEditorRequest }: { editorRequest: boolean; clearEditorRequest: () => void }) {
  const { status, statusError, globalAction, pending, lastActive, bridge, toast, refreshStatus } = useApp();
  const { capabilities, bridgeLabel, localHostLabel } = usePlatform();
  const capture = usePoll<{ capture?: { profile: string; enabled: boolean; supported: boolean; port?: number }[] }>((signal) => api("/api/usage/settings", { signal }), 6000);
  const [selected, setSelected] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [runtime, setRuntime] = useState("all");
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [duplicating, setDuplicating] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [addingHost, setAddingHost] = useState(false);
  const statuses = status?.statuses ?? [];
  const runtimes = useMemo(() => [...new Set(statuses.map((s) => s.runtime_label || s.runtime))], [statuses]);
  const visible = statuses.filter(
    (s) =>
      (runtime === "all" || (s.runtime_label || s.runtime) === runtime) &&
      (!search || `${s.profile} ${s.display_name} ${s.request_model}`.toLowerCase().includes(search.toLowerCase())),
  );
  const captureMap = new Map((capture.data?.capture ?? []).map((c) => [c.profile, c]));
  const showCreate = creating || editorRequest;
  const { models: hfModels } = useHFData();
  const hfModel = deleting ? hfModels.find((m) => m.profile === deleting && !m.host) : undefined;

  return (
    <>
      <div className="page-title">
        <div>
          <h1>Models & Servers</h1>
          <p>Start, stop, switch and benchmark every model profile on {localHostLabel} and supported remote hosts.</p>
        </div>
        <div className="btn-row">
          <button className="btn" onClick={() => refreshStatus()}>
            <RefreshCw size={14} /> Refresh
          </button>
          <button className="btn" onClick={() => globalAction("reopen-last")} disabled={!lastActive}>
            <RotateCcw size={14} /> Reopen last active{lastActive ? ` (${lastActive})` : ""}
          </button>
          <button className="btn danger" onClick={() => globalAction("stop-all")} disabled={!!pending.__all || !statuses.some((s) => s.running)}>
            {pending.__all ? <Loader2 size={14} className="spin" /> : <Power size={14} />} Stop all
          </button>
          <button className="btn" onClick={() => openHuggingFace()}>
            <PackagePlus size={14} /> Add from Hugging Face
          </button>
          <button className="btn primary" onClick={() => setCreating(true)}>
            <Plus size={14} /> New profile
          </button>
        </div>
      </div>

      <div style={{ marginBottom: 28 }}>
        <ConnectAppsSection />
      </div>

      <section id="profiles" className="section">
        <div className="toolbar">
          <div className="row wrap">
            <div className="row card" style={{ padding: "6px 10px", borderRadius: 9 }}>
              <Search size={14} className="muted" />
              <input className="input" style={{ border: 0, background: "transparent", padding: "2px 4px", width: 220 }} placeholder="Search profiles" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
            <div className="seg sm">
              <button className={runtime === "all" ? "on" : ""} onClick={() => setRuntime("all")}>
                All
              </button>
              {runtimes.map((r) => (
                <button key={r} className={runtime === r ? "on" : ""} onClick={() => setRuntime(r)}>
                  {r}
                </button>
              ))}
            </div>
          </div>
          <div className="row feed-small">
            {statuses.filter((s) => s.running).length} running · {statuses.length} profiles
            {selected.length > 0 && (
              <button className="btn ghost sm" onClick={() => setSelected([])}>
                Clear selection ({selected.length})
              </button>
            )}
          </div>
        </div>
        {statusError && !status ? (
          <Notice kind="bad">Controller not reachable: {statusError.message}</Notice>
        ) : visible.length ? (
          <div className="profiles">
            {visible.map((s) => (
              <ProfileCard
                key={s.profile}
                status={s}
                selected={selected.includes(s.profile)}
                onSelect={(on) => setSelected((list) => (on ? [...list, s.profile] : list.filter((p) => p !== s.profile)))}
                onEdit={s.source === "profile" || !s.source ? () => setEditing(s.profile) : undefined}
                onDuplicate={() => {
                  setDuplicating(s.profile);
                  setNewName(`${s.profile}-copy`);
                }}
                onDelete={() => setDeleting(s.profile)}
                capture={captureMap.get(s.profile)}
              />
            ))}
          </div>
        ) : (
          <Empty title={statuses.length ? "No profiles match" : "No model profiles yet"}>
            {statuses.length ? "Try a different search or runtime filter." : (
              <>
                Create one with <b style={{ display: "inline" }}>New profile</b>: Ollama, llama.cpp, MLX, vLLM, LM Studio or any OpenAI-compatible server. Or use{" "}
                <b style={{ display: "inline" }}>Add from Hugging Face</b> to check a model, download the version that fits and get a ready profile.
              </>
            )}
          </Empty>
        )}
      </section>

      <div style={{ marginTop: 28 }}>
        <DownloadsSection />
      </div>
      <div style={{ marginTop: 28 }}>
        <DownloadedModelsSection />
      </div>

      <section id="remote-hosts" className="section" style={{ marginTop: 28 }}>
        <div className="toolbar">
          <div className="card-title">Remote hosts</div>
          <button className="btn" onClick={() => setAddingHost(true)} disabled={!bridge?.app_connected || !capabilities.remote_gateways}>
            <Link2 size={14} /> Add remote host
          </button>
        </div>
        {!capabilities.remote_gateways ? (
          <Notice kind="info">Remote gateways are unavailable on this controller. Local profiles and usage remain available.</Notice>
        ) : !bridge?.app_connected ? (
          <Notice kind="warn">Remote hosts are managed through the ModelMatrix {bridgeLabel.toLowerCase()} (it keeps SSH tunnels and tokens in the protected credential store). Connect it to see and control them here.</Notice>
        ) : bridge.gateways.length ? (
          bridge.gateways.map((g) => <RemoteHost key={g.id} gateway={g} />)
        ) : (
          <Empty title="No remote hosts">Add a supported remote agent to control its models from here.</Empty>
        )}
      </section>

      <div style={{ marginTop: 28 }}>
        <BenchmarkPanel benchmark={status?.benchmark} selected={selected} />
      </div>

      {showCreate && (
        <ProfileEditor
          onClose={() => {
            setCreating(false);
            clearEditorRequest();
          }}
          onSaved={() => undefined}
        />
      )}
      {editing && <ProfileEditor profile={editing} onClose={() => setEditing(null)} onSaved={() => undefined} />}
      {addingHost && <GatewayForm onClose={() => setAddingHost(false)} />}
      {duplicating && (
        <Modal
          small
          title={`Duplicate ${duplicating}`}
          onClose={() => setDuplicating(null)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setDuplicating(null)}>
                Cancel
              </button>
              <button
                className="btn primary"
                onClick={async () => {
                  try {
                    await post("/api/profiles/duplicate", { profile: duplicating, new_profile: newName.trim() });
                    toast(`Created ${newName.trim()}`, "ok");
                    setDuplicating(null);
                    refreshStatus();
                  } catch (e) {
                    toast((e as Error).message, "err");
                  }
                }}
              >
                Duplicate
              </button>
            </>
          }
        >
          <div className="field">
            <label>New profile ID</label>
            <input className="input mono" value={newName} onChange={(e) => setNewName(e.target.value)} autoFocus />
          </div>
        </Modal>
      )}
      {deleting && hfModel && (
        <DeleteModelDialog model={hfModel} onClose={() => setDeleting(null)} onDeleted={() => setSelected((s) => s.filter((p) => p !== deleting))} />
      )}
      {deleting && !hfModel && (
        <Confirm
          title={`Delete ${deleting}?`}
          message="The profile file is removed from your profiles folder. Model weights are not touched."
          danger
          confirmLabel="Delete profile"
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            try {
              await post("/api/profiles/delete", { profile: deleting });
              toast(`Deleted ${deleting}`, "ok");
              setSelected((s) => s.filter((p) => p !== deleting));
              refreshStatus();
            } catch (e) {
              toast((e as Error).message, "err");
            }
          }}
        />
      )}
    </>
  );
}
