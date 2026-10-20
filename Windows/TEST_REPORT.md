# ModelMatrix Windows verification report

**Date:** 20 October 2026. **Original baseline:** `214863ac289c0197a593bd9d61a1d915e134c356`. This report covers the expanded implementation, not only the first porting pass.

## Environments

Native execution used **Windows 11 x64, build 26300**, Windows PowerShell **5.1**, portable CPython **3.12.15**, Node **24.19.0** and pinned pnpm **11.25.0** through Corepack. Code, install, venv, packages, models and data used task-owned folders, including paths with spaces. No global Python install, firewall/registry change, persistent execution-policy change or startup task was necessary. CMD wrappers use process-only script-policy handling; enforced corporate policy must still be respected.

Linux preservation checks use the original Ubuntu clone. No native Windows 10, ARM64 or macOS device was available. Windows 10 is an implementation target, **not a tested-device claim**.

## Completed native results

| Check | Result | Verified scope |
|---|---|---|
| Full RemoteAgent suite | **172 tests; 11 intentional POSIX-only skips; zero failures/errors** | Portable configuration races/locking, parsing, identity, launch/stop, native Job Object descendants, discovery, HF factories and shared contracts. |
| Full Windows feature suite | **78 tests; 2 intentional Unix-only skips; zero failures/errors** | Actual HTTP/session/origin/Host routes, profile CRUD, SQL usage/search/export/retention, capture toggles/restart, scoped clients/proxies, native process/window fixtures, HF pipelines, credentials, distribution and final race fixes. |
| CLI contracts | **4 passed** | HTTP success/error, dry-run isolation, mutation session/origin behavior and nonzero failures. |
| Dashboard | **Passed** | Frozen native install, TypeScript typecheck, frontend tests and Vite production build under Corepack. |
| PowerShell parser | **8 scripts passed** | Seven distribution scripts and Windows RemoteAgent installer parsed on PowerShell 5.1. |
| Authenticated installed lifecycle | **Passed** | Fresh isolated venv, spaced paths, BOM auth file, independent controller/relay ports, readiness/health, duplicate refusal, graceful stop, restart, repeated verify/stop and data-preserving uninstall. |
| Startup safety | **Passed** | Occupied-port refusal leaves unrelated fixture alive; invalid/stale identity handling; background launch returns under captured caller output without retaining caller pipes. |
| Concurrent shutdown | **Passed** | Eight real HTTP usage pollers finish before SQLite teardown; no handler exceptions. Regression maintained in the suite. |
| Native tray | **Passed** | Actual window/icon initialization, duplicate-instance refusal and owned tray Quit/window/mutex cleanup. |
| Windows package script | **Passed locally** | Source/static ZIP and SHA-256 output. Release-owner signing is not claimed. |
| Source integrity | **Passed** | Compilation and focused diff/whitespace checks. |

The RemoteAgent skips are intrinsically POSIX executable/Bash/GLIBC/shebang or Unix-system fixtures, not Windows implementations silently omitted. Windows engine/profile/process/installer counterparts are covered. The two feature skips concern Unix mode bits and a non-Windows production secret-store condition; real DPAPI encryption/decryption was tested on Windows.

Early worker `WinError 10013` failures came from missing loopback permission on those execution commands. The complete parent run explicitly enabled loopback access and passed the same tests; these are not unresolved application networking failures.

## Real inference, not a mock

Official **llama.cpp b11748 CPU x64** and a tiny GGUF were pinned and SHA-256 verified:

- `llama-b11748-bin-win-cpu-x64.zip`: `59083a1df768be176d851ddbab6bbde2d8a67a1ea7f1d42f770753d13dc74f60`
- `stories260K.gguf`, HF revision `96f460f9c1e2ca43d0adc1c682b832525a2c0ea3`: `270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d`

The executable reported **0.7.0-dev / build 11748 / commit 3bb63cbcb**, Clang 20.1.8, Windows x86_64. Adjacent DLLs were retained. The controller launched its owned process, loaded the GGUF and generated **8 completion tokens from 54 prompt tokens** through public Capture. Real SQLite summaries/timeseries were nonempty.

A production DPAPI-backed scoped connection then served `/api/connect/<id>/v1/models` and `/chat/completions`. Connection verification called the protected model API and recorded `api_tested: true`, without claiming a real Kilo/OpenCode application was verified. The owned model and capture listener stopped. This tiny model proves functionality, not useful output quality or large-model performance.

## Real browser coverage

An isolated native controller and QA data were used in the Desktop in-app browser. Overview, Models & Servers, Clients, Model Insights, Requests & Latency, Settings and Advanced mode rendered. A QA external profile was created through the actual modal and refreshed in the catalog. Twelve deterministic requests were inserted only in the QA database, and the populated request table rendered. Optimize opened/closed without destructive actions on user apps/models. **Final browser console: zero errors.**

The initial run revealed blocked bundled data-URL fonts. The font CSP was fixed without loosening script/request origins. Existing user Ollama services were observed only, never stopped/unloaded. Native close/optimization behavior used controlled fixture windows/processes, not arbitrary user applications.

## Expanded-run fixes

Real testing repaired lost HOST/PORT structured-command context; config-lock initialization under concurrent writers; shared remote HF selecting Linux adapters; JSON backslash path assertions; SQLite fixture cleanup; disabled usage still recording; SQL previous-period/host behavior; capture disable/restart port restoration; mismatched frontend capabilities; native tray switches/CMD quoting; JSONC commas corrupting quoted content; IPv6 client URLs; authenticated readiness/session handling; shortcut uninstall ownership; inherited PowerShell capture pipes; and active HTTP pollers racing a closed database.

Failed intermediate attempts are not counted as passes. Negative security/validation tests intentionally reject invalid requests.

## Fixtures and unverified boundaries

HF ZIP/DLL installation, hash/traversal protection, one-use approvals, model download/check variants, cancellation/resume, folder moves and engine settings use deterministic HTTP/archive fixtures. Real inference artifacts were independently official-download verified. This is not production gated-model account verification.

**Not fully exercised:** Windows 10/ARM64; GPU driver/backend permutations; large/multimodal models; real SSH/Tailscale target deployment; actual authenticated Kilo/OpenCode sessions; Explorer/clipboard interaction; persistent startup tasks/shortcuts; release signing; macOS Swift build/signing. Their code/contracts and fixture checks are not misrepresented as hardware/account certification.

Apple-only MLX/Metal, macOS cache purge/signing/SwiftUI internals are not Windows runtimes. Windows reuses the Dashboard/shared agent with native adapters and truthful capability reporting, not false-success placeholders.

## Reproduce and CI

Follow [README.md](README.md), run all three Python suites, Dashboard tests/build and Windows install/start/verify/stop/package scripts in owned folders. The public Windows workflow runs full suites, frozen build, authenticated installed lifecycle, packaging and data-preserving uninstall. Check GitHub Actions for the live result; queued is not passed.

**Conclusion:** observed Windows failures were fixed and tested core paths pass. This is not a guarantee of zero possible defects on every Windows 10/11 installation, third-party model, driver or account.
