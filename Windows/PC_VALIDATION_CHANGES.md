# PC-specific Windows validation changes

This pass starts from public revision `88be941` and uses a fresh local checkout on the selected Windows 11 PC. It preserves the original Swift/macOS source and shared Linux agent architecture.

## Production repairs

- `Scripts/build-windows.ps1`: normalize a resolved Python path as an executable array, not its first character; use a quote-safe temporary Python compilation script instead of a fragile native `-c` string.
- `Windows/controller.py`: supply the Dashboard-required all-time `tokens` alias; implement real HTTP PUT dispatch for HF settings with the same host/auth/CSRF checks; handle expected client disconnects quietly while retaining genuine-fault reporting.
- `Windows/hf_local.py`: parse the actual Hub tree `lfs.oid` SHA-256 field in addition to `lfs.sha256`; never confuse the outer Git blob SHA-1 with a file SHA-256; honor explicit repository revisions.
- `Dashboard/src/components/HFSettings.tsx`: display the existing Mbps API unit as megabits/Mbit per second instead of incorrectly labeling it MB/s.
- `Windows/native_windows.py`: title-scope selected HWND closure after PID/creation-time/executable validation; include Explorer in appropriate user-app inventory without treating all system binaries as user apps; add conservative text-only clipboard helpers.
- `Windows/tray.py`: clean native resources and raise if tray icon registration fails instead of silently running iconless.
- `Windows/portable_pc.py`: add an optional relative-path bundle launcher, ownership-safe startup/shutdown, diagnostics, included-model chat, private per-process engine discovery, and bundle-local download data. This does not replace the standard Windows installer or shared agent.

## Maintained regressions

Added PC controller, HF, bridge, native desktop, distribution, portable relocation/profile safety, Dashboard totals, and live HTTP/schema regressions. Tests explicitly distinguish real network/native execution from deterministic fixtures. A blocked external account or ownership-unidentifiable Explorer window is not represented as passing.

The complete PC report and runnable bundle are generated outside this source tree, under the selected task's outputs directory. No runtime credentials, captured prompts, user client configuration, QA databases, model weights or engine binaries are committed to this source checkout.
