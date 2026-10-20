import { useState, type ReactNode } from "react";
import {
  Activity,
  BarChart3,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Clock,
  Cpu,
  Download,
  FileDown,
  Gauge,
  HardDrive,
  KeyRound,
  LayoutGrid,
  ListOrdered,
  LogOut,
  Moon,
  Palette,
  PieChart,
  Plug,
  Plus,
  Radio,
  Server,
  Settings as SettingsIcon,
  ShieldCheck,
  Stethoscope,
  Sun,
  Timer,
  Users,
  Workflow,
  Info,
  Laptop,
  Network,
  Sparkles,
  PackagePlus,
  Wrench,
  Database,
} from "lucide-react";
import { compact, duration, num } from "../lib/format";
import { useApp, usePlatform, type Page } from "../lib/state";
import { api } from "../lib/api";
import { MatrixMark } from "./ui";
import { openOptimize } from "./Optimize";
import { DownloadsIndicator } from "./Downloads";
import { openHuggingFace } from "./HuggingFace";

export function Header() {
  const { settings, update, online, info, bridge } = useApp();
  const { bridgeLabel, localHostLabel, capabilities } = usePlatform();
  return (
    <header className="topbar">
      <a className="brand" href="#/overview" aria-label="ModelMatrix overview">
        <MatrixMark size={30} />
        <div>
          <div className="brand-title">ModelMatrix</div>
          <div className="brand-sub">Local LLM control center</div>
        </div>
      </a>
      <div className="topbar-right">
        <DownloadsIndicator />
        <button className="btn sm primary no-print" onClick={openOptimize} disabled={capabilities.optimize === false} title={`Free memory on ${localHostLabel}`}>
          <Sparkles size={14} /> Optimize
        </button>
        <span className="conn no-print" title={info?.controller_url}>
          <span className={`dot ${online ? "on" : "off"}`} /> {online ? "Controller" : "Offline"}
        </span>
        <span className="conn no-print" title={`${bridgeLabel} bridge`}>
          <span className={`dot ${bridge?.app_connected ? "on" : "warn"}`} /> {bridge?.app_connected ? bridgeLabel : `${bridgeLabel} off`}
        </span>
        <div className="seg lg no-print" role="tablist" aria-label="View mode">
          {(["standard", "advanced"] as const).map((mode) => (
            <button key={mode} className={settings.mode === mode ? "on" : ""} onClick={() => update({ mode })} role="tab" aria-selected={settings.mode === mode}>
              {mode === "standard" ? "Standard" : "Advanced"}
            </button>
          ))}
        </div>
      </div>
    </header>
  );
}

export function KpiStrip() {
  const { overview } = useApp();
  const t = overview?.totals;
  const items: { label: string; value: string; sub: string }[] = [
    { label: "Requests", value: compact(t?.requests ?? 0), sub: `24h: ${compact(t?.requests_24h ?? 0)}` },
    { label: "Tokens", value: compact(t?.tokens ?? 0), sub: `24h: ${compact(t?.tokens_24h ?? 0)}` },
    { label: "Avg t/s", value: t?.avg_tps ? num(t.avg_tps) : "-", sub: "all time" },
    { label: "Inf. time", value: duration(t?.inference_ms ?? 0), sub: "cumulative" },
    { label: "Clients", value: String(t?.clients ?? 0), sub: `active: ${t?.active_clients ?? 0}` },
    {
      label: "Local/Cloud",
      value: `${compact(t?.local_requests ?? 0)}/${compact(t?.cloud_requests ?? 0)}`,
      sub: "all time",
    },
  ];
  return (
    <div className="kpis">
      {items.map((item) => (
        <div className="kpi" key={item.label}>
          <div className="kpi-label">{item.label}</div>
          <div className="kpi-value">{item.value}</div>
          <div className="kpi-sub">{item.sub}</div>
        </div>
      ))}
    </div>
  );
}

const railItems: { page: Page; label: string; icon: ReactNode }[] = [
  { page: "overview", label: "Overview", icon: <LayoutGrid size={22} /> },
  { page: "servers", label: "Models & Servers", icon: <Server size={22} /> },
  { page: "clients", label: "Clients", icon: <Users size={22} /> },
  { page: "models", label: "Model Insights", icon: <Cpu size={22} /> },
  { page: "requests", label: "Requests & Latency", icon: <BarChart3 size={22} /> },
];

export function Rail() {
  const { page, setPage, info, settings, update } = useApp();
  const [menu, setMenu] = useState(false);
  return (
    <nav className="rail" aria-label="Sections">
      {railItems.map((item) => (
        <button key={item.page} className={`rail-btn ${page === item.page ? "on" : ""}`} onClick={() => setPage(item.page)} aria-label={item.label}>
          {item.icon}
          <span className="tip">{item.label}</span>
        </button>
      ))}
      <div className="rail-spacer" />
      <button className={`rail-btn ${page === "settings" ? "on" : ""}`} onClick={() => setPage("settings")} aria-label="Settings">
        <SettingsIcon size={22} />
        <span className="tip">Settings</span>
      </button>
      <div style={{ position: "relative" }}>
        <button className="avatar" onClick={() => setMenu((m) => !m)} aria-label="Account menu">
          H
        </button>
        {menu && (
          <div className="menu" onMouseLeave={() => setMenu(false)}>
            <div className="menu-head">
              <div style={{ color: "var(--text)", fontWeight: 600 }}>ModelMatrix {info?.version}</div>
              <div className="mono">{info?.controller_url}</div>
            </div>
            <button
              onClick={() => {
                update({ theme: settings.theme === "dark" ? "light" : "dark" });
                setMenu(false);
              }}
            >
              {settings.theme === "dark" ? <Sun size={15} /> : <Moon size={15} />} {settings.theme === "dark" ? "Light theme" : "Dark theme"}
            </button>
            <button
              onClick={() => {
                setPage("settings", "about");
                setMenu(false);
              }}
            >
              <Info size={15} /> About
            </button>
            {info?.token_required && (
              <button
                onClick={async () => {
                  await api("/api/dashboard/session", { method: "DELETE" }).catch(() => undefined);
                  location.reload();
                }}
              >
                <LogOut size={15} /> Sign out
              </button>
            )}
          </div>
        )}
      </div>
    </nav>
  );
}

type NavItem = { label: string; icon: ReactNode; section?: string; action?: () => void };

export function SubNav({ onNewProfile }: { onNewProfile: () => void }) {
  const { page, setPage, settings, update, overview } = useApp();
  const exportCsv = () => {
    const { range, host, tier } = settings;
    location.href = `/api/usage/export.csv?range=${range}&host=${encodeURIComponent(host)}&tier=${tier}`;
  };
  const navs: Record<Page, { title: string; items: NavItem[] }> = {
    overview: {
      title: "Overview",
      items: [
        { label: "Live Feed", icon: <Activity size={20} />, section: "live-feed" },
        { label: "Recent Requests", icon: <Clock size={20} />, section: "recent-requests" },
        { label: "Fleet", icon: <Server size={20} />, section: "fleet" },
        { label: "Model Split", icon: <PieChart size={20} />, section: "model-split" },
        { label: "Export PDF", icon: <FileDown size={20} />, action: () => window.print() },
      ],
    },
    servers: {
      title: "Models & Servers",
      items: [
        { label: "Profiles", icon: <Boxes size={20} />, section: "profiles" },
        { label: "New Profile", icon: <Plus size={20} />, action: onNewProfile },
        { label: "Add from Hugging Face", icon: <PackagePlus size={20} />, action: () => openHuggingFace() },
        { label: "Downloads", icon: <Download size={20} />, section: "downloads" },
        { label: "Downloaded Models", icon: <HardDrive size={20} />, section: "downloaded-models" },
        { label: "Remote Hosts", icon: <Network size={20} />, section: "remote-hosts" },
        { label: "Benchmarks", icon: <Gauge size={20} />, section: "benchmarks" },
      ],
    },
    clients: {
      title: "Clients",
      items: [
        { label: "All Clients", icon: <Users size={20} />, section: "clients-table" },
        { label: "Top Clients", icon: <BarChart3 size={20} />, section: "clients-chart" },
        { label: "Export CSV", icon: <Download size={20} />, action: exportCsv },
      ],
    },
    models: {
      title: "Model Insights",
      items: [
        { label: "Model Split", icon: <PieChart size={20} />, section: "models-split" },
        { label: "Model Table", icon: <ListOrdered size={20} />, section: "models-table" },
        { label: "Ollama", icon: <HardDrive size={20} />, section: "ollama" },
        { label: "Runtimes", icon: <Workflow size={20} />, section: "runtimes" },
      ],
    },
    requests: {
      title: "Requests & Latency",
      items: [
        { label: "Throughput", icon: <BarChart3 size={20} />, section: "throughput" },
        { label: "Latency", icon: <Timer size={20} />, section: "latency" },
        { label: "Request Log", icon: <ListOrdered size={20} />, section: "request-log" },
        { label: "Export CSV", icon: <Download size={20} />, action: exportCsv },
      ],
    },
    settings: {
      title: "Settings",
      items: [
        { label: "Appearance", icon: <Palette size={20} />, section: "appearance" },
        { label: "Usage & Relay", icon: <Radio size={20} />, section: "relay" },
        { label: "Hugging Face", icon: <Database size={20} />, section: "huggingface" },
        { label: "Engines", icon: <Wrench size={20} />, section: "engines" },
        { label: "Capture Mode", icon: <ShieldCheck size={20} />, section: "capture" },
        { label: "Controller", icon: <Laptop size={20} />, section: "controller" },
        { label: "Doctor", icon: <Stethoscope size={20} />, section: "doctor" },
        { label: "Integrations", icon: <Plug size={20} />, section: "integrations" },
        { label: "Desktop Bridge", icon: <KeyRound size={20} />, section: "menubar" },
        { label: "About", icon: <Info size={20} />, section: "about" },
      ],
    },
  };
  const nav = navs[page] ?? navs.overview;
  if (settings.subnavCollapsed) {
    return (
      <>
        <aside className="subnav collapsed" />
        <button className="btn sm expand-tab no-print" onClick={() => update({ subnavCollapsed: false })} aria-label="Show navigation">
          <ChevronRight size={14} /> {nav.title}
        </button>
      </>
    );
  }
  return (
    <aside className="subnav">
      <div className="subnav-head">
        <span>{nav.title}</span>
        <button className="icon-btn" onClick={() => update({ subnavCollapsed: true })} aria-label="Collapse navigation">
          <ChevronLeft size={18} />
        </button>
      </div>
      <div className="subnav-list">
        {nav.items.map((item) => (
          <button key={item.label} className="subnav-item" onClick={() => (item.action ? item.action() : setPage(page, item.section))}>
            {item.icon}
            {item.label}
          </button>
        ))}
      </div>
      <div className="subnav-foot">
        <div className="row between">
          <span>Relay</span>
          <span className="mono" style={{ color: overview?.relay?.listening ? "var(--green)" : "var(--amber)" }}>
            {overview?.relay?.listening ? `:${overview.relay.port} live` : "offline"}
          </span>
        </div>
        <div className="row between" style={{ marginTop: 4 }}>
          <span>Recorded</span>
          <span className="mono">{compact(overview?.relay?.requests_recorded ?? 0)}</span>
        </div>
      </div>
    </aside>
  );
}
