import assert from "node:assert/strict";
import test from "node:test";
import { routes } from "../src/lib/routes.ts";
import { platformContext } from "../src/lib/platform.ts";

const filters = { range: "24h" as const, host: "all", tier: "all" as const };

test("session, polling, usage, profile and action routes match controller paths", () => {
  assert.deepEqual(routes.dashboard.session("GET"), { method: "GET", path: "/api/dashboard/session" });
  assert.deepEqual(routes.dashboard.session("POST", "secret"), { method: "POST", path: "/api/dashboard/session", body: { token: "secret" } });
  assert.deepEqual(routes.dashboard.session("DELETE"), { method: "DELETE", path: "/api/dashboard/session" });
  assert.deepEqual(routes.dashboard.info(), { method: "GET", path: "/api/dashboard/info" });
  assert.deepEqual(routes.status(), { method: "GET", path: "/api/status" });
  assert.equal(routes.overview(filters).path, "/api/usage/overview?range=24h&host=all&tier=all");
  assert.equal(routes.usage.clients(filters).path, "/api/usage/clients?range=24h&host=all&tier=all");
  assert.equal(routes.usage.requests(filters, "qwen 3", 50, 100).path, "/api/usage/requests?range=24h&host=all&tier=all&q=qwen+3&limit=50&offset=100");
  assert.deepEqual(routes.usage.renameClient("client/win", "Workstation"), { method: "POST", path: "/api/usage/clients/rename", body: { client_id: "client/win", name: "Workstation" } });
  assert.deepEqual(routes.usage.saveSettings(30), { method: "POST", path: "/api/usage/settings", body: { retention_days: 30 } });
  assert.deepEqual(routes.usage.reset(), { method: "POST", path: "/api/usage/reset", body: { confirm: true } });
  assert.deepEqual(routes.profile.document("qwen/win profile"), { method: "GET", path: "/api/profiles/document?profile=qwen%2Fwin+profile" });
  assert.deepEqual(routes.profile.save({ profile: "qwen", create: true, values: { RUNTIME: "ollama" } }), { method: "POST", path: "/api/profiles/save", body: { profile: "qwen", create: true, values: { RUNTIME: "ollama" } } });
  assert.deepEqual(routes.profile.capture("qwen", true), { method: "POST", path: "/api/profiles/capture", body: { profile: "qwen", enabled: true } });
  assert.deepEqual(routes.profile.duplicate("qwen", "qwen-copy"), { method: "POST", path: "/api/profiles/duplicate", body: { profile: "qwen", new_profile: "qwen-copy" } });
  assert.deepEqual(routes.action("switch", "qwen"), { method: "POST", path: "/api/switch", body: { profile: "qwen" } });
  assert.deepEqual(routes.stopAll(), { method: "POST", path: "/api/stop-all", body: {} });
  assert.deepEqual(routes.benchmark(["qwen"], "context", { allow_concurrent: true, keep_running: false }), { method: "POST", path: "/api/benchmark/start", body: { suite: "context", allow_concurrent: true, keep_running: false, profiles: ["qwen"] } });
  assert.deepEqual(routes.config.profilesDir("C:\\ModelMatrix\\Profiles"), { method: "POST", path: "/api/config/profiles-dir", body: { profiles_dir: "C:\\ModelMatrix\\Profiles" } });
  assert.deepEqual(routes.integrations("droid", "sync"), { method: "POST", path: "/api/integrations/run", body: { integration: "droid", action: "sync" } });
});

test("bridge, remote and optimize routes preserve encoded Windows gateway IDs", () => {
  assert.deepEqual(routes.bridge.state(), { method: "GET", path: "/api/bridge/state" });
  assert.deepEqual(routes.bridge.command("gateway.add", { base_url: "http://192.0.2.2:7700" }), { method: "POST", path: "/api/bridge/commands", body: { type: "gateway.add", payload: { base_url: "http://192.0.2.2:7700" } } });
  assert.equal(routes.bridge.commandStatus("id with slash").path, "/api/bridge/commands/id%20with%20slash");
  assert.deepEqual(routes.remote("win host/1", routes.status()), { method: "GET", path: "/api/remote/win%20host%2F1/api/status" });
  assert.deepEqual(routes.remote("win host/1", routes.action("start", "qwen")), { method: "POST", path: "/api/remote/win%20host%2F1/api/start", body: { profile: "qwen" } });
  assert.deepEqual(routes.optimize.plan(), { method: "GET", path: "/api/optimize/plan" });
  assert.deepEqual(routes.optimize.run(["profile:qwen"]), { method: "POST", path: "/api/optimize/run", body: { items: ["profile:qwen"] } });
  assert.deepEqual(routes.optimize.purge(), { method: "POST", path: "/api/optimize/purge", body: {} });
});

test("Hugging Face routes match host-in-query and host-in-body controller contracts", () => {
  assert.equal(routes.hf.check("owner/model", "win-box", 8192).path, "/api/hf/check?name=owner%2Fmodel&host=win-box&context=8192");
  assert.equal(routes.hf.downloads().path, "/api/hf/downloads?host=all");
  assert.equal(routes.hf.models().path, "/api/hf/models?host=all");
  assert.deepEqual(routes.hf.download({ name: "owner/model", version: "Q4_K_M", host: "win-box", engine: "llama.cpp" }), { method: "POST", path: "/api/hf/downloads", body: { name: "owner/model", version: "Q4_K_M", host: "win-box", engine: "llama.cpp" } });
  assert.deepEqual(routes.hf.downloadAction("download-1", "cancel", "win-box", true), { method: "POST", path: "/api/hf/downloads/download-1/cancel", body: { host: "win-box", delete_files: true } });
  assert.equal(routes.hf.removeDownload("download-1", "win-box").path, "/api/hf/downloads/download-1?host=win-box");
  assert.deepEqual(routes.hf.deleteModel("download-1", false, "win-box"), { method: "POST", path: "/api/hf/models/delete", body: { id: "download-1", delete_files: false, host: "win-box" } });
  assert.deepEqual(routes.hf.saveSettings({ connections_per_file: 4 }), { method: "PUT", path: "/api/hf/settings", body: { connections_per_file: 4 } });
  assert.deepEqual(routes.hf.saveToken("hf_test"), { method: "POST", path: "/api/hf/token", body: { token: "hf_test" } });
  assert.equal(routes.hf.engines("win-box", true).path, "/api/hf/engines?host=win-box&refresh=1");
  assert.deepEqual(routes.hf.enginePlan("llama.cpp", "install", "win-box", "official"), { method: "POST", path: "/api/hf/engines/plan", body: { engine: "llama.cpp", action: "install", host: "win-box", method: "official" } });
  assert.deepEqual(routes.hf.engineRun("plan-abc"), { method: "POST", path: "/api/hf/engines/run", body: { plan_id: "plan-abc" } });
  assert.equal(routes.hf.engineJob("eng/abc", 4).path, "/api/hf/engines/jobs/eng%2Fabc?since=4");
  assert.deepEqual(routes.hf.cancelEngineJob("eng-abc"), { method: "POST", path: "/api/hf/engines/jobs/eng-abc/cancel", body: {} });
});

test("Connect routes require approval fields and preserve profile/connection query separation", () => {
  assert.deepEqual(routes.connect.profiles(), { method: "GET", path: "/api/connect/profiles" });
  assert.deepEqual(routes.connect.targets(), { method: "GET", path: "/api/connect/targets" });
  assert.equal(routes.connect.details("local:qwen", "connection/1").path, "/api/connect/details?profile=local%3Aqwen&connection=connection%2F1");
  assert.deepEqual(routes.connect.plan("local:qwen", "manual", true), { method: "POST", path: "/api/connect/plan", body: { profile: "local:qwen", target: "manual", make_default: true } });
  assert.deepEqual(routes.connect.apply("plan-1"), { method: "POST", path: "/api/connect/apply", body: { plan_id: "plan-1", approved: true } });
  assert.deepEqual(routes.connect.verify("connection-1"), { method: "POST", path: "/api/connect/verify", body: { id: "connection-1" } });
  assert.deepEqual(routes.connect.disconnect("connection-1"), { method: "POST", path: "/api/connect/disconnect", body: { id: "connection-1", remove_config: true, approved: true } });
  assert.deepEqual(routes.connect.undo("connection-1"), { method: "POST", path: "/api/connect/undo", body: { id: "connection-1", approved: true } });
  assert.deepEqual(routes.connect.secret("connection-1"), { method: "POST", path: "/api/connect/key", body: { id: "connection-1", approved: true } });
});

test("Windows platform metadata keeps controller-backed UI capabilities and disables cache purge", () => {
  const context = platformContext({
    name: "ModelMatrix", version: "test", controller_url: "http://127.0.0.1:7700", token_required: false,
    profiles_dir: "C:\\Profiles", controller_root: "C:\\ModelMatrix",
    platform: { os: "windows", display_name: "Windows 11", local_host_label: "This PC", credential_store: "Windows Credential Manager" },
    capabilities: { open_folders: true, quit_apps: true, purge_file_cache: false, optimize: true, clipboard: true, keep_awake: true, remote_gateways: true, capture: true, connect_apps: true, huggingface: true },
  });
  assert.equal(context.isWindows, true);
  assert.equal(context.desktopActionsAvailable, true);
  assert.equal(context.folderLabel, "Open in Explorer");
  assert.equal(context.bridgeLabel, "Desktop bridge");
  assert.equal(context.capabilities.optimize, true);
  assert.equal(context.capabilities.clipboard, true);
  assert.equal(context.capabilities.purge_file_cache, false);
});

test("native Windows-only checks are skipped on Linux; mocks do not claim native verification", { skip: process.platform !== "win32" }, () => {
  assert.equal(process.platform, "win32");
});
