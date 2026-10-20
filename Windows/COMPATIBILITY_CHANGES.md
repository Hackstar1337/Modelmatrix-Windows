# Windows compatibility implementation summary

## Baseline and approach

The original repository was cloned from `Hackstar1337/ModelMatrix` at commit `214863ac289c0197a593bd9d61a1d915e134c356` (`Add reviewed model-to-app connections`). Changes are on the local `windows-support` branch; no upstream branch was changed or force-pushed.

The completed Windows source is published separately at `Hackstar1337/Modelmatrix-Windows`. Original Git history and MIT license are preserved.

Inspection found a macOS SwiftUI/AppKit application and controller, a portable React Dashboard, and a stdlib Python RemoteAgent with POSIX assumptions. This was not simply a Linux application needing path substitutions. SwiftUI/AppKit/Apple Network/Security frameworks and macOS packaging are not native Windows dependencies. The Windows implementation therefore reuses the Dashboard and RemoteAgent behind a Python local controller and optional Windows tray, while preserving the original Mac product and Linux agent architecture.

## Implemented changes

| Area | Resolved compatibility issues |
|---|---|
| Process launch and ownership | Windows argv/JSON launch, explicit shell modes, venv `Scripts` layout, Windows PATH handling, DLL-directory support, Job Objects with kill-on-close, suspended launch before job assignment, fail-safe assignment errors, exact creation-time ownership, descendant-tree termination, retained/reaped child handles. |
| Dangerous POSIX assumptions | Replaced Windows liveness probes with native handle/exit-code checks rather than `os.kill(pid, 0)`, guarded POSIX child reaping/WNOHANG, retained Unix process-group behavior. |
| Host facts/discovery | Win32 image/creation-time/RSS/RAM/uptime APIs and `netstat -ano`, without per-listener privileged CIM queries or `/proc` assumptions on Windows. |
| Local controller | Existing lifecycle/status modules plus Dashboard assets, profile editing, token login, revocable sessions, Host/origin checks, actual bound-port metadata, real graceful shutdown, direct-launch identity records. |
| Capture and usage | Real per-profile forwarding listeners, deterministic public/internal port substitution, owned PID reporting, shared SQLite store, listener cleanup, real SQL summary/timeseries/search/pagination/export rather than empty charts or synthetic success. |
| Optimization and desktop bridge | Byte-consistent physical/process memory reporting, activity protection/revalidation, owned model stop/unload actions, native visible-window inventory and cooperative identity-checked WM_CLOSE, Explorer and Unicode clipboard integration, accurate capability reporting. |
| Credentials and clients | Windows DPAPI storage for server-side credentials; private ACL-protected client key files; scoped model/chat endpoints; exact model/profile binding; approval-gated config updates, rollback and removal; no controller credential forwarded to model servers. |
| Hugging Face | Shared real download/engine managers adapted for Windows, CPU release selection through official nightly marker when latest has no binary, official URL/SHA-256 checks, approval-gated one-use install plans, Windows executable/DLL discovery, real keep-awake worker and orderly worker shutdown. |
| Remote gateways | Existing proxy architecture with strict route/query whitelist, server-side remote credentials, SSH host-key safeguards, staged source deployment and per-user Windows remote-agent installer; no unverified upstream module fallback. |
| Installation and distribution | PowerShell 5.1-compatible build/install/start/stop/verify/package/uninstall plus CMD wrappers, per-user venv/static assets, late script-root resolution, no null py-launcher parsing, no read-only `$PID` collision, safe stop fallback, Unicode-safe launchers, optional shortcuts/startup only. |
| Frontend and dependencies | Platform/capability-aware labels/actions, matching API shapes, frozen lockfile, pinned package manager, approved esbuild build dependency, build script without nested bare `pnpm` invocation (works with Corepack-only installs). |
| Configuration and examples | Real uppercase-key profiles accepted by the original loader, structured argv with full executable paths preserved, UTF-8 BOM/CRLF support for Windows editors and settings/token readers, valid Windows example profiles. |
| Regression protection | Windows stdlib/API/capture/client/security tests, genuine native child-tree test, profile/BOM tests, Dashboard platform tests and Windows CI workflow. |

The expanded audit also fixed concurrent byte-lock initialization, lost HOST/PORT expansion, remote Windows HF factory selection, usage-enabled/previous-period SQL behavior, capture disable/restart binding, JSONC string preservation, bracketed IPv6 URLs, tray native switches/CMD quoting, data-URL font CSP, authenticated startup/stop, uninstall shortcut ownership, caller-pipe inheritance and HTTP-handler/SQLite shutdown races. These are implemented changes with executable regressions, not documentation-only proposals.

## Architecture and preservation

The original Swift application, Swift package, Mac controller sources, app/DMG signing scripts, Raycast/macOS integration files and existing Linux behavior are retained. Windows does not execute the Unix/Mac packaging path. Shared Python changes are additive/platform-guarded; the original RemoteAgent regression suite passes on Linux. The existing tracked static Dashboard is rebuilt so installation from the delivered archive does not require Node.

## Capability and validation boundaries

This is an implemented Windows port, not a claim that every upstream engine became a Windows runtime. MLX/Metal and macOS-specific purge/signing/menu-bar internals remain platform-specific. Windows file-cache purge is intentionally unavailable rather than executing unsafe privileged substitutes. Native Windows 11 CPU inference, capture, usage, protected client endpoints and installation/build/start/stop were verified. Windows 10, GPU backends, remote machines and actual Kilo/OpenCode application workflows were not fully exercised; see [TEST_REPORT.md](TEST_REPORT.md).

No production model/client profile, unrelated repository, firewall, registry setting, execution-policy persistence or scheduled startup task was altered during validation. Test installation/data were kept in isolated directories. The delivered archives do not contain credentials, downloaded model weights, or machine-local runtime state.
