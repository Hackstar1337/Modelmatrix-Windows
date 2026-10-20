# ModelMatrix Windows test report

**Date:** 20 October 2026. **Baseline:** `214863ac289c0197a593bd9d61a1d915e134c356`. **Branch:** `windows-support`.

## Test environments

Native validation used an authorized Windows 11 x64 workspace, reported OS build 26300, Windows PowerShell 5.1, portable CPython 3.12.15, Node.js 24.19.0, and project-pinned pnpm 11.25.0 via Corepack. Python/source/runtime artifacts and installation/data/store directories were isolated in the task workspace. No global Python installation, startup task, firewall rule, registry setting or persistent execution-policy change was required.

Linux regression/build validation used the original cloned repository in the Ubuntu sandbox with Python 3.12 and the pinned pnpm toolchain. No Windows 10 or Mac device was available for native testing.

## Results

| Test | Result | What it demonstrates |
|---|---|---|
| Native Windows feature suite | **37 discovered; 35 passed, 2 intentional Unix-only skips** | Controller/session/Host checks, profile handling, bridge security/config rollback, HF contracts, secrets, distribution assertions, real capture and scoped HTTP client access. Some engine/client cases use mocks; not universal end-to-end verification. |
| Native Windows process/platform suite | **12 passed** | Real Job Object child/descendant termination and Unicode/space/quote argv round-trip, plus command/PATH/PID-reuse/listener/config regression checks. |
| Native CLI suite | **4 passed** | HTTP status/error handling, dry-run isolation and mutation session/origin contract. |
| Dashboard tests | **2 passed** on Windows and Linux | Platform/capability metadata and preserved legacy Mac defaults. |
| Native Dashboard dependency install/build | **Passed** | Frozen installation, esbuild native build dependency, TypeScript check and Vite production build with Corepack-only package-manager invocation. |
| PowerShell 5.1 parser | **Passed** | All Windows scripts and Windows RemoteAgent installer parse on the actual Windows host. |
| Actual Windows build script | **Passed** | Build script ran the pinned install/build and Python compilation. |
| Actual per-user installer | **Passed** | Isolated venv creation, correct Dashboard/dist staging, spaced paths and default source-root resolution; shortcuts/startup disabled for this test. |
| Installed startup/verification/shutdown | **Passed** | Actual HTTP controller startup/health, ownership validation and graceful shutdown: `stopped: true`, `forced: false`. |
| Direct controller launch/stop | **Passed** | Native ownership record written by the Python controller and consumed by stop script; actual graceful stop, no forced kill. |
| Actual Windows packaging script | **Passed** | Created ZIP plus SHA-256 output with the expected source/static layout. |
| Original/shared RemoteAgent suite on Linux | **157 discovered; 156 passed, 1 native-Win32 skip** | Existing Python agent behavior preserved, including new portable/Windows contracts. |
| Final Windows-oriented suite on Linux | **39 passed** | Includes two final profile-loadability/BOM reader regressions added after the native pass; these portable parsing changes are tested on Linux, not falsely counted as native tests. |
| Python compilation / Git whitespace check | **Passed** | Source compiles and the change set is whitespace-clean. |

The two native feature skips are specifically Unix mode-bit checks and a non-Windows production-store expectation. They are not skipped Windows feature implementations. The Linux native-process skip requires actual Win32 Job Objects.

## Genuine Windows inference, not a mock server

Official `llama-b11748-bin-win-cpu-x64.zip` was downloaded and SHA-256 verified:

`59083a1df768be176d851ddbab6bbde2d8a67a1ea7f1d42f770753d13dc74f60`

Model `stories260K.gguf`, pinned Hugging Face revision `96f460f9c1e2ca43d0adc1c682b832525a2c0ea3`, was SHA-256 verified:

`270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d`

The actual executable reported **0.7.0-dev, build 11748, commit 3bb63cbcb**, Clang 20.1.8, Windows x86_64. The controller launched it from a directory containing spaces, loaded its adjacent DLLs and the real GGUF, and observed an owned ready PID. A `/v1/chat/completions` request through the public Capture listener generated **8 completion tokens from 54 prompt tokens**. The response, SQLite summary and nonempty usage timeseries were checked.

A manual connection was then created with a production DPAPI-backed scoped key. Its generated `/api/connect/<id>/v1/models` and `/chat/completions` endpoints worked; connection verification called the actual protected model API and recorded `api_tested: true` without claiming `client_verified: true`. Model process and public capture listener were stopped afterward. This tiny model validates functionality, not useful output quality or production-scale performance.

A separate real Python HTTP fixture acceptance test verifies exactly one initial captured usage row, correct public/internal port/PID, listener cleanup, protected scoped endpoints even when a different global Dashboard token is configured, and rejection of an incorrect scoped key.

## Native failures found and fixed

Validation exposed concrete problems which were repaired and retested: Win32 handle/structure declarations in tray/DPAPI code; locked SQLite cleanup; POSIX executable test fixtures; nested bare pnpm under Corepack; structured command precedence; Windows `WNOHANG` assumptions (including capture stop); script-root parameter initialization; a PowerShell function parameter colliding with read-only `$PID`; full `llama-server.exe` paths being replaced by a basename; real scoped client dispatch/model-list authorization; and worker lifecycle/cleanup. The final core native runs passed after those fixes. Failed intermediate runs are not reported as successes.

## Not verified / inherent platform boundaries

- **Windows 10, Windows ARM64, older/newer Python/Node versions and hardened corporate policy combinations:** not tested natively.
- **CUDA/Vulkan/ROCm/other GPU drivers, large models, multimodal/tool-calling engines and runtime-specific benchmarks:** not exercised. The verified runtime was x64 CPU llama.cpp.
- **Ollama daemon load/unload, actual Kilo/OpenCode apps, remote SSH/HTTP agent deployment and remote HF flows:** implemented contracts/safeguards and unit fixtures, but no authorized external target/application was used for end-to-end confirmation.
- **Tray visual interaction, Start Menu/startup persistence, Explorer/clipboard interaction and cooperative closure of a real user application:** native ABI/inventory or contract checks only; user application windows were not closed as a test.
- **Long/resumable/gated production HF downloads and automatic engine installation:** manager/release/approval/hash fixtures tested; the real inference artifacts were independently downloaded/checksum-verified. Do not conflate that with every manager workflow being exercised.
- **Windows CI:** workflow included, not run remotely during this task. **Mac Swift build/tests/signing:** source preserved, not rerun on a Mac.
- **Apple-only MLX/Metal, macOS SwiftUI/menu-bar internals and macOS cache-purge/signing:** remain inherently platform-specific. Windows uses Dashboard/tray and truthful capabilities; no placeholder pretends those Apple components run on Windows.

Therefore this report establishes an implemented Windows port with verified core Windows 11 functionality, **not blanket certification of every original feature on every Windows 10/11 configuration**.

## Reproduce

Follow [README.md](README.md). Run the Windows feature, platform and CLI unittest commands there, `build-windows.cmd`, then install/start/verify/stop in isolated directories with `-NoShortcuts`. For real inference, provide the complete official runtime archive and a genuine GGUF, configure uppercase profile keys/structured argv with `{port}`, enable Capture, make a real model request, and verify usage rows and orderly process/listener shutdown.
