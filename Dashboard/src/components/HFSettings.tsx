import { useEffect, useState } from "react";
import { Download, KeyRound, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { hf, size, type HFEngineId, type HFEngineStatus, type HFSettings } from "../lib/hf";
import { useApp, usePlatform, usePoll } from "../lib/state";
import { EngineInstallDialog, useRemoteHosts } from "./HuggingFace";
import { CopyButton, Notice, SectionCard, Switch } from "./ui";

/** Settings > Hugging Face: models folder, mirror, speed, keep awake, auto start and the login token. */
export function HuggingFaceSettingsSection() {
  const { toast } = useApp();
  const { localHostLabel, platform, capabilities } = usePlatform();
  const [settings, setSettings] = useState<HFSettings>();
  const [error, setError] = useState<string>();
  const [folder, setFolder] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [speed, setSpeed] = useState("0");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<string>();

  const apply = (s: HFSettings) => {
    setSettings(s);
    setFolder(s.models_folder);
    setEndpoint(s.endpoint ?? "");
    setSpeed(String(s.speed_limit_mbps));
  };
  const load = () =>
    hf
      .settings()
      .then((r) => apply(r.settings))
      .catch((e) => setError((e as Error).message));
  useEffect(() => {
    load();
  }, []);

  const save = async (patch: Parameters<typeof hf.saveSettings>[0], done: string) => {
    setBusy(Object.keys(patch)[0]);
    try {
      const result = await hf.saveSettings(patch);
      apply(result.settings);
      toast(done, "ok");
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setBusy(undefined);
    }
  };

  if (!settings) {
    return (
      <SectionCard id="huggingface" title="Hugging Face">
        {error ? <Notice kind="bad">{error}</Notice> : <Loader2 size={16} className="spin" />}
      </SectionCard>
    );
  }
  return (
    <SectionCard id="huggingface" title="Hugging Face" meta={settings.token_saved ? "token saved" : "no token"}>
      <div className="setting-row">
        <div>
          <div className="title">Models folder</div>
          <div className="desc">
            Where downloaded models go on {localHostLabel}. {settings.free_disk_bytes ? `${size(settings.free_disk_bytes)} free.` : ""} Remote hosts use their own model folder.
          </div>
        </div>
        <div className="row">
          <input className="input mono" aria-label="Models folder" style={{ width: 260 }} value={folder} onChange={(e) => setFolder(e.target.value)} />
          <button className="btn sm primary" disabled={busy === "models_folder" || folder === settings.models_folder} onClick={() => save({ models_folder: folder }, "Models folder saved")}>
            Save
          </button>
        </div>
      </div>
      <div className="setting-row">
        <div>
          <div className="title">Mirror</div>
          <div className="desc">Leave empty for huggingface.co. Set a mirror (https) where the site is blocked or slow. In use: {settings.endpoint_in_use}</div>
        </div>
        <div className="row">
          <input className="input mono" aria-label="Mirror address" style={{ width: 260 }} placeholder="https://huggingface.co" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} />
          <button
            className="btn sm primary"
            disabled={busy === "endpoint" || endpoint === (settings.endpoint ?? "")}
            onClick={() => save({ endpoint: endpoint.trim() || null }, endpoint.trim() ? "Mirror saved" : "Using huggingface.co")}
          >
            Save
          </button>
        </div>
      </div>
      <div className="setting-row">
        <div>
          <div className="title">Speed limit</div>
          <div className="desc">Megabits per second for all downloads together. 0 means no limit.</div>
        </div>
        <div className="row">
          <input className="input mono" aria-label="Speed limit in megabits per second" style={{ width: 90 }} value={speed} onChange={(e) => setSpeed(e.target.value.replace(/\D/g, ""))} />
          <span className="muted">Mbit/s</span>
          <button
            className="btn sm primary"
            disabled={busy === "speed_limit_mbps" || Number(speed || 0) === settings.speed_limit_mbps}
            onClick={() => save({ speed_limit_mbps: Number(speed || 0) }, Number(speed || 0) ? `Limited to ${speed} Mbit/s` : "No speed limit")}
          >
            Save
          </button>
        </div>
      </div>
      <div className="setting-row">
        <div>
          <div className="title">Connections per file</div>
          <div className="desc">Large files download in several pieces at once. Lower it on slow or shared connections.</div>
        </div>
        <div className="seg sm">
          {[1, 2, 4, 8].map((n) => (
            <button key={n} className={settings.connections_per_file === n ? "on" : ""} onClick={() => save({ connections_per_file: n }, `${n} connection${n > 1 ? "s" : ""} per file`)}>
              {n}
            </button>
          ))}
        </div>
      </div>
      <div className="setting-row">
        <div>
          <div className="title">Keep {localHostLabel} awake while downloading</div>
          <div className="desc">Stops idle sleep only while a download runs. The display can still turn off.</div>
        </div>
        <Switch
          on={settings.keep_awake}
          disabled={!capabilities.keep_awake}
          onChange={(v) => save({ keep_awake: v }, v ? `Keeps ${localHostLabel} awake while downloading` : `${localHostLabel} may sleep during downloads`)}
          label="Keep awake"
        />
        {!capabilities.keep_awake && <div className="hint">Keep awake is not available on {platform.display_name}; the controller will not request it.</div>}
      </div>
      <div className="setting-row">
        <div>
          <div className="title">Start and test after download</div>
          <div className="desc">Creates the profile, starts the model and sends one short test message once it is downloaded.</div>
        </div>
        <Switch
          on={settings.start_after_download}
          onChange={(v) => save({ start_after_download: v }, v ? "New models start after download" : "New models are only set up")}
          label="Start after download"
        />
      </div>
      <div className="setting-row">
        <div>
          <div className="title">Hugging Face token</div>
          <div className="desc">
            Needed only for models that require a login. Stored in the {platform.credential_store} and sent only to Hugging Face.{" "}
            {settings.token_saved ? "A token is saved." : "No token saved."}
          </div>
        </div>
        <div className="row">
          <input className="input mono" aria-label="Hugging Face token" type="password" style={{ width: 220 }} placeholder="hf_…" value={token} onChange={(e) => setToken(e.target.value)} />
          <button
            className="btn sm primary"
            disabled={!token.trim() || busy === "token"}
            onClick={async () => {
              setBusy("token");
              try {
                const result = await hf.saveToken(token.trim());
                setToken("");
                toast(result.user ? `Token saved for ${result.user}` : "Token saved", "ok");
                load();
              } catch (e) {
                toast((e as Error).message, "err");
              } finally {
                setBusy(undefined);
              }
            }}
          >
            <KeyRound size={13} /> Save
          </button>
          {settings.token_saved && (
            <button
              className="btn sm danger"
              onClick={async () => {
                try {
                  await hf.deleteToken();
                  toast("Token removed", "ok");
                  load();
                } catch (e) {
                  toast((e as Error).message, "err");
                }
              }}
            >
              <Trash2 size={13} /> Remove
            </button>
          )}
        </div>
      </div>
    </SectionCard>
  );
}

/** Settings > Engines: what is installed on this host or a remote host, and approve-first install, update and removal. */
export function EnginesSection() {
  const hosts = useRemoteHosts();
  const { localHostLabel } = usePlatform();
  const [host, setHost] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [dialog, setDialog] = useState<{ engine: HFEngineId; name: string; action: "install" | "update" | "uninstall" } | null>(null);
  const list = usePoll((signal) => hf.engines(host || undefined, false, signal), 15000, [host]);
  const engines = list.data?.engines ?? [];
  const recheck = async () => {
    setRefreshing(true);
    try {
      list.setData(await hf.engines(host || undefined, true));
    } catch {
      list.refresh();
    } finally {
      setRefreshing(false);
    }
  };
  return (
    <SectionCard
      id="engines"
      title="Engines"
      meta={list.data ? `${engines.filter((e) => e.installed).length} of ${engines.filter((e) => e.applicable).length} installed` : undefined}
      actions={
        <button className="btn sm" onClick={recheck} disabled={refreshing}>
          {refreshing ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />} Check again
        </button>
      }
    >
      <div className="stack">
        {hosts.length > 0 && (
          <div className="seg sm" style={{ alignSelf: "flex-start" }}>
            <button className={host === "" ? "on" : ""} onClick={() => setHost("")}>
              {localHostLabel}
            </button>
            {hosts.map((g) => (
              <button key={g.id} className={host === g.name ? "on" : ""} onClick={() => setHost(g.name)}>
                {g.name}
              </button>
            ))}
          </div>
        )}
        <div className="desc muted">
          ModelMatrix never installs an engine on its own. It shows exactly what will be downloaded or run, and does it only after you press Install. Engines it installs go to{" "}
          <span className="mono">{list.data?.engines_folder ?? "~/ModelMatrix/Engines"}</span>.
        </div>
        {list.error && !list.data && <Notice kind="bad">{list.error.message}</Notice>}
        {engines
          .filter((e) => e.applicable || e.installed)
          .map((engine) => (
            <EngineRow key={engine.id} engine={engine} onAction={(action) => setDialog({ engine: engine.id, name: engine.name, action })} />
          ))}
        {host === "" && list.data && <EngineLocations engines={engines.filter((e) => e.applicable || e.installed)} onSaved={recheck} />}
        {!!list.data?.jobs.length && (
          <div className="muted hf-small">
            Recent: {list.data.jobs.slice(0, 3).map((j) => `${j.title} (${j.state})`).join(" · ")}
          </div>
        )}
      </div>
      {dialog && (
        <EngineInstallDialog
          engine={dialog.engine}
          engineName={dialog.name}
          action={dialog.action}
          host={host || undefined}
          onClose={() => setDialog(null)}
          onDone={recheck}
        />
      )}
    </SectionCard>
  );
}

function EngineRow({ engine, onAction }: { engine: HFEngineStatus; onAction: (action: "install" | "update" | "uninstall") => void }) {
  const actions = engine.actions ?? [];
  const manual = !engine.installed && (!engine.install || engine.install.method === "manual" || !actions.includes("install"));
  return (
    <div className="setting-row">
      <div style={{ minWidth: 0 }}>
        <div className="title">
          {engine.name} {engine.installed && <span className="badge green">{engine.version ? `v${engine.version}`.replace(/^vb/, "b") : "installed"}</span>}
          {!engine.installed && <span className="badge amber">not installed</span>}
          {engine.running && <span className="badge blue">running</span>}
        </div>
        <div className="desc">
          {engine.installed ? (
            <>
              <span className="mono">{engine.path}</span>
              {engine.installed_by ? ` · ${engine.installed_by}` : ""}
            </>
          ) : (
            (engine.install?.summary ?? engine.note ?? "")
          )}
          {engine.installed && engine.note ? ` · ${engine.note}` : ""}
        </div>
        {manual && engine.install?.command && (
          <div className="code" style={{ marginTop: 8 }}>
            <span>{engine.install.command}</span>
            <CopyButton text={engine.install.command} label="command" />
          </div>
        )}
      </div>
      <div className="btn-row">
        {!engine.installed && actions.includes("install") && (
          <button className="btn sm primary" onClick={() => onAction("install")}>
            <Download size={13} /> Install…
          </button>
        )}
        {engine.installed && actions.includes("update") && (
          <button className="btn sm" onClick={() => onAction("update")}>
            <RefreshCw size={13} /> Update…
          </button>
        )}
        {engine.installed && actions.includes("uninstall") && (
          <button className="btn sm danger" onClick={() => onAction("uninstall")}>
            <Trash2 size={13} /> Remove…
          </button>
        )}
      </div>
    </div>
  );
}

/** Custom engine locations on the local host: a path to the server program, or empty for automatic. */
function EngineLocations({ engines, onSaved }: { engines: HFEngineStatus[]; onSaved: () => void }) {
  const { toast } = useApp();
  const [paths, setPaths] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string>();
  useEffect(() => {
    hf.settings()
      .then((r) => {
        setSaved(r.settings.engine_paths ?? {});
        setPaths(r.settings.engine_paths ?? {});
      })
      .catch(() => undefined);
  }, []);
  const save = async (engine: HFEngineId, name: string) => {
    const value = (paths[engine] ?? "").trim();
    setBusy(engine);
    try {
      const result = await hf.saveSettings({ engine_paths: { [engine]: value } });
      setSaved(result.settings.engine_paths ?? {});
      setPaths(result.settings.engine_paths ?? {});
      toast(value ? `${name} location saved` : `${name} is found automatically again`, "ok");
      onSaved();
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setBusy(undefined);
    }
  };
  return (
    <details className="hf-locations">
      <summary>Custom locations</summary>
      <div className="desc muted" style={{ margin: "6px 0 8px" }}>
        Use an engine installed somewhere ModelMatrix doesn't look. Enter the full path of its server program, or leave it empty to find it automatically.
      </div>
      {engines.map((engine) => (
        <div className="setting-row" key={engine.id}>
          <div className="title">{engine.name}</div>
          <div className="btn-row">
            <input
              className="input mono"
              aria-label={`${engine.name} location`}
              style={{ width: 300 }}
              placeholder="automatic"
              value={paths[engine.id] ?? ""}
              onChange={(e) => setPaths({ ...paths, [engine.id]: e.target.value })}
            />
            <button
              className="btn sm"
              disabled={busy === engine.id || (paths[engine.id] ?? "").trim() === (saved[engine.id] ?? "")}
              onClick={() => save(engine.id, engine.name)}
            >
              Save
            </button>
          </div>
        </div>
      ))}
    </details>
  );
}
