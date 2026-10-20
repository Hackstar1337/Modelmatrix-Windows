import { useEffect, useState } from "react";
import { AppWindow, Download, ExternalLink, FolderOpen, Loader2, RefreshCw, ScrollText, Stethoscope, Trash2 } from "lucide-react";
import { api, post } from "../lib/api";
import { ago, compact } from "../lib/format";
import { useApp, usePlatform, usePoll } from "../lib/state";
import type { CaptureState, RelayStatus } from "../lib/types";
import { Confirm, CopyButton, Empty, MatrixMark, Notice, SectionCard, Switch } from "../components/ui";
import { EnginesSection, HuggingFaceSettingsSection } from "../components/HFSettings";

interface DoctorReport {
  findings: { id: string; severity: string; subsystem: string; message: string; evidence?: string; remediation?: string }[];
  next_steps: string[];
  generated_at?: string;
  tool_version?: string;
  launch_agent?: { installed: boolean; running: boolean; plist_path?: string };
  controller?: { reachable: boolean; url: string; profiles: number; integrations: number };
}

export function SettingsPage() {
  const { settings, update, info, status, bridge, bridgeCommand, toast, refreshStatus, refreshOverview } = useApp();
  const { capabilities, bridgeLabel, folderLabel, localHostLabel, platform, desktopActionsAvailable } = usePlatform();
  const usage = usePoll<RelayStatus & { capture?: CaptureState[] }>((signal) => api("/api/usage/settings", { signal }), 6000);
  const [retention, setRetention] = useState<string>("");
  const [profilesDir, setProfilesDir] = useState("");
  const [doctor, setDoctor] = useState<DoctorReport | null>(null);
  const [doctorBusy, setDoctorBusy] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [running, setRunning] = useState<string | null>(null);

  useEffect(() => {
    if (usage.data && retention === "") setRetention(String(usage.data.retention_days));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [usage.data]);
  useEffect(() => {
    if (status?.profiles_dir && !profilesDir) setProfilesDir(status.profiles_dir);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.profiles_dir]);

  const appAction = async (type: string, label: string) => {
    try {
      await bridgeCommand(type, {});
      toast(label, "ok");
    } catch (e) {
      toast((e as Error).message, "err");
    }
  };

  const runDoctor = async () => {
    setDoctorBusy(true);
    try {
      setDoctor(await api<DoctorReport>("/api/doctor"));
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setDoctorBusy(false);
    }
  };

  const relay = usage.data;
  const capture = relay?.capture ?? [];

  return (
    <>
      <div className="page-title">
        <div>
          <h1>Settings</h1>
            <p>Dashboard preferences, usage metering, controller and {bridgeLabel.toLowerCase()}.</p>
        </div>
      </div>

      <SectionCard id="appearance" title="Appearance">
        <div className="setting-row">
          <div>
            <div className="title">Theme</div>
            <div className="desc">Choose the dashboard theme for this browser.</div>
          </div>
          <div className="seg sm">
            {(["dark", "light"] as const).map((theme) => (
              <button key={theme} className={settings.theme === theme ? "on" : ""} onClick={() => update({ theme })}>
                {theme}
              </button>
            ))}
          </div>
        </div>
        <div className="setting-row">
          <div>
            <div className="title">View mode</div>
            <div className="desc">Advanced adds latency percentiles, CPU, raw identifiers, the 1h range and the raw profile editor.</div>
          </div>
          <div className="seg sm">
            {(["standard", "advanced"] as const).map((mode) => (
              <button key={mode} className={settings.mode === mode ? "on" : ""} onClick={() => update({ mode })}>
                {mode}
              </button>
            ))}
          </div>
        </div>
        <div className="setting-row">
          <div>
            <div className="title">Refresh rate</div>
            <div className="desc">How often live numbers update while this tab is visible.</div>
          </div>
          <div className="seg sm">
            {[1500, 2500, 5000, 10000].map((ms) => (
              <button key={ms} className={settings.refreshMs === ms ? "on" : ""} onClick={() => update({ refreshMs: ms })}>
                {ms / 1000}s
              </button>
            ))}
          </div>
        </div>
      </SectionCard>

      <SectionCard id="relay" title="Usage & relay" meta={relay?.listening ? "recording" : "offline"}>
        {relay?.error && <Notice kind="bad">Relay error: {relay.error}</Notice>}
        <div className="stack" style={{ marginTop: relay?.error ? 12 : 0 }}>
          <div className="grid-2">
            <div className="field">
              <span className="label">OpenAI-compatible URL</span>
              <div className="code">
                <span>{relay?.url ?? "-"}</span>
                {relay?.url && <CopyButton text={relay.url} />}
              </div>
            </div>
            <div className="field">
              <span className="label">Ollama URL</span>
              <div className="code">
                <span>{relay?.ollama_url ?? "-"}</span>
                {relay?.ollama_url && <CopyButton text={relay.ollama_url} />}
              </div>
            </div>
          </div>
          <div className="feed-small">
            {compact(relay?.requests_recorded ?? 0)} requests recorded · database {relay?.database}
          </div>
          <div className="setting-row">
            <div>
              <div className="title">Keep history for</div>
              <div className="desc">Older requests are deleted automatically. Only metadata is stored, never prompts or responses.</div>
            </div>
            <div className="row">
              <input className="input mono" style={{ width: 90 }} value={retention} onChange={(e) => setRetention(e.target.value.replace(/\D/g, ""))} />
              <span className="muted">days</span>
              <button
                className="btn sm primary"
                onClick={async () => {
                  try {
                    await post("/api/usage/settings", { retention_days: Number(retention) });
                    toast(`Keeping ${retention} days of history`, "ok");
                    usage.refresh();
                  } catch (e) {
                    toast((e as Error).message, "err");
                  }
                }}
              >
                Save
              </button>
            </div>
          </div>
          <div className="setting-row">
            <div>
              <div className="title">Export or clear</div>
              <div className="desc">Download every recorded request as CSV, or delete all usage history.</div>
            </div>
            <div className="btn-row">
              <a className="btn sm" href="/api/usage/export.csv?range=all">
                <Download size={13} /> Export all
              </a>
              <button className="btn sm danger" onClick={() => setResetting(true)}>
                <Trash2 size={13} /> Clear history
              </button>
            </div>
          </div>
        </div>
      </SectionCard>

      <HuggingFaceSettingsSection />

      <EnginesSection />

      <SectionCard id="capture" title="Capture mode" meta={`${capture.filter((c) => c.enabled).length} on`}>
        <Notice>
          Normally only apps that use the relay URL are recorded. Capture mode moves a model server to a private port and puts the relay on its usual port, so <b>every</b> app is recorded without reconfiguring anything. Restart the model after changing this.
        </Notice>
        <div style={{ marginTop: 10 }}>
          {capture.length ? (
            capture.map((c) => (
              <div className="setting-row" key={c.profile}>
                <div>
                  <div className="title">{c.display_name || c.profile}</div>
                  <div className="desc">
                    {c.supported ? (c.enabled ? `Relay holds port ${c.port ?? "?"}${c.listening === false ? " (waiting for restart)" : ""}` : `Port ${c.port ?? "?"}`) : c.reason || "Not available for this runtime (remote or external endpoint)"}
                  </div>
                </div>
                <Switch
                  on={c.enabled}
                  disabled={!c.supported}
                  label={`Capture ${c.profile}`}
                  onChange={async (on) => {
                    try {
                      await post("/api/profiles/capture", { profile: c.profile, enabled: on });
                      toast(on ? `Capture on for ${c.profile}` : `Capture off for ${c.profile}`, "ok");
                      usage.refresh();
                      refreshStatus();
                    } catch (e) {
                      toast((e as Error).message, "err");
                    }
                  }}
                />
              </div>
            ))
          ) : (
            <Empty title="No local profiles" />
          )}
        </div>
      </SectionCard>

      <SectionCard id="controller" title="Controller" meta={info?.version ? `v${info.version}` : undefined}>
        <dl className="kv">
          <dt>Dashboard</dt>
          <dd>{info?.controller_url ?? location.origin}</dd>
          <dt>Controller root</dt>
          <dd>{status?.controller_root ?? info?.controller_root}</dd>
          <dt>Access</dt>
          <dd>{info?.token_required ? "token required" : `${localHostLabel} only (loopback)`}</dd>
        </dl>
        <div className="sep" />
        <div className="field">
          <label>Profiles folder</label>
          <div className="row">
            <input className="input mono grow" value={profilesDir} onChange={(e) => setProfilesDir(e.target.value)} />
            <button
              className="btn primary"
              onClick={async () => {
                try {
                  await post("/api/config/profiles-dir", { profiles_dir: profilesDir.trim() });
                  toast("Profiles folder updated", "ok");
                  refreshStatus();
                  refreshOverview();
                } catch (e) {
                  toast((e as Error).message, "err");
                }
              }}
            >
              Save
            </button>
          </div>
          <span className="hint">Every .env or .json file in this folder is a model profile.</span>
        </div>
        <div className="btn-row" style={{ marginTop: 14 }}>
          <button className="btn" disabled={!desktopActionsAvailable || !bridge?.app_connected || !capabilities.open_folders} onClick={() => appAction("app.open_profiles_folder", `Opened profiles folder in ${folderLabel.replace(/^Open in /, "")}`)}>
            <FolderOpen size={14} /> {folderLabel}
          </button>
          <button className="btn" disabled={!desktopActionsAvailable || !bridge?.app_connected} onClick={() => appAction("app.open_logs", "Opened logs folder")}>
            <ScrollText size={14} /> Open logs
          </button>
        </div>
      </SectionCard>

      <SectionCard
        id="doctor"
        title="Doctor"
        meta={doctor?.generated_at ? new Date(doctor.generated_at).toLocaleTimeString() : undefined}
        actions={
          <button className="btn sm primary" onClick={runDoctor} disabled={doctorBusy}>
            {doctorBusy ? <Loader2 size={13} className="spin" /> : <Stethoscope size={13} />} Run checks
          </button>
        }
      >
        {doctor ? (
          <div className="stack">
            <div className="row wrap feed-small" style={{ gap: 16 }}>
              <span>Controller: {doctor.controller?.reachable ? "reachable" : "unreachable"}</span>
              <span>Startup service: {doctor.launch_agent?.installed ? (doctor.launch_agent.running ? "installed, running" : "installed, stopped") : "not installed"}</span>
              <span>Profiles: {doctor.controller?.profiles ?? "-"}</span>
            </div>
            {doctor.findings.length ? (
              doctor.findings.map((f) => (
                <Notice key={f.id} kind={f.severity === "error" || f.severity === "critical" ? "bad" : f.severity === "warning" ? "warn" : "info"}>
                  <b>{f.subsystem}</b>: {f.message}
                  {f.remediation && <div className="muted" style={{ marginTop: 4 }}>Fix: {f.remediation}</div>}
                  {f.evidence && <div className="feed-small" style={{ marginTop: 2 }}>{f.evidence}</div>}
                </Notice>
              ))
            ) : (
              <Notice>No problems found.</Notice>
            )}
            {doctor.next_steps.length > 0 && (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {doctor.next_steps.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <Empty title="Check your setup">Runs the same checks as the menu bar app's Doctor and `modelmatrixctl doctor`.</Empty>
        )}
      </SectionCard>

      <SectionCard id="integrations" title="Integrations" meta={`${status?.integrations?.length ?? 0} available`}>
        {status?.integrations?.length ? (
          status.integrations.map((integration: any) => (
            <div className="setting-row" key={integration.id}>
              <div>
                <div className="title">{integration.display_name || integration.id}</div>
                <div className="desc">{integration.description || integration.kind}</div>
              </div>
              <div className="btn-row">
                {(integration.capabilities ?? ["sync"]).map((action: string) => (
                  <button
                    key={action}
                    className="btn sm"
                    disabled={running === `${integration.id}:${action}`}
                    onClick={async () => {
                      setRunning(`${integration.id}:${action}`);
                      try {
                        await post("/api/integrations/run", { integration: integration.id, action });
                        toast(`${integration.display_name || integration.id}: ${action} done`, "ok");
                      } catch (e) {
                        toast((e as Error).message, "err");
                      } finally {
                        setRunning(null);
                      }
                    }}
                  >
                    {running === `${integration.id}:${action}` ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />} {integration.sync_label || action}
                  </button>
                ))}
              </div>
            </div>
          ))
        ) : (
          <Empty title="No integrations detected">Integrations appear when a supported tool (for example Droid) is installed.</Empty>
        )}
      </SectionCard>

      <SectionCard
        id="menubar"
        title={
          <>
            {bridgeLabel} <span className={`dot ${bridge?.app_connected ? "on" : "warn"}`} />
          </>
        }
        meta={bridge?.app_connected ? `connected · ${ago(bridge.last_heartbeat)}` : "not running"}
      >
        {bridge?.app_connected ? (
          <div className="stack">
            <dl className="kv">
              <dt>Version</dt>
              <dd>{String(bridge.app.version ?? "-")}</dd>
              <dt>Remote hosts</dt>
              <dd>{bridge.gateways.length}</dd>
              <dt>Last active</dt>
              <dd>{String(bridge.app.last_active_profile ?? "-")}</dd>
            </dl>
            <div className="btn-row">
              <button className="btn" disabled={!desktopActionsAvailable} onClick={() => appAction("app.open_settings", `Opened ${bridgeLabel.toLowerCase()} settings`)}>
                <AppWindow size={14} /> Open app settings
              </button>
              <button className="btn" disabled={!desktopActionsAvailable} onClick={() => appAction("app.open_usage", `Opened Usage in the ${bridgeLabel.toLowerCase()}`)}>
                <AppWindow size={14} /> Usage in menu bar
              </button>
              <button className="btn" disabled={!desktopActionsAvailable} onClick={() => appAction("app.open_profiles", `Opened Profiles in the ${bridgeLabel.toLowerCase()}`)}>
                <AppWindow size={14} /> Profiles in menu bar
              </button>
              <button className="btn" disabled={!desktopActionsAvailable} onClick={() => appAction("app.open_optimize", `Opened Optimize in the ${bridgeLabel.toLowerCase()}`)}>
                <AppWindow size={14} /> Optimize in menu bar
              </button>
              <button className="btn" disabled={!desktopActionsAvailable} onClick={() => appAction("app.open_downloads", `Opened Downloads in the ${bridgeLabel.toLowerCase()}`)}>
                <AppWindow size={14} /> Downloads in menu bar
              </button>
              <button className="btn" disabled={!desktopActionsAvailable} onClick={() => appAction("app.refresh", `${bridgeLabel} refreshed`)}>
                <RefreshCw size={14} /> Refresh app
              </button>
            </div>
          </div>
        ) : (
          <Notice kind="warn">The {bridgeLabel.toLowerCase()} is not connected. Core controller features remain available, but desktop actions and remote gateways may be unavailable on this host.</Notice>
        )}
      </SectionCard>

      <SectionCard id="about" title="About">
        <div className="row" style={{ gap: 16 }}>
          <MatrixMark size={48} />
          <div>
            <div style={{ fontWeight: 700, fontSize: 18 }}>ModelMatrix {info?.version}</div>
            <div className="muted">Local LLM control center for {platform.display_name}: run, switch, benchmark and meter every model from this dashboard or the {bridgeLabel.toLowerCase()}.</div>
            <a href="https://github.com/Hackstar1337/ModelMatrix" target="_blank" rel="noreferrer" className="row" style={{ gap: 4, marginTop: 6, display: "inline-flex" }}>
              github.com/Hackstar1337/ModelMatrix <ExternalLink size={12} />
            </a>
          </div>
        </div>
      </SectionCard>

      {resetting && (
        <Confirm
          title="Clear all usage history?"
          message="Every recorded request, client name and statistic is deleted. This cannot be undone."
          danger
          confirmLabel="Delete history"
          onClose={() => setResetting(false)}
          onConfirm={async () => {
            try {
              await post("/api/usage/reset", { confirm: true });
              toast("Usage history cleared", "ok");
              usage.refresh();
              refreshOverview();
            } catch (e) {
              toast((e as Error).message, "err");
            }
          }}
        />
      )}
    </>
  );
}
