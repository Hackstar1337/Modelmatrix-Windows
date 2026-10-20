import { useEffect, useState } from "react";
import { Cpu, Server, Thermometer, Zap, Sparkles } from "lucide-react";
import { openOptimize } from "./Optimize";
import { api, remote } from "../lib/api";
import { clock, compact, gb, hostLabel, num, pct, seconds } from "../lib/format";
import { useApp, usePlatform, usePoll } from "../lib/state";
import type { Gateway, HostMetrics, InflightRow, RequestRow, StatusPayload } from "../lib/types";
import { Empty, Meter } from "./ui";

/* ---------------- Live feed ---------------- */

function runtimeBadge(row: { runtime?: string; host?: string; tier?: string }, gateways: Gateway[]) {
  if (row.tier === "cloud") return { text: "CLOUD", cls: "orange" };
  if (row.host && row.host !== "local") return { text: hostLabel(row.host, gateways).toUpperCase().slice(0, 14), cls: "" };
  const map: Record<string, string> = {
    ollama: "OLLAMA",
    "llama.cpp": "LLAMA.CPP",
    llamacpp: "LLAMA.CPP",
    "llama-server": "LLAMA.CPP",
    mlx: "MLX",
    "mlx-lm": "MLX",
    vllm: "VLLM",
    "lm-studio": "LM STUDIO",
    lmstudio: "LM STUDIO",
    external: "API",
  };
  const runtime = (row.runtime ?? "").toLowerCase();
  return { text: map[runtime] ?? (runtime ? runtime.toUpperCase().slice(0, 12) : "LOCAL"), cls: "" };
}

function useNow(ms = 500) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}

function LiveItem({ row, gateways, now }: { row: InflightRow; gateways: Gateway[]; now: number }) {
  const badge = runtimeBadge(row, gateways);
  const elapsed = now / 1000 - row.started_at;
  const ttft = row.first_token_at ? (row.first_token_at - row.started_at) * 1000 : null;
  const progress = Math.min(95, (row.chunks ?? 0) * 2);
  return (
    <div className="feed-item live">
      <div className="feed-model">
        <b>{row.model || "model"}</b>
        <span className={`badge ${badge.cls}`}>{badge.text}</span>
        <span className="badge amber">{row.stream ? "STREAMING" : "WAITING"}</span>
      </div>
      <div className="feed-right">
        <div className="feed-tps">{row.chunks ? `${row.chunks} chunks` : "…"}</div>
        <div className="feed-small">{seconds(elapsed * 1000)}</div>
      </div>
      <div className="feed-client">
        <span className="mono">{row.client_label || "client"}</span>
        <span className="muted">{row.client_process || row.endpoint}</span>
      </div>
      <div />
      <div className="feed-bottom">
        <span className="row" style={{ gap: 8 }}>
          <span className="mini-bar">
            <i style={{ width: `${progress}%`, background: "var(--amber)" }} />
          </span>
          <span className="feed-small">live</span>
        </span>
        <span className="feed-stats">
          {ttft !== null && <span>TTFT {seconds(ttft)}</span>}
          <span>{clock(row.started_at)}</span>
        </span>
      </div>
    </div>
  );
}

export function FeedItem({ row, gateways }: { row: RequestRow; gateways: Gateway[] }) {
  const badge = runtimeBadge(row, gateways);
  const failed = (row.status ?? 0) >= 400 || row.status === 0;
  const alias = row.model_requested && row.model_served && row.model_requested !== row.model_served ? row.model_requested : null;
  const prompt = row.prompt_tokens ?? 0;
  const out = row.completion_tokens ?? 0;
  const share = prompt + out > 0 ? Math.round((out / (prompt + out)) * 100) : 0;
  return (
    <div className={`feed-item ${failed ? "err" : ""}`} title={row.error || undefined}>
      <div className="feed-model">
        <b>{row.model || "unknown"}</b>
        <span className={`badge ${badge.cls}`}>{badge.text}</span>
        {alias && <span className="badge amber">← {alias}</span>}
        {failed && <span className="badge red">{row.status || "ERR"}</span>}
      </div>
      <div className="feed-right">
        <div className="feed-tps">{row.tokens_per_second ? `${num(row.tokens_per_second)} t/s` : "- t/s"}</div>
        <div className="feed-small">
          {row.tokens_estimated ? "~" : ""}
          {compact(out)} tok
        </div>
      </div>
      <div className="feed-client">
        <span className="mono">{row.client_name || row.client_label || "client"}</span>
        <span className="muted ellipsis">{row.client_process || row.endpoint}</span>
      </div>
      <div />
      <div className="feed-bottom">
        <span className="row" style={{ gap: 8 }} title="Output share of tokens (completion / total)">
          <span className="mini-bar">
            <i style={{ width: `${share}%` }} />
          </span>
          <span className="feed-small">{share}%</span>
        </span>
        <span className="feed-stats">
          {row.ttft_ms ? <span>TTFT {seconds(row.ttft_ms)}</span> : null}
          <span>{seconds(row.duration_ms)}</span>
          <span>{clock(row.started_at)}</span>
        </span>
      </div>
    </div>
  );
}

export function LiveFeed() {
  const { overview, bridge } = useApp();
  const now = useNow();
  const gateways = bridge?.gateways ?? [];
  const inflight = overview?.in_flight ?? [];
  const recent = overview?.recent ?? [];
  const today = overview?.totals?.requests_24h ?? 0;
  return (
    <section id="live-feed" className="card card-pad section">
      <div className="card-head">
        <div className="card-title">
          Live Feed <span className={`dot accent ${inflight.length ? "pulse" : ""}`} />
        </div>
        <span className="card-meta">{compact(today)} req today</span>
      </div>
      <div className="feed">
        {inflight.map((row) => (
          <LiveItem key={row.id} row={row} gateways={gateways} now={now} />
        ))}
        {recent.map((row) => (
          <FeedItem key={row.id} row={row} gateways={gateways} />
        ))}
        {!inflight.length && !recent.length && (
          <Empty title="No requests yet">
            Point your apps at <span className="mono accent">{overview?.relay?.url ?? "http://localhost:7701/v1"}</span> and requests show up here live.
          </Empty>
        )}
      </div>
    </section>
  );
}

/* ---------------- Fleet ---------------- */

interface HostCardProps {
  name: string;
  health: "ok" | "warn" | "bad";
  healthLabel: string;
  metrics?: HostMetrics;
  models: string[];
  stats?: { requests?: number; avg_tps?: number; avg_ttft_ms?: number };
  advanced: boolean;
  icon?: "local" | "remote";
  onOptimize?: () => void;
}

export function HostCard({ name, health, healthLabel, metrics, models, stats, advanced, onOptimize }: HostCardProps) {
  const gpu = metrics?.gpus?.[0];
  const memory = metrics?.memory;
  const memPercent = memory?.percent ?? (memory?.used_mb && memory?.total_mb ? (memory.used_mb / memory.total_mb) * 100 : null);
  const temperature = metrics?.temperature_c ?? gpu?.temperature_c;
  const power = metrics?.power_w ?? gpu?.power_w;
  const vram = gpu?.vram_total_mb && !(gpu as any)?.unified_memory ? gpu : null;
  return (
    <div className="card host-card">
      <div className="host-head">
        <div className="host-name">
          <Server size={18} />
          <span className="ellipsis">{name}</span>
          <span className={`health ${health}`}>
            <span className={`dot ${health === "ok" ? "on" : health === "warn" ? "warn" : "off"}`} /> {healthLabel}
          </span>
        </div>
        <div className="host-env">
          {temperature !== undefined && temperature !== null && (
            <span className="row" style={{ gap: 3 }}>
              <Thermometer size={12} /> {Math.round(temperature)}°
            </span>
          )}
          {power !== undefined && power !== null && (
            <span className="row" style={{ gap: 3 }}>
              <Zap size={12} /> {Math.round(power)}W
            </span>
          )}
          {metrics?.chip && <span>{metrics.chip}</span>}
          {onOptimize && (
            <button className="btn sm ghost host-opt no-print" onClick={onOptimize} title="Free memory on this host">
              <Sparkles size={12} /> Optimize
            </button>
          )}
        </div>
      </div>
      {advanced && <Meter label="CPU" percent={metrics?.cpu_percent ?? 0} value={pct(metrics?.cpu_percent ?? null)} />}
      <Meter label="GPU" percent={gpu?.util_percent ?? 0} value={pct(gpu?.util_percent ?? null)} />
      <Meter label="MEM" percent={memPercent} value={memory?.total_mb ? `${gb(memory.used_mb)}/${gb(memory.total_mb)}GB` : "-"} />
      {vram && <Meter label="VRAM" percent={vram.vram_total_mb ? ((vram.vram_used_mb ?? 0) / vram.vram_total_mb) * 100 : 0} value={`${gb(vram.vram_used_mb)}/${gb(vram.vram_total_mb)}GB`} />}
      <div className="host-models">
        {models.length ? models.map((m) => <span key={m} className="chip">{m}</span>) : <span className="feed-small">No models loaded</span>}
      </div>
      <div className="host-foot">
        <span>1H</span>
        <span>
          <b>{stats?.requests ?? 0}</b> req
        </span>
        <span>
          <b>{stats?.avg_tps ? num(stats.avg_tps) : "–"}</b> t/s
        </span>
        <span>
          <b>{stats?.avg_ttft_ms ? seconds(stats.avg_ttft_ms) : "–"}</b> TTFT
        </span>
      </div>
    </div>
  );
}

function RemoteHostCard({ gateway, advanced }: { gateway: Gateway; advanced: boolean }) {
  const { overview } = useApp();
  const status = usePoll<StatusPayload>((signal) => api(remote(gateway.id, "/api/status"), { signal }), 8000, [gateway.id]);
  const metrics = usePoll<HostMetrics>((signal) => api(remote(gateway.id, "/api/host/metrics"), { signal }), 6000, [gateway.id]);
  const usage = usePoll<{ requests?: number; avg_tps?: number | null; avg_ttft_ms?: number | null }>(
    (signal) => api(remote(gateway.id, "/api/usage/summary?range=1h"), { signal }),
    10000,
    [gateway.id],
  );
  const models = (status.data?.statuses ?? []).filter((s) => s.running).map((s) => s.request_model || s.profile);
  // Requests the Mac relayed to this host plus requests the host's own relay metered.
  const relayed = overview?.host_stats?.find((h) => h.host === gateway.id);
  const own = usage.data;
  const stats = own
    ? {
        requests: (own.requests ?? 0) + (relayed?.requests ?? 0),
        avg_tps: own.avg_tps ?? relayed?.avg_tps,
        avg_ttft_ms: own.avg_ttft_ms ?? relayed?.avg_ttft_ms,
      }
    : relayed;
  const offline = !!status.error && !status.data;
  return (
    <HostCard
      name={gateway.name}
      health={!gateway.enabled ? "warn" : offline ? "bad" : "ok"}
      healthLabel={!gateway.enabled ? "PAUSED" : offline ? "OFFLINE" : "HEALTHY"}
      metrics={metrics.data}
      models={[...new Set(models)]}
      stats={stats}
      advanced={advanced}
    />
  );
}

export function Fleet() {
  const { metrics, status, statusError, overview, bridge, ollama, settings } = useApp();
  const { localHostLabel, bridgeLabel } = usePlatform();
  const advanced = settings.mode === "advanced";
  const running = (status?.statuses ?? []).filter((s) => s.running);
  const models = new Set<string>();
  running.forEach((s) => models.add(s.request_model || s.profile));
  (ollama?.loaded ?? []).forEach((m) => models.add(m.name));
  const notReady = running.some((s) => !s.ready);
  const local = overview?.host_stats?.find((h) => h.host === "local");
  return (
    <section id="fleet" className="fleet section">
      <HostCard
        name={metrics?.host?.replace(/\.local$/, "") || localHostLabel}
        health={statusError ? "bad" : notReady ? "warn" : "ok"}
        healthLabel={statusError ? "OFFLINE" : notReady ? "LOADING" : "HEALTHY"}
        metrics={metrics}
        models={[...models]}
        stats={local}
        advanced={advanced}
        onOptimize={openOptimize}
      />
      {(bridge?.gateways ?? [])
        .filter((g) => g.enabled !== false)
        .map((gateway) => (
          <RemoteHostCard key={gateway.id} gateway={gateway} advanced={advanced} />
        ))}
      {!bridge?.app_connected && (
        <div className="feed-small" style={{ padding: "0 4px" }}>
          <Cpu size={12} style={{ verticalAlign: -2 }} /> Remote hosts appear here while the ModelMatrix {bridgeLabel.toLowerCase()} is connected.
        </div>
      )}
    </section>
  );
}

/* ---------------- Filter bar ---------------- */

export function FilterBar() {
  const { settings, update, bridge, overview } = useApp();
  const { localHostLabel } = usePlatform();
  const ranges = settings.mode === "advanced" ? (["1h", "24h", "7d", "30d"] as const) : (["24h", "7d", "30d"] as const);
  const gateways = bridge?.gateways ?? [];
  const knownHosts = new Set(["local", ...gateways.map((g) => g.id), ...(overview?.hosts ?? [])]);
  return (
    <div className="filterbar no-print-hide">
      <div className="card filters">
        <span className="label">Filter</span>
        <Server size={16} />
        <select className="select" value={settings.host} onChange={(e) => update({ host: e.target.value })} aria-label="Server filter">
          <option value="all">All servers</option>
          {[...knownHosts].map((host) => (
            <option key={host} value={host}>
              {hostLabel(host, gateways, localHostLabel)}
            </option>
          ))}
        </select>
        <Cpu size={16} />
        <select className="select" value={settings.tier} onChange={(e) => update({ tier: e.target.value as any })} aria-label="Tier filter">
          <option value="all">Local + cloud</option>
          <option value="local">Local only</option>
          <option value="cloud">Cloud only</option>
        </select>
      </div>
      <div className="seg">
        {ranges.map((range) => (
          <button key={range} className={settings.range === range ? "on" : ""} onClick={() => update({ range })}>
            {range}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---------------- Recent requests table ---------------- */

export function RequestsTable({ rows, gateways, advanced }: { rows: RequestRow[]; gateways: Gateway[]; advanced: boolean }) {
  const { localHostLabel } = usePlatform();
  if (!rows.length) return <Empty title="No requests in this range" />;
  return (
    <div className="table-wrap">
      <table className="tbl">
        <thead>
          <tr>
            <th>Time</th>
            <th>Model</th>
            <th>Client</th>
            <th>Server</th>
            {advanced && <th>Endpoint</th>}
            <th className="num">In</th>
            <th className="num">Out</th>
            <th className="num">t/s</th>
            <th className="num">TTFT</th>
            <th className="num">Duration</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const failed = (r.status ?? 0) >= 400 || r.status === 0;
            return (
              <tr key={r.id} title={r.error || undefined}>
                <td className="mono">{clock(r.started_at)}</td>
                <td className="mono">
                  {r.model || "-"}
                  {r.model_requested && r.model_served && r.model_requested !== r.model_served && <span className="badge amber" style={{ marginLeft: 6 }}>← {r.model_requested}</span>}
                </td>
                <td>
                  <span className="accent mono">{r.client_name || r.client_label}</span>
                  {advanced && r.client_process && <span className="muted"> · {r.client_process}</span>}
                </td>
                <td>
                  {hostLabel(r.host, gateways, localHostLabel)}
                  {r.profile && <span className="muted"> · {r.profile}</span>}
                  {r.tier === "cloud" && <span className="badge orange" style={{ marginLeft: 6 }}>CLOUD</span>}
                </td>
                {advanced && <td className="mono muted">{r.endpoint}</td>}
                <td className="num">
                  {r.tokens_estimated ? "~" : ""}
                  {compact(r.prompt_tokens ?? 0)}
                </td>
                <td className="num">
                  {r.tokens_estimated ? "~" : ""}
                  {compact(r.completion_tokens ?? 0)}
                </td>
                <td className="num">{r.tokens_per_second ? num(r.tokens_per_second) : "-"}</td>
                <td className="num">{seconds(r.ttft_ms)}</td>
                <td className="num">{seconds(r.duration_ms)}</td>
                <td>{failed ? <span className="badge red">{r.status || "ERR"}</span> : <span className="badge green">{r.status}</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
