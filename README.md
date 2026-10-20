<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="Resources/Brand/title-dark.svg">
  <img src="Resources/Brand/title-light.svg" alt="ModelMatrix" width="560">
</picture>
<p>
  <em><strong>Modelmatrix-Windows: local LLM control on Windows 10/11, with the original Mac/Linux sources preserved.</strong></em><br/>
  <strong>Windows Dashboard + native tray at <code>http://localhost:7700</code>.</strong>
</p>
<p>
  <a href="Windows/README.md"><strong>Windows installation and setup</strong></a>
  <a href="DASHBOARD.md"><img alt="Web dashboard" src="https://img.shields.io/badge/Web-Dashboard-111111?style=for-the-badge&logo=googlechrome&logoColor=white"></a>
  <a href="SETUP.md"><img alt="Docs" src="https://img.shields.io/badge/Docs-SETUP.md-1F6FEB?style=for-the-badge&logo=readthedocs&logoColor=white"></a>
</p>
<p>
  <a href="VERSION"><img alt="Version" src="https://img.shields.io/badge/version-1.0.0-blue?style=flat-square"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green?style=flat-square"></a>
  <a href="#requirements"><img alt="Platform" src="https://img.shields.io/badge/macOS-14%2B-lightgrey?style=flat-square&logo=apple&logoColor=white"></a>
  <a href="Package.swift"><img alt="Swift" src="https://img.shields.io/badge/swift-6.0-orange?style=flat-square&logo=swift&logoColor=white"></a>
  <a href="CHANGELOG.md"><img alt="Changelog" src="https://img.shields.io/badge/changelog-1.0.0-EF6155?style=flat-square"></a>
</p>
</div>

---

## Windows quick start

This repository contains the implemented Windows port of [ModelMatrix](https://github.com/Hackstar1337/ModelMatrix). It reuses the original Dashboard and Python agent, with native Windows lifecycle, metrics, credentials, tray and installation scripts. Windows 11 was tested directly; Windows 10 is targeted but was not tested on a Windows 10 device.

```powershell
git clone https://github.com/Hackstar1337/Modelmatrix-Windows.git
cd .\Modelmatrix-Windows
.\Scripts\install-windows.cmd -NoBuild
.\Scripts\start-windows.cmd -NoBrowser
.\Scripts\verify-windows.cmd
```

Open **http://127.0.0.1:7700/**. Python 3.10+ and a Windows-compatible model/runtime are required. The committed static Dashboard permits `-NoBuild`; Node/pnpm are needed only to rebuild it.

Read the [complete Windows guide](Windows/README.md), [compatibility changes](Windows/COMPATIBILITY_CHANGES.md), and [verified test report](Windows/TEST_REPORT.md). Apple-only MLX/Metal cannot run on Windows; native Windows alternatives and remote Linux hosts are supported without pretending platform-only features are portable. The original upstream repository was not modified.

## Original Mac and Linux product

ModelMatrix's original macOS control center starts, stops and switches runtimes such as `llama.cpp`, MLX, Ollama, vLLM, SGLang, TGI, LM Studio and many more, checks that each endpoint actually answers, tracks every request your apps make, and manages remote GPU hosts over SSH or Tailscale.

You can drive it two ways, and both offer the same features: a native **SwiftUI menu bar app**, and a **web dashboard** that the bundled controller serves on `http://localhost:7700`.

## New: connect a model to an app

Choose **Connect to an app** in Models & Servers, select a model and Kilo Code or OpenCode, review the plan, and let ModelMatrix set the client JSON/JSONC's base URL, exact Model ID and private key-file reference. Automatic writing is qualified for Kilo Code 7.8.12 and OpenCode 1.16.2 CLI/TUI; it never upgrades clients or replaces unrelated settings. **Other app** shows the correct manual endpoints and key requirement. Protected connections stay bound to one model, with separate configured/API-tested/client-use states, safe disconnect and undo. Also available in the menu bar and `modelmatrixctl connect`. See [Connect a model to an app](CONNECT_APPS.md).

## Latest feature: add models from Hugging Face

Enter a Hugging Face model name and ModelMatrix checks its format, available versions, memory and disk requirements, login status and required engine **before downloading anything**. Choose a version, download it with resume and checksum verification, and get an automatically configured profile, startup and test chat. Missing engines are installed **only after you approve the install plan**. Available in the dashboard, menu bar and CLI, on Apple Silicon and Intel Macs and managed Linux hosts. See [the full update notes](CHANGELOG.md#unreleased).

<table>
<tr>
<td align="center" width="33%" valign="top">
<h3>Switch in one click</h3>
<p>Pick a profile and press <strong>Activate</strong>. Every other model stops and the chosen runtime comes up at an OpenAI-compatible endpoint.</p>
</td>
<td align="center" width="33%" valign="top">
<h3>See every request</h3>
<p>A local metering relay records which app called which model, tokens in and out, time to first token and tokens per second. Prompts are never stored.</p>
</td>
<td align="center" width="33%" valign="top">
<h3>Your whole fleet</h3>
<p>Add Linux GPU servers by pairing link or SSH. ModelMatrix installs a small agent and shows their models, GPU, memory and traffic next to your Mac.</p>
</td>
</tr>
</table>

---

## Web dashboard

<p align="center">
  <img src="Resources/Brand/dashboard-overview.png" alt="ModelMatrix web dashboard overview" width="900">
</p>

Open it from the menu bar (the compass button in the header, or **Usage → Open Web Dashboard**), or go straight to **http://localhost:7700**. It is served by the controller that ships inside the app, so nothing extra is installed and nothing leaves your machine.

| Page | What you get |
|---|---|
| **Overview** | Live feed of in-flight and recent requests, fleet cards per host (GPU, memory, loaded models, last-hour traffic), KPI strip (requests, tokens, average t/s, inference time, clients, local vs cloud), requests and token volume chart, local vs cloud split, PDF export |
| **Models & Servers** | Every profile with Activate / Start / Stop / Restart / Benchmark, create, edit (form or raw file), duplicate, delete, capture mode, **Add from Hugging Face**, Downloads and Downloaded models, **Connect to an app**, remote hosts (pair, add, edit, pause, install or update agent, remove), benchmark results |
| **Clients** | Every app that called a model, with friendly renames, request and token totals, favourite model, last seen |
| **Model Insights** | Model split, per-model speed and latency, Ollama installed and loaded models, runtime catalog |
| **Requests & Latency** | TTFT and throughput charts with percentiles, searchable request log, CSV export |
| **Settings** | Theme and accent, relay and retention, Hugging Face (models folder, mirror, token, download options), engines (install, update, remove, custom paths), capture mode, controller doctor, integrations, profiles folder, menu bar shortcuts |
| **Optimize** (sparkles button in the header, or **Optimize** on the This Mac card) | Frees memory: see [Free up memory](#free-up-memory-optimize) |

<table>
<tr>
<td width="50%"><img src="Resources/Brand/dashboard-servers.png" alt="Models and servers"></td>
<td width="50%"><img src="Resources/Brand/dashboard-requests.png" alt="Requests and latency"></td>
</tr>
<tr>
<td align="center"><sub>Models &amp; Servers</sub></td>
<td align="center"><sub>Requests &amp; Latency</sub></td>
</tr>
</table>

**Standard / Advanced** in the header switches between a clean view and one with extra columns, percentiles and raw identifiers. Details, security model and API reference: **[DASHBOARD.md](DASHBOARD.md)**.

---

## Menu bar app

<p align="center">
  <img src="Resources/Brand/menubar-usage.png" alt="Menu bar panel with the Usage inspector" width="640">
</p>

The menu bar panel is the quick way in. It has everything the dashboard has:

| Panel | Contents |
|---|---|
| **Main panel** | One-tap Activate / Stop per model, hero card for the live model, CPU / GPU / memory sparklines, Stop everything, reopen last model, remote host sections |
| **Usage** (chart icon) | The dashboard's KPIs, live feed, model split, local vs cloud, clients with rename, relay URLs, retention and clear history, CSV export, capture mode switches, Ollama status |
| **Profiles** (stack icon) | Create from a template, edit any key or the raw file, duplicate, delete, **Connect to an app** |
| **Optimize** (sparkles icon in the header) | The same memory breakdown, presets, checklist, app selection (with Select all) and results as the dashboard's Optimize window |
| **Hugging Face Downloads** (download arrow in the header, with a count while downloads run) | The same check, version choice, approve-first engine installs, Downloads list (pause, resume, cancel) and Downloaded models (start, delete) as the dashboard, for this Mac and remote hosts, plus a Download settings card (models folder, mirror, speed, token, engine locations) |
| **Benchmarks** | Latest run, prefill scaling (TTFT at 1k/4k/8k context), ranked decode speed, CSV export |
| **Remote Hosts / Settings** | Pairing, SSH agent install and update, appearance, launch at login, controller connection, doctor, integrations |

Changes made in one place show up in the other within a few seconds: both talk to the same controller, and the app reports its remote hosts to the controller so the dashboard can reach them through the app's SSH tunnels.

---

## Usage tracking

Point any OpenAI- or Ollama-compatible client at the relay instead of the model's own port:

| Client type | Base URL |
|---|---|
| OpenAI-compatible (Cursor, Continue, Open WebUI, scripts) | `http://127.0.0.1:7701/v1` |
| Ollama-compatible | `http://127.0.0.1:7701` |

The relay forwards each request to the running model named in its `model` field (falling back to the best ready model, with the active profile winning ties), streams the answer back untouched, and records metadata only: time, client, model, host, status, token counts, time to first token and speed.

Prefer not to change app settings? Turn on **capture mode** for a profile. ModelMatrix then starts that model on a private port and puts the relay on the model's usual port, so existing clients are measured without any change. Capture mode is off by default.

History lives in `~/Library/Application Support/ModelMatrix/Controller/usage/usage.sqlite3`; retention defaults to 30 days and can be changed or cleared from either interface.

---

## Free up memory (Optimize)

Models are the biggest memory users on a Mac that runs them locally. **Optimize** hands that memory back in one click, from the dashboard, the menu bar or the terminal.

It shows memory the way Activity Monitor does (App, Wired, Compressed, Cached files, Free, swap and pressure), then lists what it can free with the size of each:

| Item | What Optimize does |
|---|---|
| Loaded Ollama models | Unloads them. Ollama loads a model again on its next request. |
| Running model servers (llama.cpp, MLX, vLLM and other profiles) | Stops them. Start them again any time. |
| File cache | Runs macOS's `purge` to drop cached files, including the model files of servers it just stopped. macOS asks for your password. |

Pick a preset or tick items yourself:

- **Maximum** frees everything that is not answering a request right now, plus the file cache.
- **Recommended** unloads unused Ollama models and stops model servers idle for 15 minutes or more. It needs no password.

Anything that handled a request in the last minute, or is answering one now, is marked **In use** and left out of both presets. When it finishes, Optimize shows memory before and after and the result of each step. To free even more, tick apps under **Other apps using memory**, or use **Select all apps that can quit**, and they close as part of the same run. Optimize never picks apps for you: the presets leave them alone, and you confirm the list of names before anything closes. Each app closes the way it does with ⌘Q, so it can still ask you to save your work; one that is waiting on a save dialog is reported as **Still open**. Menu bar utilities can be selected too. Parts of macOS (Finder, the Dock), background-only agents, command-line tools such as `node`, and ModelMatrix itself are shown with the reason and cannot be selected. If the browser showing the dashboard is selected, it closes last, a few seconds after the results appear.

From the terminal: `modelmatrixctl optimize` (Maximum), `--recommended`, `--no-purge` to skip the password step, or `--dry-run` to see the plan without changing anything.

---

## Models from Hugging Face

Type a model name from Hugging Face and ModelMatrix does the rest: it checks the model first, tells you what it is and whether it will run, downloads the version you choose (resumable), creates the profile, starts it and sends it a test message. It works the same from the web dashboard, the menu bar and the terminal, on any supported Mac and on your Linux hosts.

```bash
modelmatrixctl check bartowski/Qwen2.5-7B-Instruct-GGUF     # what is it, will it run, which version?
modelmatrixctl pull bartowski/Qwen2.5-7B-Instruct-GGUF --wait
modelmatrixctl models                                        # downloaded models and their profiles
```

In the dashboard, open **Models & Servers → Add from Hugging Face**. In the menu bar, press the download arrow in the header.

### 1. Check before downloading

You can type `owner/name`, `owner/name:VERSION`, a `huggingface.co` link (including a link to one file), or search words. Nothing is downloaded or installed by a check. It reports:

- **Format**: GGUF, MLX, MLC-LLM, original weights, or a GPU-server format such as AWQ, GPTQ or EXL2. For formats ModelMatrix can't run, it lists ready-to-run GGUF (and, on Apple Silicon, MLX) versions of the same model, with plain conversions before modified ones.
- **Type and licence**: chat, vision chat, or not a chat model (speech, image generation, embeddings), the licence, and whether Hugging Face needs a login to download it.
- **The machine**: processor, memory, how much of it a model may use (measured from the GPU on Apple Silicon, processor-only on Intel Macs, NVIDIA/AMD/Vulkan GPU or processor on Linux), and free disk space.
- **Every version** with its download size, quality, estimated memory at your context length, and whether it fits well, fits tightly or is too big. Large models split into several files count as one version, and image adapters (`mmproj`) for vision models are included in the size.
- **A recommendation**: the best quality that fits comfortably, at the longest context that still fits. Versions under 3.5 bits lose a lot of quality, so they are never recommended automatically; you can still pick one yourself.
- **The engine it needs** (llama.cpp for GGUF, mlx-lm for MLX on Apple Silicon, Ollama as a backup), whether it is installed, which version and where.

The check reads the model's details from Hugging Face and at most the first few megabytes of one GGUF file (its settings, not the model). Memory figures are estimates.

**Suggestions do not block downloads.** A low-quality version that fits is labelled as a quality suggestion, not “too big”. You may select any supported version and confirm **Download anyway** when warned about quality loss or memory requirements. Downloading a version that exceeds the estimated memory budget does not guarantee that it will start or run quickly. Login/license access, unsupported files and insufficient disk space are still enforced. If there is no automatic recommendation, the CLI requires an explicit choice: `modelmatrixctl download owner/name:VERSION`.

### 2. Engines are installed only when you approve

If the engine a model needs is missing, ModelMatrix says so and offers to install it. Before anything runs you see the exact plan: the command (for example `brew install llama.cpp`) or the official download with its SHA-256 checksum, where it goes, how much space it takes, and how to remove it. Nothing is installed until you press **Approve** (or answer `y` in the terminal). An approval is used once and expires after 10 minutes.

| Engine | How ModelMatrix can install it |
|---|---|
| llama.cpp | Homebrew (`brew install llama.cpp`) if you have Homebrew, or the official llama.cpp release for your Mac or Linux host (checksum verified) into `~/ModelMatrix/Engines/llama.cpp` |
| mlx-lm (Apple Silicon) | Homebrew, or a private Python made by the official `uv` tool into `~/ModelMatrix/Engines/mlx-lm` (nothing is added to your shell or system Python) |
| Ollama | Homebrew or the official app; on Linux hosts it shows the official command for you to run yourself |

Engines already on the machine are found automatically (Homebrew, MacPorts, pip, pipx, uv, conda, `~/.local/bin`, Ollama) or at a path you set in Settings. ModelMatrix only updates or removes engines it installed itself. From the terminal: `modelmatrixctl engines`, `modelmatrixctl engine install llama.cpp [--method homebrew|official]`.

### 3. Resumable downloads

Downloads continue where they stopped after a network drop, sleep, quitting the app or a restart. Large files download in several pieces at once, every file is checked against Hugging Face's SHA-256, and files you already have are reused instead of downloaded again. Pause, resume or cancel from the **Downloads** list in either interface, or with `modelmatrixctl downloads pause|resume|cancel <id>`. ModelMatrix checks free disk space first, keeps the Mac awake while downloading (optional), and shows a notification when a model is ready or needs your attention.

Models go to `~/ModelMatrix/Models/<owner>/<name>` unless you choose another folder. Settings (dashboard **Settings → Hugging Face**, or `modelmatrixctl hf-settings`) cover the models folder, a Hugging Face mirror for regions where the site is slow or blocked, connections per file, a speed limit, starting after download, and keeping the Mac awake. System proxy settings are used automatically.

### 4. Profiles are made for you

When a download finishes, ModelMatrix creates a profile with sensible settings for the machine (context length, GPU or processor use, a free port), starts it, sends a short test message, and shows the model name and address to use. If the engine is still missing, the model waits and starts by itself once you install it. If there isn't enough free memory, it says so and suggests Optimize. If it doesn't start, it shows the reason from the log and a suggested fix, with **Start and test** to try again. Requests go through the usage relay like any other profile.

**Downloaded models** lists every model with its disk use and profile. Deleting one removes its profile, and optionally its files. Models you already had in Ollama before ModelMatrix pulled them are never deleted from Ollama.

### Gated models

Some models (for example Meta's Llama) need you to accept their terms on huggingface.co and sign in. Create a read token at huggingface.co/settings/tokens and save it with **Settings → Hugging Face → Token** or `modelmatrixctl hf-token set`. It is stored in the macOS Keychain and sent only to Hugging Face.

### Remote Linux hosts

Choose a remote host in the check (`--host NAME` in the terminal) and the model downloads directly onto that host from Hugging Face, using the llama.cpp build that matches its GPU (CUDA, Vulkan or processor-only). Engine installs on a host follow the same approve-first plan. The host's models, downloads and profiles appear next to your Mac's.

---

## Runtimes supported

ModelMatrix keys off the runtime. Any backend with an OpenAI-compatible endpoint can be health-checked, switched and tagged.

| Support level | Runtimes and providers |
|---|---|
| **Native command adapters** | `llama.cpp`, MLX / `mlx_lm.server`, `rVLLM MLX`, `vLLM-MLX`, `Ollama`, `vLLM`, `SGLang`, Hugging Face `TGI`, `llama-cpp-python` |
| **Named launcher profiles** | `DDTree MLX`, `TurboQuant`, `oMLX`, `Mistral.rs`, `MLC-LLM`, `LightLLM`, `FastChat`, `OpenLLM`, `Nexa`, `ExLlamaV2`, `Aphrodite`, `LMDeploy`, `MLX Omni Server`, `MLX OpenAI Server`, `MLX Serve`, `text-generation-webui`, `KoboldCpp`, `TabbyAPI`, `llamafile` |
| **External endpoints** | `LM Studio`, `Jan`, `LocalAI`, `LiteLLM`, `ollmlx`, Triton-backed servers, or any OpenAI-compatible base URL |

Use `RUNTIME_TAGS` for traits such as `coding`, `q8`, `long-context`, `vision` or `agentic`. The full table is in [Controller/RUNTIME_SUPPORT.md](Controller/RUNTIME_SUPPORT.md).

---

## Remote hosts

| Item | Summary |
|---|---|
| Pair, install, update | Paste a `modelmatrix-gateway://` pairing link, or enter `user@host` and choose **Install agent**. **Update agent** pushes the bundled agent over SSH. Available in the dashboard, Remote Hosts and Settings. |
| SSH (recommended) | The agent listens on loopback only; the app keeps a `BatchMode` tunnel using your SSH config, keys and agent (never passwords). |
| Tailscale / direct | `--tailscale` binds only the tailnet address. Plain-LAN binds need `--unsafe-bind` plus a bearer token of at least 16 bytes, kept in the Keychain. |
| Profiles | Remote default folder `~/model-profiles/`; templates for vLLM, llama.cpp, SGLang and TGI, or use `START_COMMAND`. |

See [RemoteAgent/README.md](RemoteAgent/README.md) and [SETUP.md](SETUP.md#remote-gateways).

---

## Requirements

- **macOS 14** (Sonoma) or later
- **Apple Silicon recommended.** Intel Macs run the app, but MLX runtimes need Apple Silicon
- A model runtime and weights on the machine that will serve them. ModelMatrix can download models from Hugging Face and, after you approve, install llama.cpp, mlx-lm or Ollama for you
- Remote hosts: Linux with Python 3.10 or later (x86_64 or arm64)
- To build from source: Xcode 16+, [XcodeGen](https://github.com/yonaskolb/XcodeGen), and Node.js 20+ with pnpm or npm for the web dashboard

---

## Install

**DMG.** Signed and notarized DMGs are published under **[Releases](https://github.com/Hackstar1337/ModelMatrix/releases/latest)** once release signing is configured (see `Scripts/build-dmg.sh` and `Scripts/sign-and-notarize-dmg.sh`). Until then, install from source.

**From source.**

```bash
git clone https://github.com/Hackstar1337/ModelMatrix.git
cd ModelMatrix
./Scripts/install.sh
```

The installer builds the app (including the web dashboard), places it in `~/Applications/`, installs `modelmatrixctl` (status, start, stop, switch, usage, optimize and more) to `~/.local/bin` with bash/zsh/fish completions, and registers the app with Launch Services and Spotlight. Run `./Scripts/install.sh --help` for quiet mode, custom paths, `--verify` and `--skip-open`.

**Uninstall.** `./Scripts/uninstall.sh` removes the app, its controller, `modelmatrixctl` and the completions but keeps your profiles and usage history; add `--purge` to delete those too.

`modelmatrixctl capabilities`, `robot-docs guide`, `triage` and `doctor --json` give agents machine-readable contracts and diagnostics. Mutating commands (`start`, `stop`, `restart`, `switch`/`activate`, `stop-all`) accept `--dry-run` and `--json`.

---

## Quickstart

**1. Open the menu bar icon.** The first launch registers the bundled controller. If macOS asks, allow ModelMatrix in **System Settings → General → Login Items & Extensions**.

**2. Add a profile.** In the menu bar choose **Profiles → New Profile**, or in the dashboard **Models & Servers → New profile**, and start from a template. Profiles are plain files in:

```text
~/Library/Application Support/ModelMatrix/Controller/model-profiles
```

A minimal `llama.cpp` profile:

```env
DISPLAY_NAME=Qwen3 8B
RUNTIME=llama.cpp
MODEL_PATH=/path/to/model.gguf
PORT=8080
REQUEST_MODEL=qwen3-8b
SERVER_MODEL_ID=qwen3-8b
```

An Ollama model is even shorter:

```env
DISPLAY_NAME=Gemma 3
RUNTIME=ollama
REQUEST_MODEL=gemma3:4b
BASE_URL=http://127.0.0.1:11434/v1
```

**3. Activate it.** Every other model stops and the one you picked comes up. Each profile must use a unique endpoint; the doctor flags clashes.

**4. Watch it work.** Point your apps at `http://127.0.0.1:7701/v1` and open **http://localhost:7700**.

<details>
<summary><strong>Let an AI agent write profiles for the runtimes you have</strong></summary>

<br/>

```text
I use ModelMatrix on my Mac. Its controller is already running inside the app.
Create working model profiles for the runtimes actually installed on this machine.
Rules:
- macOS only.
- Do not hardcode Homebrew, repo-local or personal paths unless you verify they exist here.
- Save profiles in ~/Library/Application Support/ModelMatrix/Controller/model-profiles; copy from its examples/ folder and leave examples/ untouched.
- Prefer MODEL_PATH (or MODEL_FILE + MODEL_ROOT) for llama.cpp, MODEL_DIR or MODEL_REPO for MLX, SERVER_BIN when a binary is not on PATH, RUNTIME + START_COMMAND for other launchers.
- .env profiles are key/value files, not shell scripts; use JSON for structured values.
- Follow the profile format and controller API in SETUP.md.
- Verify each profile starts, reports ready and stops cleanly. Ask me only what you cannot discover.
```

</details>

---

## Troubleshooting

| Label | Where | Meaning |
|---|---|---|
| `RUNNING` / `READY` | Profile badge | Process is running / its health probe answered |
| `STARTING` / `STOPPING` / `RESTARTING` / `ACTIVATING` | Profile badge | An action is in progress |
| `STALE` | Footer | Last successful refresh is older than about 45 seconds |
| `CACHED` | Footer | The controller was briefly unreachable; last known status is shown |
| `ERROR` | Footer | The latest refresh or action failed |
| Dashboard says *Controller unreachable* | Browser | The app is not running, or the controller was stopped; open ModelMatrix from Applications |
| No requests in Usage | Both | Clients are calling the model port directly; use the relay URL or turn on capture mode |
| A `START_COMMAND` profile never becomes ready | Profile log | macOS privacy blocks background processes from reading `~/Documents`, `~/Desktop` and `~/Downloads`. Keep launch scripts and weights elsewhere (for example `~/models`), or give the runtime Full Disk Access |

More in [SETUP.md](SETUP.md#troubleshooting).

---

## Integrations

- **Raycast**: scripts in [`Integrations/Raycast/`](Integrations/Raycast/) call the same controller API.
- **SwiftBar**: plugin at [`Controller/swiftbar/local-models.15s.sh`](Controller/swiftbar/local-models.15s.sh).
- **Factory Droid**: `Sync Droid` pushes managed profiles into Droid's custom models.

---

## Documentation

- **[DASHBOARD.md](DASHBOARD.md)**: web dashboard pages, usage relay, capture mode, security and API routes
- **[SETUP.md](SETUP.md)**: profile formats, runtimes, health checks, controller API, building, releasing, troubleshooting
- **[Controller/RUNTIME_SUPPORT.md](Controller/RUNTIME_SUPPORT.md)**: runtime table, launch modes, templates, readiness modes
- **[RemoteAgent/README.md](RemoteAgent/README.md)**: remote agent install, security and API
- **[CHANGELOG.md](CHANGELOG.md)**: release notes

---

## Development

```bash
swift test                    # Swift tests
python3 -m unittest discover -s RemoteAgent/tests   # agent tests
./Scripts/check-cycles        # dependency cycle check
./Scripts/build-app.sh        # dist/ModelMatrix.app (includes the dashboard)
cd Dashboard && pnpm dev      # dashboard with hot reload, proxied to localhost:7700
```

Release prep is scripted:

```bash
./Scripts/bump-version patch   # or minor / major / x.y.z
./Scripts/release-preflight.sh
```

Contributions are welcome. Keep the app generic (runtime-specific behaviour belongs in the controller or a profile), keep controller API changes additive, and include a runnable example with any new adapter.

---

## License

[MIT](LICENSE) © 2026 Hackstar1337


## Windows distribution

The Windows port reuses this Dashboard and shared RemoteAgent through a per-user Python controller and optional native tray. Python 3.10+ is required; Node is only needed to rebuild the included static Dashboard. From the complete Windows source directory:

```powershell
.\Scripts\install-windows.cmd -NoBuild
.\Scripts\start-windows.cmd -NoBrowser
.\Scripts\verify-windows.cmd
```

Open **http://127.0.0.1:7700/**. Follow the [step-by-step Windows setup guide](Windows/README.md) for model profiles, execution, authentication, build/package, and troubleshooting. See the [change summary](Windows/COMPATIBILITY_CHANGES.md) and [test report](Windows/TEST_REPORT.md) for the source changes and exact verification scope.

Core functionality was tested natively on Windows 11 x64, including real llama.cpp GGUF inference, capture, usage, protected client endpoints, installation and graceful shutdown. Windows 10 is targeted but was not tested on a Windows 10 device. The original Mac application and Linux agent path are retained; Apple-only MLX/Metal and macOS internals do not become native Windows components. Windows CI is included, not claimed remotely executed. No Bash, Xcode, administrator rights, global Python/Node installation or automatic persistence is needed for the Windows path.
