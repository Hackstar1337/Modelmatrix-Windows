# ModelMatrix on Windows 10 and Windows 11

The Windows port uses the **existing Dashboard and shared Python RemoteAgent**, a local controller, and an optional native Windows tray. It preserves the original macOS Swift application and Unix paths rather than rewriting them. It does not require Bash, Xcode, administrator privileges, or third-party Python packages.

Windows 11 x64 was tested natively, including real GGUF inference. Windows 10 is targeted but was not tested on a Windows 10 device. See [TEST_REPORT.md](TEST_REPORT.md) for exact verification boundaries and [COMPATIBILITY_CHANGES.md](COMPATIBILITY_CHANGES.md) for the changes.

## 1. Get the Windows source

Clone the public Windows repository into a writable directory:

```powershell
git clone https://github.com/Hackstar1337/Modelmatrix-Windows.git
cd .\Modelmatrix-Windows
```

Alternatively, extract the source ZIP from this repository. The original `Hackstar1337/ModelMatrix` upstream was not changed. Keep the complete source layout, including `Windows`, `RemoteAgent`, `Scripts`, and `Dashboard`.

## 2. Prerequisites

Use Python **3.10+**, resolved from `-PythonPath`, a real `python` executable, or `py -3`. The Microsoft Store alias is not an interpreter. Portable/per-user Python is supported.

Building the Dashboard requires Node.js **22.12+** and pnpm or Corepack. Run package-manager commands inside `Dashboard` so the pinned `packageManager` version is used. The scripts do not install prerequisites or modify global settings. Node is **not needed** to install the included static Dashboard with `-NoBuild`.

Inference requires a Windows-supported runtime and a model. Extract an official llama.cpp Windows release **with all adjacent DLLs**; copying only `llama-server.exe` is insufficient. Official x64 CPU release `b11748` and a checksum-verified `stories260K.gguf` were used for the real inference test.

MLX/Metal are Apple-only, not Windows engines. Linux-only runtimes require a suitable external Linux/WSL host or a native alternative. The controller reports unsupported capabilities instead of inventing successful installers.

## 3. Install

Open PowerShell in the source directory. The `.cmd` wrappers use process-scoped script execution-policy bypass, not a machine/user policy change:

```powershell
.\Scripts\install-windows.cmd -NoBuild
```

To build the Dashboard during installation, omit `-NoBuild`. For an explicit interpreter or isolated directories:

```powershell
.\Scripts\install-windows.cmd -PythonPath "C:\Python312\python.exe" -NoBuild -NoShortcuts `
  -InstallRoot "C:\Projects\ModelMatrix Installed" -DataRoot "C:\Projects\ModelMatrix Data"
```

Default installed code is `%LOCALAPPDATA%\Programs\ModelMatrix`; data is `%LOCALAPPDATA%\ModelMatrix`. Installation creates a stdlib-only virtual environment and optional Start Menu shortcuts. `-NoShortcuts` avoids those shortcuts. Startup is not enabled automatically, and upgrades retain user data.

## 4. Start and verify

```powershell
.\Scripts\start-windows.cmd -NoBrowser
.\Scripts\verify-windows.cmd
```

Open **http://127.0.0.1:7700/**. The shared usage relay defaults to **http://127.0.0.1:7701/v1**. Pass custom roots consistently:

```powershell
.\Scripts\start-windows.cmd -InstallRoot "C:\Projects\ModelMatrix Installed" `
  -DataRoot "C:\Projects\ModelMatrix Data" -Port 17770 -RelayPort 17771 -NoBrowser
.\Scripts\verify-windows.cmd -InstallRoot "C:\Projects\ModelMatrix Installed" `
  -DataRoot "C:\Projects\ModelMatrix Data" -Port 17770
```

Use `start-windows.cmd -Foreground -NoBrowser` for visible recovery. The optional tray is available from the installed **ModelMatrix Tray** shortcut or `python .\Windows\tray.py --no-browser`; it uses the same controller/API and a per-session mutex.

For custom roots/ports, set `MODELMATRIX_DATA_ROOT` and `MODELMATRIX_CONTROLLER_URL` before starting the tray, and use the CLI's `--endpoint` option. Background startup detaches caller pipe handles and polls authenticated readiness. Shutdown drains accepted HTTP handlers before closing SQLite.

## 5. Set up the first model

Profiles use the original **uppercase configuration keys**. Every JSON example in `Windows/examples` is tested with the real `ProfileRepository`. Edit its executable/model paths and model IDs to match your installed files before copying it.

For llama.cpp, edit `llama-cpp-windows.json`. `START_COMMAND_JSON` is a JSON-encoded argument array inside the profile; `--alias model.gguf` makes the requested ID match the server. `{port}` allows deterministic public/internal capture-port substitution. Do not build shell strings from untrusted values.

```powershell
$profiles = Join-Path $env:LOCALAPPDATA 'ModelMatrix\profiles'
New-Item -ItemType Directory -Force $profiles | Out-Null
Copy-Item .\Windows\examples\llama-cpp-windows.json $profiles
python .\Windows\modelmatrixctl.py start llama-cpp-windows
python .\Windows\modelmatrixctl.py status
```

Readiness requires the configured model ID, not merely any process listening on the port. Enable Capture in the Dashboard to meter direct profile requests: the owned server moves to an internal port and its public port becomes a forwarding listener using the shared usage database. Ambiguous custom commands are rejected rather than silently redirected.

For Ollama, install/start its Windows daemon and pull the desired model first. The example controls load/unload through that daemon; it does not claim ownership of another app's daemon. External profiles similarly describe already-running endpoints.

## 6. CLI, client connections, and authentication

```powershell
python .\Windows\modelmatrixctl.py --json list
python .\Windows\modelmatrixctl.py restart llama-cpp-windows
python .\Windows\modelmatrixctl.py usage --range 24h
python .\Windows\modelmatrixctl.py stop-all
```

The CLI also provides `doctor`, `switch`, `benchmark`, `optimize`, `HF`/`hf`, and `connect`; use `--help`. `--dry-run` only reports a plan, not a runtime success. Failures return nonzero status.

Connect Apps requires explicit plan/apply approval and generates an OpenAI-compatible `/api/connect/<connection-id>/v1` endpoint with a scoped key. Both model listing and chat use that key, not the Dashboard credential. Profile identity and readiness are revalidated. Automatic Kilo/OpenCode setup requires the exact supported client version; configuration written is not the same as client verified. Server-side secrets use DPAPI. Client-required key files are private ACL-protected files, not public configuration.

Loopback binding is the default. For authentication, create a private per-user token file containing at least 16 bytes of random token text, then use it consistently:

```powershell
.\Scripts\start-windows.cmd -AuthTokenFile "$env:LOCALAPPDATA\ModelMatrix\controller.token" -NoBrowser
python .\Windows\modelmatrixctl.py --auth-token-file "$env:LOCALAPPDATA\ModelMatrix\controller.token" status
.\Scripts\stop-windows.cmd -AuthTokenFile "$env:LOCALAPPDATA\ModelMatrix\controller.token"
```

The Dashboard supports token login and revocable HttpOnly sessions. The CLI also supports ephemeral `MODELMATRIX_TOKEN`. Readers accept UTF-8 BOM files produced by Windows editors/PowerShell. Never put credentials in profiles, public code, reports, or literal command arguments. Non-loopback binding requires an explicit unsafe-bind option and strong authentication; installation does not open firewall/public access.

## 7. Build, package, and tests

```powershell
.\Scripts\build-windows.cmd
.\Scripts\package-windows.cmd -Version 1.0.0
python -m unittest discover -s Windows\tests -p 'test*.py' -v
python -m unittest discover -s RemoteAgent\tests -p 'test*.py' -v
python -m unittest discover -s Windows\examples -p 'test*.py' -v
```

Build runs frozen pnpm installation, TypeScript/Vite build, and Python compilation. Package creates a ZIP and SHA-256 file with source/tests, static Dashboard, license, and documentation. It still requires Python and the chosen model/runtime. Code signing is a release-owner step. The Windows workflow runs full suites, build, authenticated installed lifecycle and packaging; check the Actions page for its current result. Expected POSIX-only skips are explained in the test report.

## 8. Stop, optional startup, and uninstall

```powershell
.\Scripts\stop-windows.cmd
.\Scripts\uninstall-windows.cmd
```

Stop validates PID/executable/creation time, requests genuine graceful shutdown, and revalidates before a bounded fallback. `forced` reports an actual kill. Direct controller launches also write ownership records. Uninstall retains user data; `uninstall-windows.cmd -Purge` intentionally deletes it.

Startup is opt-in: install with either `-StartupTask` for a limited per-user Scheduled Task or `-StartupShortcut` for a Startup-folder shortcut. Neither was enabled during native validation.

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| Python opens the Store or cannot run | Supply a real `-PythonPath`; inspect `python --version` / `py -3 --version`. |
| Node/pnpm missing | Install a compatible per-user toolchain, or install static output with `-NoBuild`. |
| Runtime DLL missing | Retain the entire official runtime archive and choose the correct architecture/backend and official prerequisites. |
| Profile absent | Use uppercase keys, valid `REQUEST_MODEL`, one `.env` **or** `.json`, and no reserved Windows device filename. |
| Model not ready | Check model ID/alias, executable/model paths, health endpoint, and runtime logs. |
| Port occupied | Choose another port; do not kill unrelated processes. |
| Stop refuses a PID | Investigate the ownership record instead of bypassing PID validation. |
| Corporate script policy blocks execution | Follow administrator policy; scripts do not alter enforced Group Policy or antivirus settings. |
| Download/remote integration fails | Check network/proxy/authentication, approved source, disk space, and the precise error. |

Inspect `%LOCALAPPDATA%\ModelMatrix\Logs\controller.log`, `controller-error.log`, and model logs. `verify-windows.cmd -NoControllerProbe` checks an offline installation only. A health response does **not** prove every GPU, model, client integration, or Apple-only runtime works on Windows.
