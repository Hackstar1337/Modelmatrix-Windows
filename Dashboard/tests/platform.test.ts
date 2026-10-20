import assert from "node:assert/strict";
import test from "node:test";
import { platformContext } from "../src/lib/platform.ts";

test("legacy dashboard info keeps macOS defaults", () => {
  const context = platformContext();
  assert.equal(context.platform.os, "macos");
  assert.equal(context.localHostLabel, "This Mac");
  assert.equal(context.capabilities.open_folders, true);
  assert.equal(context.capabilities.purge_file_cache, true);
  assert.equal(context.bridgeLabel, "Menu bar app");
});

test("Windows metadata keeps controller-backed features while disabling unsupported cache purge", () => {
  const context = platformContext({
    name: "ModelMatrix",
    version: "test",
    controller_url: "http://127.0.0.1:7700",
    token_required: false,
    profiles_dir: "C:\\\\Profiles",
    controller_root: "C:\\\\ModelMatrix",
    platform: {
      os: "windows",
      display_name: "Windows 11",
      local_host_label: "This PC",
      credential_store: "Windows Credential Manager",
    },
    capabilities: {
      open_folders: true,
      quit_apps: true,
      purge_file_cache: false,
      optimize: true,
      clipboard: true,
      keep_awake: true,
      remote_gateways: true,
      capture: true,
      connect_apps: true,
      huggingface: true,
    },
  });
  assert.equal(context.isWindows, true);
  assert.equal(context.localHostLabel, "This PC");
  assert.equal(context.capabilities.purge_file_cache, false);
  assert.equal(context.capabilities.open_folders, true);
  assert.equal(context.capabilities.quit_apps, true);
  assert.equal(context.capabilities.optimize, true);
  assert.equal(context.capabilities.clipboard, true);
  assert.equal(context.desktopActionsAvailable, true);
  assert.equal(context.bridgeLabel, "Desktop bridge");
});
