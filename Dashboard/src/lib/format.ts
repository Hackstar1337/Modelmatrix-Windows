// Formatting helpers that match the compact dashboard style (8.2k, 3.5M, 1.4d).

export function compact(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const abs = Math.abs(value);
  const fmt = (n: number, suffix: string) => {
    const fixed = n.toFixed(n >= 100 ? 0 : digits);
    return `${fixed.replace(/\.0+$/, "")}${suffix}`;
  };
  // Switch unit just below each boundary so 999.9M reads "1B", not "1000M".
  if (abs >= 999.5e6) return fmt(value / 1e9, "B");
  if (abs >= 999.5e3) return fmt(value / 1e6, "M");
  if (abs >= 999.5) return fmt(value / 1e3, "k");
  return Number.isInteger(value) ? String(value) : value.toFixed(digits);
}

export function num(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(digits);
}

export function int(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return Math.round(value).toLocaleString();
}

/** Seconds with two decimals below 10 s ("0.51s"), otherwise one. */
export function seconds(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "-";
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(2).replace(/0$/, "")}s`;
  if (s < 600) return `${s.toFixed(1)}s`;
  return duration(ms);
}

/** Long durations: 45s, 12m, 3.2h, 1.4d. */
export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "-";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = s / 60;
  if (m < 60) return `${m.toFixed(m < 10 ? 1 : 0)}m`;
  const h = m / 60;
  if (h < 24) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
}

export function clock(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "-";
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour12: false });
}

export function dateTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "-";
  return new Date(epochSeconds * 1000).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function ago(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "never";
  const delta = Date.now() / 1000 - epochSeconds;
  if (delta < 5) return "just now";
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}

export function gb(mb: number | null | undefined): string {
  if (mb === null || mb === undefined) return "-";
  return (mb / 1024).toFixed(1);
}

export function bytes(value: number | null | undefined): string {
  if (!value) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function pct(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return `${value.toFixed(digits)}%`;
}

export function trend(current: number | null | undefined, previous: number | null | undefined) {
  if (!current || !previous) return null;
  const change = ((current - previous) / previous) * 100;
  if (!Number.isFinite(change) || Math.abs(change) < 0.5) return null;
  return change;
}

export function bucketLabel(epochSeconds: number, range: string): string {
  const date = new Date(epochSeconds * 1000);
  if (range === "1h" || range === "24h") {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  if (range === "7d") {
    return `${date.toLocaleDateString([], { weekday: "short" })} ${String(date.getHours()).padStart(2, "0")}h`;
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function hostLabel(host: string | undefined, gateways: { id: string; name: string }[] = [], localLabel = "This Mac"): string {
  if (!host || host === "local") return localLabel;
  return gateways.find((g) => g.id === host)?.name ?? host;
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}
