#!/usr/bin/env bash
# Local model launcher for profile-backed runtimes on macOS.
set -euo pipefail

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE_DIR="$WORK_DIR/model-profiles"
MODEL_PROFILE="${MODEL_PROFILE:-}"
PROFILE_PATH="${MODEL_PROFILE_PATH:-}"
AGL_VENV="$WORK_DIR/.venv-agentlightning"
AGL_PYTHON="$AGL_VENV/bin/python"
AGL_UV_CACHE="${UV_CACHE_DIR:-/tmp/modelmatrix-uv-cache}"
AGL_PYTHON_VERSION="${AGL_PYTHON_VERSION:-3.12}"

log() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
die() { printf '[ERR] %s\n' "$*"; exit 1; }

expand_user_path() {
    local path="$1"
    case "$path" in
        "~")
            printf '%s\n' "$HOME"
            ;;
        ~/*)
            printf '%s/%s\n' "$HOME" "${path#~/}"
            ;;
        *)
            printf '%s\n' "$path"
            ;;
    esac
}

detect_model_root() {
    local candidate
    for candidate in "${MODEL_ROOT:-}" "${MODEL_ROOT_HINT:-}"; do
        if [ -n "$candidate" ]; then
            expand_user_path "$candidate"
            return 0
        fi
    done
    return 1
}

if [ -z "$PROFILE_PATH" ] && [ -z "$MODEL_PROFILE" ]; then
    die "Set MODEL_PROFILE or MODEL_PROFILE_PATH before launching a profile"
fi

if [ -z "$PROFILE_PATH" ]; then
    if [ -f "$PROFILE_DIR/${MODEL_PROFILE}.env" ]; then
        PROFILE_PATH="$PROFILE_DIR/${MODEL_PROFILE}.env"
    elif [ -f "$PROFILE_DIR/${MODEL_PROFILE}.json" ]; then
        PROFILE_PATH="$PROFILE_DIR/${MODEL_PROFILE}.json"
    else
        die "Unknown MODEL_PROFILE=$MODEL_PROFILE (expected $PROFILE_DIR/${MODEL_PROFILE}.env or .json)"
    fi
fi

if [ -z "$MODEL_PROFILE" ]; then
    MODEL_PROFILE="$(basename "$PROFILE_PATH")"
    MODEL_PROFILE="${MODEL_PROFILE%.*}"
fi

controller_bin() {
    local bin="${MODELMATRIX_CONTROLLER_BIN:-$WORK_DIR/bin/ModelMatrixController}"
    if [ ! -x "$bin" ]; then
        die "Native controller not found: $bin"
    fi
    printf '%s\n' "$bin"
}

load_profile_exports() {
    local path="$1"
    local exports
    if ! exports="$("$(controller_bin)" profile-exports --root "$WORK_DIR" --profile-file "$path")"; then
        die "Failed to load profile: $path"
    fi
    eval "$exports"
}

if [ "${MODELMATRIX_PROFILE_LOADED:-0}" != "1" ]; then
    load_profile_exports "$PROFILE_PATH"
fi

calc_threads() {
    local cpu_count
    cpu_count="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 8)"
    if [ "$cpu_count" -gt 10 ] 2>/dev/null; then
        echo 10
    else
        echo "$cpu_count"
    fi
}

resolve_llama_server() {
    local candidate
    for candidate in \
        "${SERVER_BIN:-}" \
        "${LLAMA_SERVER_BIN:-}" \
        "${LLAMA_CPP_SERVER_BIN:-}"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    if command -v llama-server >/dev/null 2>&1; then
        command -v llama-server
        return 0
    fi
    if command -v llama.cpp-server >/dev/null 2>&1; then
        command -v llama.cpp-server
        return 0
    fi
    return 1
}

resolve_mlx_server() {
    local candidate
    for candidate in \
        "${SERVER_BIN:-}" \
        "${MLX_SERVER_BIN:-}" \
        "${MLX_LM_SERVER_BIN:-}"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    if command -v mlx_lm.server >/dev/null 2>&1; then
        command -v mlx_lm.server
        return 0
    fi
    return 1
}

resolve_rvllm_mlx_server() {
    local candidate
    for candidate in \
        "${SERVER_BIN:-}" \
        "${RVLLM_MLX_SERVER_BIN:-}"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

resolve_vllm_mlx_server() {
    local candidate
    for candidate in \
        "${SERVER_BIN:-}" \
        "${VLLM_MLX_BIN:-}"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    if command -v vllm-mlx >/dev/null 2>&1; then
        command -v vllm-mlx
        return 0
    fi
    return 1
}

canonical_runtime() {
    local raw normalized
    raw="${1:-unknown}"
    normalized="$(printf '%s' "$raw" | tr '[:upper:]_' '[:lower:]-')"
    case "$normalized" in
        llamacpp|llama-cpp|llama.cpp)
            printf '%s\n' "llama.cpp"
            ;;
        mlx-lm)
            printf '%s\n' "mlx"
            ;;
        rvllm|rvllm-mlx)
            printf '%s\n' "rvllm-mlx"
            ;;
        vllm_mlx|vllm-mlx)
            printf '%s\n' "vllm-mlx"
            ;;
        ddtree|ddtree_mlx|ddtree-mlx)
            printf '%s\n' "ddtree-mlx"
            ;;
        mlx_vlm|mlx-vlm)
            printf '%s\n' "mlx-vlm"
            ;;
        mlx-omni|mlx-omni-server)
            printf '%s\n' "mlx-omni-server"
            ;;
        mlx-openai|mlx-openai-server)
            printf '%s\n' "mlx-openai-server"
            ;;
        mlx-llm-server)
            printf '%s\n' "mlx-llm-server"
            ;;
        mlx-serve)
            printf '%s\n' "mlx-serve"
            ;;
        mlx-engine|mlxengine)
            printf '%s\n' "mlxengine"
            ;;
        local-ai)
            printf '%s\n' "localai"
            ;;
        custom|command)
            printf '%s\n' "command"
            ;;
        openai|openai-compatible|endpoint|external)
            printf '%s\n' "external"
            ;;
        lmstudio|lm-studio)
            printf '%s\n' "lm-studio"
            ;;
        text-generation-inference|huggingface-tgi)
            printf '%s\n' "tgi"
            ;;
        oobabooga|text-generation-webui)
            printf '%s\n' "text-generation-webui"
            ;;
        kobold-cpp)
            printf '%s\n' "koboldcpp"
            ;;
        exllama|exllama-v2|exllamav2)
            printf '%s\n' "exllamav2"
            ;;
        aphrodite-engine)
            printf '%s\n' "aphrodite"
            ;;
        mistral-rs|mistralrs|mistral.rs)
            printf '%s\n' "mistral.rs"
            ;;
        mlc|mlc-llm)
            printf '%s\n' "mlc-llm"
            ;;
        fast-chat)
            printf '%s\n' "fastchat"
            ;;
        bentoml-openllm)
            printf '%s\n' "openllm"
            ;;
        nexa-sdk|nexaai)
            printf '%s\n' "nexa"
            ;;
        litellm-proxy)
            printf '%s\n' "litellm"
            ;;
        llama-swap|llamaswap)
            printf '%s\n' "llama-swap"
            ;;
        hf-transformers|huggingface-transformers)
            printf '%s\n' "transformers"
            ;;
        nvidia-triton)
            printf '%s\n' "triton"
            ;;
        tensorrtllm)
            printf '%s\n' "tensorrt-llm"
            ;;
        ort-genai)
            printf '%s\n' "onnxruntime-genai"
            ;;
        *)
            printf '%s\n' "$normalized"
            ;;
    esac
}

runtime_default_port() {
    case "$1" in
        ollama|ollmlx)
            printf '%s\n' "11434"
            ;;
        lm-studio|mistral.rs)
            printf '%s\n' "1234"
            ;;
        openllm)
            printf '%s\n' "3000"
            ;;
        litellm)
            printf '%s\n' "4000"
            ;;
        llama-swap)
            printf '%s\n' "8080"
            ;;
        sglang)
            printf '%s\n' "30000"
            ;;
        koboldcpp|text-generation-webui)
            printf '%s\n' "5001"
            ;;
        tabbyapi)
            printf '%s\n' "5000"
            ;;
        *)
            printf '%s\n' "8080"
            ;;
    esac
}

resolve_first_executable() {
    local candidate
    for candidate in "$@"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
        if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

append_json_args() {
    local raw_json="${1:-}"
    local parsed
    local arg
    [ -n "$raw_json" ] || return 0
    if ! parsed="$(printf '%s' "$raw_json" | "$(controller_bin)" json-strings)"; then
        die "SERVER_ARGS_JSON must be a JSON array"
    fi
    while IFS= read -r arg; do
        cmd+=("$arg")
    done <<EOF
$parsed
EOF
}

model_source_for_adapter() {
    local source
    for source in "${MODEL_DIR:-}" "${MODEL_PATH:-}"; do
        if [ -n "$source" ]; then
            printf '%s\n' "$source"
            return 0
        fi
    done
    if [ -n "${MODEL_FILE:-}" ]; then
        local root
        root="$(detect_model_root || true)"
        [ -n "$root" ] || return 1
        printf '%s/%s\n' "$root" "$MODEL_FILE"
        return 0
    fi
    for source in "${MODEL_ID:-}" "${MODEL_REPO:-}"; do
        if [ -n "$source" ]; then
            printf '%s\n' "$source"
            return 0
        fi
    done
    return 1
}

spawn_detached() {
    local log_path="$1"
    local pid_path="$2"
    shift 2
    local workdir="${WORKING_DIRECTORY:-${WORKDIR:-}}"
    local pid
    (
        if [ -n "$workdir" ]; then
            cd "$workdir" || exit 1
        fi
        if command -v setsid >/dev/null 2>&1; then
            exec setsid "$@" </dev/null >>"$log_path" 2>&1
        else
            exec "$@" </dev/null >>"$log_path" 2>&1
        fi
    ) &
    pid=$!
    printf '%s\n' "$pid" >"$pid_path"
    printf '%s\n' "$pid"
}

# Set while waiting for a server this script just started, so a server
# that exits during startup is reported at once instead of at the timeout.
WATCH_PID=""
server_exited() {
    [ -n "$WATCH_PID" ] || return 1
    kill -0 "$WATCH_PID" 2>/dev/null && return 1
    warn "The model server exited while starting"
    return 0
}

# llama.cpp renames and re-types options between versions (for example
# --reasoning none -> off, --flash-attn 1 -> on, --mmap -> --load-mode).
# The option list of the installed llama-server is read from its --help,
# cached per build, and each version-dependent option is matched to it.
# When the list can't be read, options are passed as written.
LLAMA_OPTIONS=""
load_llama_options() {
    local bin="$1" stamp key cache pid tries=0
    stamp="$(stat -L -f '%m %z' "$bin" 2>/dev/null || stat -L -c '%Y %s' "$bin" 2>/dev/null || true)"
    key="$(printf '%s %s' "$bin" "$stamp" | cksum | awk '{print $1}')"
    cache="$RUN_DIR/llama-options-$key.txt"
    if [ ! -s "$cache" ]; then
        "$bin" --help </dev/null >"$cache.tmp" 2>&1 &
        pid=$!
        while kill -0 "$pid" 2>/dev/null && [ "$tries" -lt 60 ]; do
            sleep 0.25
            tries=$((tries + 1))
        done
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        if grep -q -- '--port' "$cache.tmp" 2>/dev/null; then
            # Keep only the option column of each option line.
            awk '/^-/ { if (length($0) > 40 && substr($0, 39, 2) != "  ") print $0; else print substr($0, 1, 40) }' \
                "$cache.tmp" >"$cache"
        fi
        rm -f "$cache.tmp"
    fi
    LLAMA_OPTIONS="$(cat "$cache" 2>/dev/null || true)"
    [ -n "$LLAMA_OPTIONS" ] || warn "Could not read the options of $bin; passing the profile's options as written"
}

llama_has() {
    [ -n "$LLAMA_OPTIONS" ] || return 0
    grep -E -q -- "(^|[ ,])$1([ ,=]|\$)" <<<"$LLAMA_OPTIONS"
}

llama_takes_value() {
    grep -E -q -- "(^|[ ,])$1 [^ ,-]" <<<"$LLAMA_OPTIONS"
}

# Accepted values such as "on|off|auto", or nothing.
llama_choices() {
    local line
    line="$(grep -E -m 1 -- "(^|[ ,])$1 \\[" <<<"$LLAMA_OPTIONS" || true)"
    [ -n "$line" ] || return 0
    line="${line#*"$1 ["}"
    printf '%s\n' "${line%%]*}"
}

llama_choice_ok() {
    case "|$1|" in *"|$2|"*) return 0 ;; esac
    return 1
}

# Adds FLAG VALUE to cmd in the form the installed llama-server accepts.
llama_value_arg() {
    local flag="$1" value="$2" choices wanted
    [ -n "$value" ] || return 0
    if ! llama_has "$flag"; then
        warn "Leaving out $flag: this llama-server version doesn't have it"
        return 0
    fi
    if [ -n "$LLAMA_OPTIONS" ] && ! llama_takes_value "$flag"; then
        # A plain on/off switch in this version.
        case "$value" in 0|off|false|no|none|disabled) return 0 ;; esac
        cmd+=("$flag")
        return 0
    fi
    choices="$(llama_choices "$flag")"
    if [ -n "$choices" ] && ! llama_choice_ok "$choices" "$value"; then
        case "$value" in
            1|on|true|yes|enabled) wanted=on ;;
            0|off|false|no|none|disabled) wanted=off ;;
            *) wanted="" ;;
        esac
        if [ -n "$wanted" ] && llama_choice_ok "$choices" "$wanted"; then
            value="$wanted"
        else
            warn "Leaving out $flag $value: this llama-server accepts $choices"
            return 0
        fi
    fi
    cmd+=("$flag" "$value")
}

llama_switch() {
    if llama_has "$1"; then
        cmd+=("$1")
    else
        warn "Leaving out $1: this llama-server version doesn't have it"
    fi
}

# USE_MMAP / USE_MLOCK as --load-mode (newer builds) or --mmap/--no-mmap/--mlock.
llama_load_mode() {
    local mmap="$1" mlock="$2" mode=""
    if [ -n "$LLAMA_OPTIONS" ] && llama_has --load-mode; then
        case "$mmap:$mlock" in
            1:1) mode="mmap+mlock" ;;
            0:1) mode="mlock" ;;
            0:*) mode="none" ;;
        esac
        [ -z "$mode" ] || cmd+=(--load-mode "$mode")
        return 0
    fi
    if [ "$mmap" = "1" ]; then
        if [ -z "$LLAMA_OPTIONS" ] || llama_has --mmap; then cmd+=(--mmap); fi
    else
        llama_switch --no-mmap
    fi
    if [ "$mlock" = "1" ]; then llama_switch --mlock; fi
}

wait_for_models_endpoint() {
    local models_url="$1"
    local expected_id="$2"
    local retries="${3:-60}"
    local response
    while [ "$retries" -gt 0 ]; do
        if response="$(curl -fsS "$models_url" 2>/dev/null)"; then
            if [ -n "$expected_id" ] && ! printf '%s' "$response" | "$(controller_bin)" openai-models-contains --id "$expected_id"; then
                sleep 2
                retries=$((retries - 1))
                continue
            fi
            return 0
        fi
        if server_exited; then
            return 1
        fi
        sleep 2
        retries=$((retries - 1))
    done
    return 1
}

wait_for_http_200() {
    local url="$1"
    local retries="${2:-60}"
    while [ "$retries" -gt 0 ]; do
        if curl -fsS "$url" >/dev/null 2>&1; then
            return 0
        fi
        if server_exited; then
            return 1
        fi
        sleep 2
        retries=$((retries - 1))
    done
    return 1
}

wait_for_profile_ready() {
    local mode="$1"
    local url="$2"
    local expected_id="$3"
    local retries="${4:-60}"
    case "$mode" in
        disabled)
            return 0
            ;;
        http-200)
            wait_for_http_200 "$url" "$retries"
            ;;
        openai-models|*)
            wait_for_models_endpoint "$url" "$expected_id" "$retries"
            ;;
    esac
}

install_agentlightning() {
    if [ -x "$AGL_PYTHON" ] && "$AGL_PYTHON" -c "import agentlightning" >/dev/null 2>&1; then
        log "Agent Lightning already installed"
        return 0
    fi

    mkdir -p "$AGL_UV_CACHE"
    if command -v uv >/dev/null 2>&1; then
        if [ ! -x "$AGL_PYTHON" ]; then
            UV_CACHE_DIR="$AGL_UV_CACHE" uv venv --python "$AGL_PYTHON_VERSION" "$AGL_VENV" >/dev/null
        fi
        if UV_CACHE_DIR="$AGL_UV_CACHE" uv pip install --python "$AGL_PYTHON" agentlightning >/dev/null; then
            log "Installed Agent Lightning in $AGL_VENV"
            return 0
        fi
    fi
    warn "Agent Lightning install skipped"
}

RUNTIME="$(canonical_runtime "${RUNTIME:-unknown}")"
REQUEST_MODEL="${REQUEST_MODEL:-${SERVER_MODEL_ID:-$MODEL_PROFILE}}"
SERVER_MODEL_ID="${SERVER_MODEL_ID:-$REQUEST_MODEL}"
MODEL_ALIAS="${MODEL_ALIAS:-$REQUEST_MODEL}"
LOG_ALIAS="${LOG_ALIAS:-$MODEL_PROFILE}"
CONTEXT_SIZE="${CONTEXT_SIZE:-4096}"
N_PARALLEL="${N_PARALLEL:-1}"
GPU_LAYERS="${GPU_LAYERS:-999}"
BATCH_SIZE="${BATCH_SIZE:-512}"
UBATCH_SIZE="${UBATCH_SIZE:-512}"
CACHE_TYPE_K="${CACHE_TYPE_K:-f16}"
CACHE_TYPE_V="${CACHE_TYPE_V:-f16}"
FLASH_ATTN="${FLASH_ATTN:-1}"
REASONING="${REASONING:-none}"
FIT="${FIT:-0}"
FIT_TARGET="${FIT_TARGET:-0}"
FIT_CTX="${FIT_CTX:-0}"
TEMP="${TEMP:-0.7}"
TOP_P="${TOP_P:-0.95}"
MAX_TOKENS="${MAX_TOKENS:-2048}"
PROMPT_CACHE_SIZE="${PROMPT_CACHE_SIZE:-4096}"
PROMPT_CACHE_BYTES="${PROMPT_CACHE_BYTES:-0}"
PROMPT_CONCURRENCY="${PROMPT_CONCURRENCY:-1}"
DECODE_CONCURRENCY="${DECODE_CONCURRENCY:-1}"
PREFILL_STEP_SIZE="${PREFILL_STEP_SIZE:-512}"
CHAT_TEMPLATE_ARGS="${CHAT_TEMPLATE_ARGS:-}"
if [ -z "$CHAT_TEMPLATE_ARGS" ]; then
    CHAT_TEMPLATE_ARGS="{}"
fi
SERVER_ARGS_JSON="${SERVER_ARGS_JSON:-}"
MAX_REQUEST_TOKENS="${MAX_REQUEST_TOKENS:-$MAX_TOKENS}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
CACHE_MEMORY_PERCENT="${CACHE_MEMORY_PERCENT:-}"
CHAT_TEMPLATE_KWARGS="${CHAT_TEMPLATE_KWARGS:-}"
ENABLE_TOOL_CALLS="${ENABLE_TOOL_CALLS:-${ENABLE_AUTO_TOOL_CHOICE:-0}}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen}"
REASONING_PARSER="${REASONING_PARSER:-}"
CONTINUOUS_BATCHING="${CONTINUOUS_BATCHING:-0}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-4}"
PREFILL_BATCH_SIZE="${PREFILL_BATCH_SIZE:-4}"
COMPLETION_BATCH_SIZE="${COMPLETION_BATCH_SIZE:-8}"
THREADS="${THREADS:-$(calc_threads)}"
THREADS_BATCH="${THREADS_BATCH:-$THREADS}"
HOST="${HOST:-127.0.0.1}"
if [ -z "${PORT:-}" ] && [ -z "${BASE_URL:-}" ]; then
    PORT="$(runtime_default_port "$RUNTIME")"
fi
PORT="${PORT:-}"
BASE_URL="${BASE_URL:-http://${HOST}:${PORT}/v1}"
BASE_URL="${BASE_URL%/}"
MODELS_URL="${MODEL_LIST_URL:-$BASE_URL/models}"
HEALTHCHECK_MODE="${HEALTHCHECK_MODE:-openai-models}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"
if [ -z "$HEALTHCHECK_URL" ]; then
    if [ "$HEALTHCHECK_MODE" = "openai-models" ]; then
        HEALTHCHECK_URL="$MODELS_URL"
    else
        HEALTHCHECK_URL="$BASE_URL"
    fi
fi
RUN_DIR="$WORK_DIR/run"
mkdir -p "$RUN_DIR"
LOG_ALIAS_SAFE="$(printf '%s' "$LOG_ALIAS" | tr -c '[:alnum:]_.-' '_')"
LOG_PATH="/tmp/${LOG_ALIAS_SAFE}.log"
PID_PATH="$RUN_DIR/${MODEL_PROFILE}.pid"

if [ "$RUNTIME" = "llama.cpp" ]; then
    MODEL_PATH="${MODEL_PATH:-}"
    if [ -z "$MODEL_PATH" ]; then
        [ -n "${MODEL_FILE:-}" ] || die "MODEL_PATH or MODEL_FILE is required for $MODEL_PROFILE"
        MODEL_ROOT_RESOLVED="$(detect_model_root || true)"
        [ -n "$MODEL_ROOT_RESOLVED" ] || die "MODEL_FILE requires MODEL_ROOT or MODEL_ROOT_HINT for $MODEL_PROFILE"
        MODEL_PATH="$MODEL_ROOT_RESOLVED/$MODEL_FILE"
    fi
    if [ ! -f "$MODEL_PATH" ]; then
        die "Model file not found: $MODEL_PATH"
    fi
    LLAMA_SERVER="$(resolve_llama_server)" || die 'llama-server not found; set SERVER_BIN or LLAMA_SERVER_BIN in the profile, or add llama-server to PATH'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "$SERVER_MODEL_ID" 1; then
        log "Model server already responding on $HEALTHCHECK_URL"
    else
        log "Starting llama.cpp profile $MODEL_PROFILE"
        cmd=(
            "$LLAMA_SERVER"
            --model "$MODEL_PATH"
            --alias "$MODEL_ALIAS"
            --host "$HOST"
            --port "$PORT"
            --ctx-size "$CONTEXT_SIZE"
            --parallel "$N_PARALLEL"
            --threads "$THREADS"
            --threads-batch "$THREADS_BATCH"
            --n-gpu-layers "$GPU_LAYERS"
            --batch-size "$BATCH_SIZE"
            --ubatch-size "$UBATCH_SIZE"
            --cache-type-k "$CACHE_TYPE_K"
            --cache-type-v "$CACHE_TYPE_V"
        )
        # Options whose name or values differ between llama.cpp versions.
        load_llama_options "$LLAMA_SERVER"
        llama_value_arg --flash-attn "$FLASH_ATTN"
        llama_value_arg --reasoning "$REASONING"
        llama_value_arg --fit "$FIT"
        llama_value_arg --fit-target "$FIT_TARGET"
        llama_value_arg --fit-ctx "$FIT_CTX"
        llama_switch --metrics
        llama_switch --cont-batching
        llama_value_arg --reasoning-format "${REASONING_FORMAT:-}"
        llama_value_arg --reasoning-budget "${REASONING_BUDGET:-}"
        llama_value_arg --chat-template-kwargs "${CHAT_TEMPLATE_KWARGS:-}"
        llama_value_arg --cache-ram "${CACHE_RAM:-}"
        llama_load_mode "${USE_MMAP:-1}" "${USE_MLOCK:-0}"
        append_json_args "$SERVER_ARGS_JSON"
        GGML_METAL_N_CB="${METAL_N_CB:-1}" spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        WATCH_PID="$(cat "$PID_PATH" 2>/dev/null || true)"
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "$SERVER_MODEL_ID" 90; then
            tail -n 80 "$LOG_PATH" || true
            die "llama.cpp server failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "mlx" ]; then
    MODEL_SOURCE="${MODEL_DIR:-}"
    if [ -z "$MODEL_SOURCE" ] || [ ! -d "$MODEL_SOURCE" ]; then
        MODEL_SOURCE="${MODEL_REPO:-}"
    fi
    [ -n "$MODEL_SOURCE" ] || die "No MODEL_DIR or MODEL_REPO configured for $MODEL_PROFILE"
    MLX_SERVER="$(resolve_mlx_server)" || die 'mlx_lm.server not found; set SERVER_BIN or MLX_SERVER_BIN in the profile, or add mlx_lm.server to PATH'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "$SERVER_MODEL_ID" 1; then
        log "MLX server already responding on $HEALTHCHECK_URL"
    else
        log "Starting MLX profile $MODEL_PROFILE"
        cmd=(
            "$MLX_SERVER"
            --model "$MODEL_SOURCE"
            --host "$HOST"
            --port "$PORT"
            --temp "$TEMP"
            --top-p "$TOP_P"
            --max-tokens "$MAX_TOKENS"
            --prompt-cache-size "$PROMPT_CACHE_SIZE"
            --prompt-cache-bytes "$PROMPT_CACHE_BYTES"
            --prompt-concurrency "$PROMPT_CONCURRENCY"
            --decode-concurrency "$DECODE_CONCURRENCY"
            --prefill-step-size "$PREFILL_STEP_SIZE"
            --chat-template-args "$CHAT_TEMPLATE_ARGS"
        )
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        WATCH_PID="$(cat "$PID_PATH" 2>/dev/null || true)"
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "$SERVER_MODEL_ID" 180; then
            tail -n 120 "$LOG_PATH" || true
            die "MLX server failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "rvllm-mlx" ]; then
    MODEL_SOURCE="${MODEL_DIR:-}"
    [ -n "$MODEL_SOURCE" ] || die "MODEL_DIR is required for rvllm-mlx profiles"
    [ -d "$MODEL_SOURCE" ] || die "MODEL_DIR not found: $MODEL_SOURCE"
    RVLLM_MLX_SERVER="$(resolve_rvllm_mlx_server)" || die 'rvllm-mlx server not found; set SERVER_BIN in the profile'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "$SERVER_MODEL_ID" 1; then
        log "rvllm-mlx server already responding on $HEALTHCHECK_URL"
    else
        log "Starting rvllm-mlx profile $MODEL_PROFILE"
        cmd=(
            "$RVLLM_MLX_SERVER"
            --model-dir "$MODEL_SOURCE"
            --host "$HOST"
            --port "$PORT"
        )
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "$SERVER_MODEL_ID" 180; then
            tail -n 120 "$LOG_PATH" || true
            die "rvllm-mlx server failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "vllm-mlx" ]; then
    MODEL_SOURCE="$(model_source_for_adapter || true)"
    [ -n "$MODEL_SOURCE" ] || die "MODEL_REPO, MODEL_ID, MODEL_DIR, MODEL_PATH, or MODEL_FILE is required for vLLM-MLX profiles"
    VLLM_MLX_SERVER="$(resolve_vllm_mlx_server)" || die 'vllm-mlx not found; set SERVER_BIN or VLLM_MLX_BIN in the profile, or add vllm-mlx to PATH'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "$SERVER_MODEL_ID" 1; then
        log "vLLM-MLX already responding on $HEALTHCHECK_URL"
    else
        log "Starting vLLM-MLX profile $MODEL_PROFILE"
        cmd=(
            "$VLLM_MLX_SERVER"
            serve "$MODEL_SOURCE"
            --host "$HOST"
            --port "$PORT"
            --served-model-name "$SERVER_MODEL_ID"
            --max-tokens "$MAX_TOKENS"
            --max-request-tokens "$MAX_REQUEST_TOKENS"
            --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
            --prefill-step-size "$PREFILL_STEP_SIZE"
        )
        [ -z "$CACHE_MEMORY_PERCENT" ] || cmd+=(--cache-memory-percent "$CACHE_MEMORY_PERCENT")
        [ -z "$CHAT_TEMPLATE_KWARGS" ] || cmd+=(--default-chat-template-kwargs "$CHAT_TEMPLATE_KWARGS")
        [ -z "$REASONING_PARSER" ] || cmd+=(--reasoning-parser "$REASONING_PARSER")
        if [ "$ENABLE_TOOL_CALLS" = "1" ]; then
            cmd+=(--enable-auto-tool-choice --tool-call-parser "$TOOL_CALL_PARSER")
        elif [ -n "$TOOL_CALL_PARSER" ] && [ "$TOOL_CALL_PARSER" != "qwen" ]; then
            cmd+=(--tool-call-parser "$TOOL_CALL_PARSER")
        fi
        if [ "$CONTINUOUS_BATCHING" = "1" ]; then
            cmd+=(
                --continuous-batching
                --max-num-seqs "$MAX_NUM_SEQS"
                --prefill-batch-size "$PREFILL_BATCH_SIZE"
                --completion-batch-size "$COMPLETION_BATCH_SIZE"
            )
        fi
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 240; then
            tail -n 120 "$LOG_PATH" || true
            die "vLLM-MLX failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "ollama" ]; then
    OLLAMA_BIN="$(resolve_first_executable "${SERVER_BIN:-}" "${OLLAMA_BIN:-}" ollama)" || die 'ollama not found; set SERVER_BIN or OLLAMA_BIN in the profile, or add ollama to PATH'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "Ollama already responding on $HEALTHCHECK_URL"
    else
        log "Starting Ollama profile $MODEL_PROFILE"
        cmd=("$OLLAMA_BIN" serve)
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" "${START_TIMEOUT_SECONDS:-120}"; then
            tail -n 120 "$LOG_PATH" || true
            die "Ollama failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "vllm" ]; then
    MODEL_SOURCE="$(model_source_for_adapter || true)"
    [ -n "$MODEL_SOURCE" ] || die "MODEL_REPO, MODEL_ID, MODEL_DIR, MODEL_PATH, or MODEL_FILE is required for vLLM profiles"
    PYTHON_BIN="$(resolve_first_executable "${PYTHON_BIN:-}" python3 python)" || die 'python not found for vLLM profile'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "vLLM already responding on $HEALTHCHECK_URL"
    else
        log "Starting vLLM profile $MODEL_PROFILE"
        cmd=("$PYTHON_BIN" -m "${VLLM_MODULE:-vllm.entrypoints.openai.api_server}" --model "$MODEL_SOURCE" --host "$HOST" --port "$PORT")
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 240; then
            tail -n 120 "$LOG_PATH" || true
            die "vLLM failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "sglang" ]; then
    MODEL_SOURCE="$(model_source_for_adapter || true)"
    [ -n "$MODEL_SOURCE" ] || die "MODEL_REPO, MODEL_ID, MODEL_DIR, MODEL_PATH, or MODEL_FILE is required for SGLang profiles"
    PYTHON_BIN="$(resolve_first_executable "${PYTHON_BIN:-}" python3 python)" || die 'python not found for SGLang profile'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "SGLang already responding on $HEALTHCHECK_URL"
    else
        log "Starting SGLang profile $MODEL_PROFILE"
        cmd=("$PYTHON_BIN" -m "${SGLANG_MODULE:-sglang.launch_server}" --model-path "$MODEL_SOURCE" --host "$HOST" --port "$PORT")
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 240; then
            tail -n 120 "$LOG_PATH" || true
            die "SGLang failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "tgi" ]; then
    MODEL_SOURCE="$(model_source_for_adapter || true)"
    [ -n "$MODEL_SOURCE" ] || die "MODEL_REPO, MODEL_ID, MODEL_DIR, MODEL_PATH, or MODEL_FILE is required for TGI profiles"
    TGI_BIN="$(resolve_first_executable "${SERVER_BIN:-}" "${TGI_SERVER_BIN:-}" text-generation-launcher)" || die 'text-generation-launcher not found; set SERVER_BIN or add it to PATH'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "TGI already responding on $HEALTHCHECK_URL"
    else
        log "Starting TGI profile $MODEL_PROFILE"
        cmd=("$TGI_BIN" --model-id "$MODEL_SOURCE" --hostname "$HOST" --port "$PORT")
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 300; then
            tail -n 120 "$LOG_PATH" || true
            die "TGI failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "llama-cpp-python" ]; then
    MODEL_SOURCE="$(model_source_for_adapter || true)"
    [ -n "$MODEL_SOURCE" ] || die "MODEL_PATH or MODEL_FILE is required for llama-cpp-python profiles"
    [ -f "$MODEL_SOURCE" ] || die "Model file not found: $MODEL_SOURCE"
    PYTHON_BIN="$(resolve_first_executable "${PYTHON_BIN:-}" python3 python)" || die 'python not found for llama-cpp-python profile'

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "llama-cpp-python already responding on $HEALTHCHECK_URL"
    else
        log "Starting llama-cpp-python profile $MODEL_PROFILE"
        cmd=("$PYTHON_BIN" -m "${LLAMA_CPP_PYTHON_MODULE:-llama_cpp.server}" --model "$MODEL_SOURCE" --host "$HOST" --port "$PORT" --model_alias "$MODEL_ALIAS")
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 180; then
            tail -n 120 "$LOG_PATH" || true
            die "llama-cpp-python failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ "$RUNTIME" = "external" ] || [ "$RUNTIME" = "lm-studio" ] || [ "$RUNTIME" = "localai" ] || [ "$RUNTIME" = "jan" ] || [ "$RUNTIME" = "litellm" ] || [ "$RUNTIME" = "llama-swap" ]; then
    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "External profile responding on $HEALTHCHECK_URL"
    elif [ "$HEALTHCHECK_MODE" = "disabled" ]; then
        log "External profile has health checks disabled"
    else
        die "External runtime '$RUNTIME' is not ready at $HEALTHCHECK_URL; start it outside ModelMatrix or provide START_COMMAND"
    fi
elif [ "$RUNTIME" = "command" ] || [ -n "${START_COMMAND:-}" ]; then
    START_COMMAND="${START_COMMAND:-}"
    [ -n "$START_COMMAND" ] || die "START_COMMAND is required for custom runtime profiles"

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "$RUNTIME profile already responding on $HEALTHCHECK_URL"
    else
        log "Starting $RUNTIME profile $MODEL_PROFILE"
        spawn_detached "$LOG_PATH" "$PID_PATH" bash -lc "$START_COMMAND" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" "${START_TIMEOUT_SECONDS:-120}"; then
            tail -n 120 "$LOG_PATH" || true
            die "$RUNTIME runtime failed to become ready for $MODEL_PROFILE"
        fi
    fi
elif [ -n "${SERVER_BIN:-}" ]; then
    [ -n "${SERVER_ARGS_JSON:-}" ] || die "SERVER_ARGS_JSON is required when SERVER_BIN is used for runtime '$RUNTIME' without a native adapter"

    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "$RUNTIME already responding on $HEALTHCHECK_URL"
    else
        log "Starting generic binary profile $MODEL_PROFILE with runtime $RUNTIME"
        cmd=("$SERVER_BIN")
        append_json_args "$SERVER_ARGS_JSON"
        spawn_detached "$LOG_PATH" "$PID_PATH" "${cmd[@]}" >/dev/null
        if ! wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" "${START_TIMEOUT_SECONDS:-180}"; then
            tail -n 120 "$LOG_PATH" || true
            die "Generic runtime '$RUNTIME' failed to become ready for $MODEL_PROFILE"
        fi
    fi
else
    if wait_for_profile_ready "$HEALTHCHECK_MODE" "$HEALTHCHECK_URL" "${HEALTHCHECK_EXPECT_ID:-${SERVER_MODEL_ID:-}}" 1; then
        log "$RUNTIME external profile responding on $HEALTHCHECK_URL"
    elif [ "$HEALTHCHECK_MODE" = "disabled" ]; then
        log "$RUNTIME profile has health checks disabled"
    else
        die "Runtime '$RUNTIME' is not ready at $HEALTHCHECK_URL; provide START_COMMAND, SERVER_BIN with SERVER_ARGS_JSON, or start the endpoint externally"
    fi
fi

install_agentlightning || true

printf 'profile=%s\nruntime=%s\nbase_url=%s\nrequest_model=%s\nlog=%s\n' \
    "$MODEL_PROFILE" "$RUNTIME" "$BASE_URL" "$REQUEST_MODEL" "$LOG_PATH"
if [ -f "$PID_PATH" ]; then
    printf 'pid=%s\n' "$(cat "$PID_PATH" 2>/dev/null || true)"
fi
