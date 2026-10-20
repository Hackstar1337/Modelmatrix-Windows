import { useState } from "react";
import { FeedItem, FilterBar, Fleet, LiveFeed, RequestsTable } from "../components/overview";
import { LatencyChart, SplitPie, VolumeChart, useColors } from "../components/charts";
import { SectionCard, Tile } from "../components/ui";
import { compact, hostLabel, num, pct, seconds, trend } from "../lib/format";
import { useApp, usePlatform } from "../lib/state";

export function OverviewPage() {
  const { overview, status, ollama, settings, bridge } = useApp();
  const { localHostLabel } = usePlatform();
  const colors = useColors();
  const [split, setSplit] = useState<"tier" | "server">("tier");
  const s = overview?.summary;
  const t = overview?.totals;
  const advanced = settings.mode === "advanced";
  const gateways = bridge?.gateways ?? [];

  const loaded = new Set<string>();
  (status?.statuses ?? []).filter((p) => p.running && p.ready).forEach((p) => loaded.add(p.request_model || p.profile));
  (ollama?.loaded ?? []).forEach((m) => loaded.add(m.name));

  const requests = s?.requests ?? 0;
  const localShare = requests ? Math.round(((s?.local_requests ?? 0) / requests) * 100) : 0;
  const cloudShare = requests ? 100 - localShare : 0;
  const ratio = s && s.completion_tokens ? s.prompt_tokens / s.completion_tokens : null;
  const rangeLabel = settings.range.toUpperCase();

  const tierSlices = [
    { name: "Cloud", value: s?.cloud_requests ?? 0, color: colors.accent },
    { name: "Local", value: s?.local_requests ?? 0, color: colors.pieMuted },
  ];
  const serverMap = new Map<string, number>();
  for (const m of overview?.models ?? []) {
    const key = m.tier === "cloud" ? "Cloud" : m.host && m.host !== "local" ? hostLabel(m.host, gateways, localHostLabel) : m.profile || m.runtime || localHostLabel;
    serverMap.set(key, (serverMap.get(key) ?? 0) + m.requests);
  }
  const serverSlices = [...serverMap.entries()].sort((a, b) => b[1] - a[1]).map(([name, value]) => ({ name, value }));

  return (
    <>
      <div className="grid-top">
        <LiveFeed />
        <Fleet />
      </div>

      <div className="tiles-8">
        <Tile label="Local in VRAM" value={loaded.size} accent />
        <Tile label="Req/min" value={num(t?.requests_per_minute ?? 0)} />
        <Tile label="Req 24h" value={compact(t?.requests_24h ?? 0)} />
        <Tile label="Clients" value={s?.clients ?? 0} />
        <Tile label="Tok in" value={compact(s?.prompt_tokens ?? 0)} />
        <Tile label="Tok out" value={compact(s?.completion_tokens ?? 0)} accent />
        <Tile label="Avg t/s" value={s?.avg_tps ? num(s.avg_tps, 2) : "-"} trendValue={advanced ? trend(s?.avg_tps, s?.previous?.avg_tps) : undefined} />
        <Tile label="Local/Cloud" value={`${localShare}/${cloudShare}%`} />
      </div>
      <div className="tiles-4">
        <Tile label="Local requests" value={compact(s?.local_requests ?? 0)} sub={`${compact(s?.local_completion_tokens ?? 0)} tok out`} trendValue={advanced ? trend(s?.requests, s?.previous?.requests) : undefined} />
        <Tile label="Cloud requests" value={compact(s?.cloud_requests ?? 0)} accent sub={`${compact(s?.cloud_completion_tokens ?? 0)} tok out`} />
        <Tile label="Avg prompt tokens" value={compact(s?.avg_prompt_tokens ?? 0)} sub="per request" />
        <Tile label="In : out ratio" value={ratio ? `${num(ratio)}×` : "-"} sub="prompt to eval" />
      </div>
      {advanced && (
        <div className="tiles-4">
          <Tile label="TTFT p50 / p95" value={`${seconds(s?.p50_ttft_ms)} / ${seconds(s?.p95_ttft_ms)}`} sub={`avg ${seconds(s?.avg_ttft_ms)}`} />
          <Tile label="Peak t/s" value={s?.peak_tps ? num(s.peak_tps) : "-"} sub={`median ${s?.p50_tps ? num(s.p50_tps) : "-"}`} />
          <Tile label="Error rate" value={requests ? pct(((s?.errors ?? 0) / requests) * 100, 1) : "-"} sub={`${s?.errors ?? 0} failed`} />
          <Tile label="Estimated tokens" value={requests ? pct(((s?.estimated_rows ?? 0) / requests) * 100) : "-"} sub="servers that did not report usage" />
        </div>
      )}

      <FilterBar />

      <div className="grid-chart">
        <SectionCard id="volume" title="Requests + Token Volume" meta={rangeLabel}>
          <VolumeChart buckets={overview?.timeseries ?? []} range={settings.range} />
        </SectionCard>
        <section id="model-split" className="card card-pad section">
          <div className="card-head">
            <div className="card-title" style={{ lineHeight: 1.35 }}>
              Local vs cloud
              <br />({rangeLabel})
            </div>
            <div className="seg sm">
              <button className={split === "tier" ? "on" : ""} onClick={() => setSplit("tier")}>
                Tier
              </button>
              <button className={split === "server" ? "on" : ""} onClick={() => setSplit("server")}>
                Server
              </button>
            </div>
          </div>
          <SplitPie slices={split === "tier" ? tierSlices : serverSlices.length ? serverSlices : tierSlices} height={260} />
        </section>
      </div>

      {advanced && (
        <div className="grid-2" style={{ marginTop: 22 }}>
          <SectionCard title="Latency & throughput" meta={rangeLabel}>
            <LatencyChart buckets={overview?.timeseries ?? []} range={settings.range} height={240} />
          </SectionCard>
          <SectionCard title="Top models" meta={`${overview?.models?.length ?? 0} models`}>
            <div className="table-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th className="num">Req</th>
                    <th className="num">Share</th>
                    <th className="num">Tok out</th>
                    <th className="num">t/s</th>
                    <th className="num">TTFT</th>
                  </tr>
                </thead>
                <tbody>
                  {(overview?.models ?? []).slice(0, 8).map((m) => (
                    <tr key={m.model}>
                      <td className="mono">{m.model}</td>
                      <td className="num">{compact(m.requests)}</td>
                      <td className="num">{pct(m.share * 100)}</td>
                      <td className="num">{compact(m.completion_tokens)}</td>
                      <td className="num">{m.avg_tps ? num(m.avg_tps) : "-"}</td>
                      <td className="num">{seconds(m.avg_ttft_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </SectionCard>
        </div>
      )}

      <SectionCard id="recent-requests" title="Recent requests" meta={`${overview?.recent?.length ?? 0} latest`} className="" >
        {advanced ? (
          <RequestsTable rows={overview?.recent ?? []} gateways={gateways} advanced />
        ) : (
          <div className="grid-2" style={{ gap: 12 }}>
            {(overview?.recent ?? []).slice(0, 10).map((row) => (
              <FeedItem key={row.id} row={row} gateways={gateways} />
            ))}
            {!(overview?.recent ?? []).length && <RequestsTable rows={[]} gateways={gateways} advanced={false} />}
          </div>
        )}
      </SectionCard>
    </>
  );
}
