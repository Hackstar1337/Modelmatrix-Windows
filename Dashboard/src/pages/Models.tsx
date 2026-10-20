import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { ago, bytes, compact, hostLabel, num, pct, seconds } from "../lib/format";
import { useApp, usePlatform } from "../lib/state";
import type { ProfileTemplate, RuntimeInfo } from "../lib/types";
import { SplitPie } from "../components/charts";
import { FilterBar } from "../components/overview";
import { CopyButton, Empty, Notice, SectionCard } from "../components/ui";

export function ModelsPage({ onNewProfile }: { onNewProfile: () => void }) {
  const { overview, ollama, refreshOllama, settings, bridge } = useApp();
  const { localHostLabel } = usePlatform();
  const [runtimes, setRuntimes] = useState<RuntimeInfo[]>([]);
  const [templates, setTemplates] = useState<ProfileTemplate[]>([]);
  useEffect(() => {
    api<{ runtimes: RuntimeInfo[]; templates: ProfileTemplate[] }>("/api/profiles/runtimes")
      .then((r) => {
        setRuntimes(r.runtimes ?? []);
        setTemplates(r.templates ?? []);
      })
      .catch(() => undefined);
  }, []);
  const models = overview?.models ?? [];
  const relay = overview?.relay;
  const advanced = settings.mode === "advanced";
  const gateways = bridge?.gateways ?? [];
  return (
    <>
      <div className="page-title">
        <div>
          <h1>Model Insights</h1>
          <p>Which models do the work, how fast they run, and what Ollama has installed and loaded.</p>
        </div>
      </div>
      <FilterBar />
      <div className="grid-chart">
        <SectionCard id="models-table" title="Models" meta={`${models.length} models · ${settings.range}`}>
          {models.length ? (
            <div className="table-wrap">
              <table className="tbl compact">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Server</th>
                    <th className="num">Requests</th>
                    <th className="num">Share</th>
                    <th className="num">Tok in</th>
                    <th className="num">Tok out</th>
                    <th className="num">Avg t/s</th>
                    <th className="num">TTFT</th>
                    <th>Last used</th>
                  </tr>
                </thead>
                <tbody>
                  {models.map((m) => (
                    <tr key={`${m.model}-${m.host}-${m.profile}`}>
                      <td className="mono">
                        {m.model}
                        {m.tier === "cloud" && <span className="badge orange" style={{ marginLeft: 6 }}>CLOUD</span>}
                        {advanced && m.alias && m.alias !== m.model && <span className="badge amber" style={{ marginLeft: 6 }}>← {m.alias}</span>}
                      </td>
                      <td>
                        {hostLabel(m.host, gateways, localHostLabel)}
                        {advanced && m.profile && <span className="muted"> · {m.profile}</span>}
                      </td>
                      <td className="num">{compact(m.requests)}</td>
                      <td className="num">{pct(m.share * 100)}</td>
                      <td className="num">{compact(m.prompt_tokens)}</td>
                      <td className="num accent">{compact(m.completion_tokens)}</td>
                      <td className="num">{m.avg_tps ? num(m.avg_tps) : "-"}</td>
                      <td className="num">{seconds(m.avg_ttft_ms)}</td>
                      <td>{ago(m.last_used)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="No model usage in this range" />
          )}
        </SectionCard>
        <SectionCard id="models-split" title="Model split" meta="by requests">
          <SplitPie slices={models.slice(0, 7).map((m) => ({ name: m.model, value: m.requests })).concat(models.length > 7 ? [{ name: "Other", value: models.slice(7).reduce((s, m) => s + m.requests, 0) }] : [])} height={260} />
        </SectionCard>
      </div>

      <SectionCard
        id="ollama"
        title={
          <>
            Ollama <span className={`dot ${ollama?.reachable ? "on" : "off"}`} />
          </>
        }
        meta={ollama?.reachable ? `v${ollama.version ?? "?"} · ${ollama.base_url}` : "not running"}
        actions={
          <button className="btn ghost sm" onClick={() => refreshOllama()} aria-label="Refresh Ollama">
            <RefreshCw size={13} />
          </button>
        }
      >
        <div className="stack">
          {relay && (
            <Notice>
              To meter Ollama apps, point them at the ModelMatrix relay: <span className="mono accent">OLLAMA_HOST={relay.ollama_url}</span> or OpenAI base URL <span className="mono accent">{relay.url}</span>. Requests are forwarded to the right model server and recorded.
              <span style={{ marginLeft: 8 }}>
                <CopyButton text={relay.ollama_url} label="Ollama URL" />
                <CopyButton text={relay.url} label="OpenAI URL" />
              </span>
            </Notice>
          )}
          <div className="grid-2">
            <div>
              <div className="card-meta" style={{ marginBottom: 8 }}>
                Loaded in memory ({ollama?.loaded?.length ?? 0})
              </div>
              {ollama?.loaded?.length ? (
                <table className="tbl">
                  <tbody>
                    {ollama.loaded.map((m) => (
                      <tr key={m.name}>
                        <td className="mono">{m.name}</td>
                        <td className="num">{bytes(m.size_vram ?? m.size)}</td>
                        <td className="feed-small">{m.expires_at ? `unloads ${new Date(m.expires_at).toLocaleTimeString()}` : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty title="Nothing loaded" />
              )}
            </div>
            <div>
              <div className="card-meta" style={{ marginBottom: 8 }}>
                Installed ({ollama?.installed?.length ?? 0})
              </div>
              {ollama?.installed?.length ? (
                <table className="tbl">
                  <tbody>
                    {ollama.installed.map((m) => (
                      <tr key={m.name}>
                        <td className="mono">{m.name}</td>
                        <td className="muted">{[m.details?.parameter_size, m.details?.quantization_level].filter(Boolean).join(" · ")}</td>
                        <td className="num">{bytes(m.size)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty title={ollama?.reachable ? "No models installed" : "Ollama is not running"}>{ollama?.reachable ? "Pull one with: ollama pull gemma3" : "Start Ollama to see its models."}</Empty>
              )}
            </div>
          </div>
        </div>
      </SectionCard>

      <SectionCard
        id="runtimes"
        title="Supported runtimes"
        meta={`${runtimes.length} runtimes`}
        actions={
          <button className="btn sm primary" onClick={onNewProfile}>
            New profile
          </button>
        }
      >
        <div className="grid-3">
          {runtimes.map((r) => (
            <div key={r.id} className="tile">
              <div className="row between">
                <b>{r.label}</b>
                <span className="badge">{r.launch_mode}</span>
              </div>
              <div className="row wrap" style={{ gap: 4, marginTop: 8 }}>
                {r.tags.map((t) => (
                  <span key={t} className="badge">
                    {t}
                  </span>
                ))}
              </div>
              <div className="feed-small" style={{ marginTop: 6 }}>
                id: {r.id}
              </div>
            </div>
          ))}
        </div>
        {templates.length > 0 && <div className="feed-small" style={{ marginTop: 12 }}>Templates: {templates.map((t) => t.label).join(" · ")}</div>}
      </SectionCard>
    </>
  );
}
