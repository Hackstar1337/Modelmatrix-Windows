import type { DashboardCapabilities, DashboardInfo, PlatformInfo } from "./types";

export interface PlatformContext {
  platform: PlatformInfo;
  capabilities: DashboardCapabilities;
  isWindows: boolean;
  localHostLabel: string;
  folderLabel: string;
  bridgeLabel: string;
}

const macPlatform: PlatformInfo = {
  os: "macos",
  display_name: "macOS",
  local_host_label: "This Mac",
  credential_store: "macOS Keychain",
};

const macCapabilities: DashboardCapabilities = {
  open_folders: true,
  quit_apps: true,
  purge_file_cache: true,
  keep_awake: true,
  remote_gateways: true,
  capture: true,
  connect_apps: true,
  huggingface: true,
};

const windowsCapabilities: DashboardCapabilities = {
  open_folders: false,
  quit_apps: false,
  purge_file_cache: false,
  keep_awake: false,
  remote_gateways: false,
  capture: true,
  connect_apps: true,
  huggingface: true,
};

export function platformContext(info?: DashboardInfo): PlatformContext {
  const os = info?.platform?.os?.toLowerCase() ?? macPlatform.os;
  const isWindows = os === "windows" || os === "win32";
  const platform = {
    ...(isWindows ? { ...macPlatform, os: "windows", display_name: "Windows", local_host_label: "This PC", credential_store: "Windows protected credential store" } : macPlatform),
    ...info?.platform,
  };
  const defaults = isWindows ? windowsCapabilities : macCapabilities;
  const capabilities = { ...defaults, ...info?.capabilities };
  return {
    platform,
    capabilities,
    isWindows,
    localHostLabel: platform.local_host_label,
    folderLabel: isWindows ? "Open in Explorer" : "Open in Finder",
    bridgeLabel: isWindows ? "Desktop bridge" : "Menu bar app",
  };
}

export function localHostLabel(info?: DashboardInfo): string {
  return platformContext(info).localHostLabel;
}
