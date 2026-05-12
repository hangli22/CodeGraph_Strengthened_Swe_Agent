cat > ~/server_swe_batch.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

# =========================
# Local -> Server SWE-bench batch controller
# Run this script on your LOCAL WSL machine.
#
# Single-slice commands:
#   submit/status/tail/fetch/cleanup START END
#
# Sequential range command:
#   run START END
#   Example: run 0 30 => run 0:10, 10:20, 20:30 sequentially.
# =========================

# ---- Basic config ----
SERVER="${SERVER:-root@8.136.135.101}"
REMOTE_PROJECT="${REMOTE_PROJECT:-/root/CodeAgent/files}"

LOCAL_SAVE_ROOT="${LOCAL_SAVE_ROOT:-$HOME/save}"
LOCAL_RESULT_ROOT="${LOCAL_RESULT_ROOT:-$HOME/save/server_results}"

MODE="${MODE:-retrieval}"
LLM_BACKEND="${LLM_BACKEND:-uni}"
MODEL_NAME="${MODEL_NAME:-openai/deepseek-v4-flash}"

# Keep empty by default.
# run_swebench_batch.py will choose backend defaults:
#   uni      -> https://uni-api.cstcloud.cn/v1
#   deepseek -> https://api.deepseek.com
API_BASE="${API_BASE:-}"

SUBSET="${SUBSET:-lite}"
SPLIT="${SPLIT:-test}"
WORKERS="${WORKERS:-1}"
STEP_LIMIT="${STEP_LIMIT:-60}"
DOCKER_IMAGE="${DOCKER_IMAGE:-sweagent-multipy:latest}"

RUN_PREFIX="${RUN_PREFIX:-retrieval_server}"

SSH_OPTS="${SSH_OPTS:-}"
RSYNC_SSH="ssh ${SSH_OPTS}"

CHUNK_SIZE="${CHUNK_SIZE:-10}"

usage() {
  cat <<EOF_USAGE
Usage:
  $0 submit  START END     # upload one slice repos/cache and start remote job in background
  $0 status  START END     # show remote pid/status for one slice
  $0 tail    START END     # tail remote server log for one slice
  $0 fetch   START END     # download results/logs for one slice
  $0 cleanup START END     # remove remote repos/cache and this slice results/log hints
  $0 run     START END     # run one or multiple 10-size slices sequentially, fetch each, cleanup remote between slices

Examples:
  $0 submit 20 30
  $0 tail 20 30
  $0 status 20 30
  $0 fetch 20 30
  $0 cleanup 20 30

  $0 run 20 30
  $0 run 0 30      # runs 0:10, 10:20, 20:30 sequentially
  $0 run 30 60     # runs 30:40, 40:50, 50:60 sequentially

Backend examples:
  LLM_BACKEND=uni MODEL_NAME=openai/deepseek-v4-flash $0 run 20 30
  LLM_BACKEND=deepseek MODEL_NAME=openai/deepseek-v4-flash $0 run 20 30
  LLM_BACKEND=ds MODEL_NAME=openai/deepseek-v4-flash $0 run 20 30

Env overrides:
  SERVER=root@8.136.135.101
  REMOTE_PROJECT=/root/CodeAgent/files
  LOCAL_SAVE_ROOT=~/save
  LOCAL_RESULT_ROOT=~/save/server_results
  RUN_PREFIX=retrieval_server
  CHUNK_SIZE=10
  LLM_BACKEND=uni|deepseek|ds
  MODEL_NAME=openai/deepseek-v4-flash
  API_BASE=https://api.deepseek.com
EOF_USAGE
}

need_args() {
  if [[ $# -ne 2 ]]; then
    usage
    exit 1
  fi
}

require_int() {
  local x="$1"
  if ! [[ "$x" =~ ^[0-9]+$ ]]; then
    echo "ERROR: not a non-negative integer: $x"
    exit 1
  fi
}

validate_range() {
  local start="$1"
  local end="$2"

  require_int "$start"
  require_int "$end"

  if (( start >= end )); then
    echo "ERROR: START must be less than END: $start $end"
    exit 1
  fi

  if (( start % CHUNK_SIZE != 0 || end % CHUNK_SIZE != 0 )); then
    echo "ERROR: START and END must be multiples of CHUNK_SIZE=$CHUNK_SIZE"
    exit 1
  fi
}

validate_single_slice() {
  local start="$1"
  local end="$2"
  validate_range "$start" "$end"

  if (( end - start != CHUNK_SIZE )); then
    echo "ERROR: this command expects exactly one slice of size $CHUNK_SIZE."
    echo "Use:"
    echo "  $0 run $start $end"
    echo "for multi-slice sequential execution."
    exit 1
  fi
}

slice_name() {
  local start="$1"
  local end="$2"
  echo "${start}_${end}"
}

remote_out_name() {
  local start="$1"
  local end="$2"
  echo "${RUN_PREFIX}_$(slice_name "$start" "$end")"
}

remote_run_id() {
  local start="$1"
  local end="$2"
  echo "$(remote_out_name "$start" "$end")_${start}_${end}"
}

local_repo_dir() {
  local start="$1"
  local end="$2"
  local s
  s="$(slice_name "$start" "$end")"
  echo "${LOCAL_SAVE_ROOT}/repos/slice_${s}"
}

local_cache_dir() {
  local start="$1"
  local end="$2"
  local s
  s="$(slice_name "$start" "$end")"
  echo "${LOCAL_SAVE_ROOT}/cache/slice_${s}"
}

is_dir_empty() {
  local d="$1"
  [[ -d "$d" ]] && [[ -z "$(find "$d" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]
}

ensure_local_dirs_for_submit() {
  local start="$1"
  local end="$2"

  local repo_src cache_src
  repo_src="$(local_repo_dir "$start" "$end")"
  cache_src="$(local_cache_dir "$start" "$end")"

  if [[ ! -d "$repo_src" ]]; then
    echo "ERROR: local repo slice not found: $repo_src"
    echo "You must prepare local repos for this slice first."
    exit 1
  fi

  if [[ ! -d "$cache_src" ]]; then
    echo "WARNING: local cache slice not found; creating empty cache dir: $cache_src"
    mkdir -p "$cache_src"
  fi
}

ssh_remote() {
  ssh ${SSH_OPTS} "$SERVER" "$@"
}

submit_job() {
  local start="$1"
  local end="$2"
  validate_single_slice "$start" "$end"

  local s out_name run_id
  s="$(slice_name "$start" "$end")"
  out_name="$(remote_out_name "$start" "$end")"
  run_id="$(remote_run_id "$start" "$end")"

  local repo_src cache_src
  repo_src="$(local_repo_dir "$start" "$end")/"
  cache_src="$(local_cache_dir "$start" "$end")/"

  ensure_local_dirs_for_submit "$start" "$end"

  echo "==> Preparing remote directories"
  ssh_remote "bash -lc '
    set -e
    mkdir -p ${REMOTE_PROJECT}/repos ${REMOTE_PROJECT}/cache ${REMOTE_PROJECT}/results ${REMOTE_PROJECT}/_server_runs
    git config --global --add safe.directory \"*\" 2>/dev/null || true
    rm -rf ${REMOTE_PROJECT}/repos/* ${REMOTE_PROJECT}/cache/*
  '"

  echo "==> Uploading repos: $repo_src -> ${SERVER}:${REMOTE_PROJECT}/repos/"
  rsync -az --delete --info=progress2 -e "$RSYNC_SSH" \
    "$repo_src" \
    "${SERVER}:${REMOTE_PROJECT}/repos/"

  echo "==> Uploading cache: $cache_src -> ${SERVER}:${REMOTE_PROJECT}/cache/"
  rsync -az --delete --info=progress2 -e "$RSYNC_SSH" \
    "$cache_src" \
    "${SERVER}:${REMOTE_PROJECT}/cache/"

  echo "==> Creating remote run script"
  ssh_remote "bash -s" <<EOF_REMOTE
set -euo pipefail

REMOTE_PROJECT="${REMOTE_PROJECT}"
OUT_NAME="${out_name}"
RUN_ID="${run_id}"
START="${start}"
END="${end}"
MODE="${MODE}"
LLM_BACKEND="${LLM_BACKEND}"
MODEL_NAME="${MODEL_NAME}"
API_BASE="${API_BASE}"
SUBSET="${SUBSET}"
SPLIT="${SPLIT}"
WORKERS="${WORKERS}"
STEP_LIMIT="${STEP_LIMIT}"
DOCKER_IMAGE="${DOCKER_IMAGE}"

mkdir -p "\${REMOTE_PROJECT}/_server_runs" "\${REMOTE_PROJECT}/results/\${OUT_NAME}"

cat > "\${REMOTE_PROJECT}/_server_runs/run_\${OUT_NAME}.sh" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail

cd "__REMOTE_PROJECT__"

# Load user shell settings first.
# Important: /root/.bashrc may auto-activate conda base or rewrite PATH.
# Therefore we source it BEFORE activating the target sweagent environment.
if [[ -f /root/.bashrc ]]; then
  set +u
  source /root/.bashrc || true
  set -u
fi

# Load CodeAgent-specific environment variables.
# Do not rely on .bashrc tail because .bashrc may return early in non-interactive shells.
if [[ -f /root/codeagent_env.sh ]]; then
  source /root/codeagent_env.sh
fi

# Activate the target runtime environment last so it cannot be overwritten by .bashrc.
source /root/miniforge3/etc/profile.d/conda.sh
conda activate sweagent
hash -r

# Load project env again after conda activation so project variables win.
if [[ -f /root/codeagent_env.sh ]]; then
  source /root/codeagent_env.sh
fi

git config --global --add safe.directory "*" 2>/dev/null || true

export PYTHONUNBUFFERED=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

PYTHON_BIN="\$(command -v python)"

echo "[server-debug] CONDA_DEFAULT_ENV=\${CONDA_DEFAULT_ENV:-}"
echo "[server-debug] python=\${PYTHON_BIN}"
"\${PYTHON_BIN}" - <<'PY'
import os
import sys

print("[server-debug] sys.executable=", sys.executable)
print("[server-debug] HF_HOME=", os.environ.get("HF_HOME", ""))
print("[server-debug] HF_DATASETS_OFFLINE=", os.environ.get("HF_DATASETS_OFFLINE", ""))
print("[server-debug] SWE_LLM_BACKEND=", os.environ.get("SWE_LLM_BACKEND", ""))
print("[server-debug] UNI_API_KEY_len=", len(os.environ.get("UNI_API_KEY", "")))
print("[server-debug] OPENAI_API_KEY_len=", len(os.environ.get("OPENAI_API_KEY", "")))
print("[server-debug] DASHSCOPE_API_KEY_len=", len(os.environ.get("DASHSCOPE_API_KEY", "")))
print("[server-debug] DEEPSEEK_API_KEY_len=", len(os.environ.get("DEEPSEEK_API_KEY", "")))
try:
    import datasets
    print("[server-debug] datasets ok:", getattr(datasets, "__version__", "unknown"))
except Exception as e:
    print("[server-debug] datasets failed:", repr(e))
    raise
PY

OUT_NAME="__OUT_NAME__"
RUN_ID="__RUN_ID__"
START="__START__"
END="__END__"
MODE="__MODE__"
LLM_BACKEND="__LLM_BACKEND__"
MODEL_NAME="__MODEL_NAME__"
API_BASE="__API_BASE__"

BATCH="\${PYTHON_BIN} mini_swe_agent_integration/run_swebench_batch.py \
  --mode \${MODE} \
  --llm_backend \${LLM_BACKEND} \
  --model_name \${MODEL_NAME} \
  --subset __SUBSET__ \
  --split __SPLIT__ \
  --slice \${START}:\${END} \
  --output_dir ./results/\${OUT_NAME} \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers __WORKERS__ \
  --step_limit __STEP_LIMIT__ \
  --use_docker \
  --docker_image __DOCKER_IMAGE__ \
  --redo"

if [[ -n "\${API_BASE}" ]]; then
  BATCH="\${BATCH} --api_base \${API_BASE}"
fi

mkdir -p "./results/\${OUT_NAME}"

echo "[server] started at \$(date -Is)"
echo "[server] cwd: \$(pwd)"
echo "[server] out_name: \${OUT_NAME}"
echo "[server] run_id: \${RUN_ID}"
echo "[server] mode: \${MODE}"
echo "[server] llm_backend: \${LLM_BACKEND}"
echo "[server] model_name: \${MODEL_NAME}"
echo "[server] api_base: \${API_BASE:-<backend-default>}"
echo "[server] batch: \${BATCH}"

"\${PYTHON_BIN}" run_and_analyse.py \
  --batch-cmd "\${BATCH}" \
  --run-id "\${RUN_ID}" \
  --running-log "./results/\${OUT_NAME}/running.md" \
  --analyse-log "./results/\${OUT_NAME}/analyse_result.md"

echo "[server] finished at \$(date -Is)"
EOS

python3 - <<PY
from pathlib import Path

p = Path("${REMOTE_PROJECT}/_server_runs/run_${out_name}.sh")
text = p.read_text()
repls = {
    "__REMOTE_PROJECT__": "${REMOTE_PROJECT}",
    "__OUT_NAME__": "${out_name}",
    "__RUN_ID__": "${run_id}",
    "__START__": "${start}",
    "__END__": "${end}",
    "__MODE__": "${MODE}",
    "__LLM_BACKEND__": "${LLM_BACKEND}",
    "__MODEL_NAME__": "${MODEL_NAME}",
    "__API_BASE__": "${API_BASE}",
    "__SUBSET__": "${SUBSET}",
    "__SPLIT__": "${SPLIT}",
    "__WORKERS__": "${WORKERS}",
    "__STEP_LIMIT__": "${STEP_LIMIT}",
    "__DOCKER_IMAGE__": "${DOCKER_IMAGE}",
}
for k, v in repls.items():
    text = text.replace(k, v)
p.write_text(text)
PY

chmod +x "\${REMOTE_PROJECT}/_server_runs/run_\${OUT_NAME}.sh"
EOF_REMOTE

  echo "==> Starting remote job with nohup"
  ssh_remote "bash -lc '
    set -e
    cd ${REMOTE_PROJECT}
    OUT_NAME=${out_name}
    RUN_SCRIPT=${REMOTE_PROJECT}/_server_runs/run_${out_name}.sh
    LOG=${REMOTE_PROJECT}/results/${out_name}/server_nohup.log
    PID_FILE=${REMOTE_PROJECT}/results/${out_name}/server.pid

    nohup bash \"\$RUN_SCRIPT\" > \"\$LOG\" 2>&1 < /dev/null &
    echo \$! > \"\$PID_FILE\"

    echo \"Started remote job\"
    echo \"PID: \$(cat \$PID_FILE)\"
    echo \"Log: \$LOG\"
  '"

  echo
  echo "Submitted."
  echo "Check status:"
  echo "  $0 status $start $end"
  echo "Tail log:"
  echo "  $0 tail $start $end"
  echo "Fetch results after finish:"
  echo "  $0 fetch $start $end"
}

status_job() {
  local start="$1"
  local end="$2"
  validate_single_slice "$start" "$end"

  local out_name
  out_name="$(remote_out_name "$start" "$end")"

  ssh_remote "bash -lc '
    PID_FILE=${REMOTE_PROJECT}/results/${out_name}/server.pid
    LOG=${REMOTE_PROJECT}/results/${out_name}/server_nohup.log

    echo \"out_name: ${out_name}\"
    echo \"pid_file: \$PID_FILE\"
    echo \"log: \$LOG\"

    if [[ ! -f \"\$PID_FILE\" ]]; then
      echo \"status: NO_PID_FILE\"
      exit 0
    fi

    PID=\$(cat \"\$PID_FILE\")
    echo \"pid: \$PID\"

    if kill -0 \"\$PID\" 2>/dev/null; then
      echo \"status: RUNNING\"
      ps -p \"\$PID\" -o pid,ppid,etime,cmd || true
    else
      echo \"status: NOT_RUNNING_OR_FINISHED\"
    fi

    echo
    echo \"last log lines:\"
    tail -40 \"\$LOG\" 2>/dev/null || true
  '"
}

pid_status_one_slice() {
  local start="$1"
  local end="$2"
  local out_name
  out_name="$(remote_out_name "$start" "$end")"

  ssh_remote "bash -lc '
    PID_FILE=${REMOTE_PROJECT}/results/${out_name}/server.pid
    if [[ ! -f \"\$PID_FILE\" ]]; then
      echo NO_PID
    else
      PID=\$(cat \"\$PID_FILE\")
      if kill -0 \"\$PID\" 2>/dev/null; then
        echo RUNNING
      else
        echo FINISHED
      fi
    fi
  '"
}

tail_job() {
  local start="$1"
  local end="$2"
  validate_single_slice "$start" "$end"

  local out_name
  out_name="$(remote_out_name "$start" "$end")"

  ssh_remote "bash -lc '
    tail -f ${REMOTE_PROJECT}/results/${out_name}/server_nohup.log
  '"
}

fetch_results() {
  local start="$1"
  local end="$2"
  validate_single_slice "$start" "$end"

  local s out_name local_dst
  s="$(slice_name "$start" "$end")"
  out_name="$(remote_out_name "$start" "$end")"
  local_dst="${LOCAL_RESULT_ROOT}/slice_${s}"

  mkdir -p "${local_dst}/results" "${local_dst}/logs"

  echo "==> Fetching result dir for slice ${start}:${end}"
  rsync -az --info=progress2 -e "$RSYNC_SSH" \
    "${SERVER}:${REMOTE_PROJECT}/results/${out_name}/" \
    "${local_dst}/results/${out_name}/"

  echo "==> Fetching harness logs"
  rsync -az --info=progress2 -e "$RSYNC_SSH" \
    "${SERVER}:${REMOTE_PROJECT}/logs/run_evaluation/" \
    "${local_dst}/logs/run_evaluation/" || true

  echo "==> Local result saved to:"
  echo "    ${local_dst}"

  if [[ -f "${local_dst}/results/${out_name}/preds.json" ]]; then
    echo "OK: preds.json fetched"
  else
    echo "WARNING: preds.json not found in fetched result"
  fi

  if [[ -f "${local_dst}/results/${out_name}/eval_summary.csv" ]]; then
    echo "OK: eval_summary.csv fetched"
  else
    echo "WARNING: eval_summary.csv not found in fetched result"
  fi
}

fetch_remote_cache_to_local_if_needed() {
  local start="$1"
  local end="$2"
  local cache_dst
  cache_dst="$(local_cache_dir "$start" "$end")"

  mkdir -p "$cache_dst"

  echo "==> Backfilling local cache from remote for slice ${start}:${end}"
  echo "    ${SERVER}:${REMOTE_PROJECT}/cache/ -> ${cache_dst}/"

  rsync -az --delete --info=progress2 -e "$RSYNC_SSH" \
    "${SERVER}:${REMOTE_PROJECT}/cache/" \
    "${cache_dst}/"

  local count
  count="$(find "$cache_dst" -mindepth 1 -maxdepth 2 -print 2>/dev/null | wc -l || true)"
  echo "==> Local cache backfilled: ${cache_dst} (${count} entries within depth<=2)"
}

cleanup_remote_no_prompt() {
  local start="$1"
  local end="$2"
  local out_name run_id
  out_name="$(remote_out_name "$start" "$end")"
  run_id="$(remote_run_id "$start" "$end")"

  echo "==> Auto cleanup remote data for ${out_name}"
  ssh_remote "bash -lc '
    set -e
    docker ps --format \"{{.Names}}\" | grep \"^minisweagent-\" | xargs -r docker rm -f || true
    rm -rf ${REMOTE_PROJECT}/repos/*
    rm -rf ${REMOTE_PROJECT}/cache/*
    rm -rf ${REMOTE_PROJECT}/_server_runs/run_${out_name}.sh

    if [[ -d ${REMOTE_PROJECT}/logs/run_evaluation ]]; then
      find ${REMOTE_PROJECT}/logs/run_evaluation -path \"*${run_id}*\" -print -exec rm -rf {} + 2>/dev/null || true
    fi

    echo \"remote auto cleanup done\"
  '"
}

cleanup_remote() {
  local start="$1"
  local end="$2"
  validate_single_slice "$start" "$end"

  local out_name run_id
  out_name="$(remote_out_name "$start" "$end")"
  run_id="$(remote_run_id "$start" "$end")"

  echo "About to clean remote data for ${out_name}"
  echo "This removes:"
  echo "  ${REMOTE_PROJECT}/repos/*"
  echo "  ${REMOTE_PROJECT}/cache/*"
  echo "  ${REMOTE_PROJECT}/results/${out_name}"
  echo "  ${REMOTE_PROJECT}/_server_runs/run_${out_name}.sh"
  echo "  matching harness logs containing run_id: ${run_id}"
  read -r -p "Type YES to continue: " ans
  if [[ "$ans" != "YES" ]]; then
    echo "Cancelled."
    exit 0
  fi

  ssh_remote "bash -lc '
    set -e
    docker ps --format \"{{.Names}}\" | grep \"^minisweagent-\" | xargs -r docker rm -f || true
    rm -rf ${REMOTE_PROJECT}/repos/*
    rm -rf ${REMOTE_PROJECT}/cache/*
    rm -rf ${REMOTE_PROJECT}/results/${out_name}
    rm -f ${REMOTE_PROJECT}/_server_runs/run_${out_name}.sh

    if [[ -d ${REMOTE_PROJECT}/logs/run_evaluation ]]; then
      find ${REMOTE_PROJECT}/logs/run_evaluation -path \"*${run_id}*\" -print -exec rm -rf {} + 2>/dev/null || true
    fi

    echo \"remote cleanup done\"
  '"
}

wait_for_slice_finish() {
  local start="$1"
  local end="$2"

  echo "==> Waiting for remote job ${start}:${end} to finish..."
  while true; do
    sleep 60
    local pid_status
    pid_status="$(pid_status_one_slice "$start" "$end")"

    echo "remote status for ${start}:${end}: ${pid_status}"

    if [[ "$pid_status" != "RUNNING" ]]; then
      break
    fi
  done
}

run_one_slice_and_fetch_cleanup() {
  local start="$1"
  local end="$2"
  validate_single_slice "$start" "$end"

  local cache_dir cache_was_empty
  cache_dir="$(local_cache_dir "$start" "$end")"

  ensure_local_dirs_for_submit "$start" "$end"

  cache_was_empty=0
  if is_dir_empty "$cache_dir"; then
    cache_was_empty=1
    echo "==> Local cache for slice ${start}:${end} is empty; it will be backfilled after remote run if cache is generated."
  else
    echo "==> Local cache for slice ${start}:${end} is non-empty; it will be uploaded and not overwritten automatically."
  fi

  submit_job "$start" "$end"
  wait_for_slice_finish "$start" "$end"
  fetch_results "$start" "$end"

  if [[ "$cache_was_empty" == "1" ]]; then
    fetch_remote_cache_to_local_if_needed "$start" "$end"
  fi

  cleanup_remote_no_prompt "$start" "$end"

  echo
  echo "==> Slice ${start}:${end} finished, fetched, and remote repos/cache cleaned."
  echo
}

run_range_sequential() {
  local start="$1"
  local end="$2"
  validate_range "$start" "$end"

  echo "==> Sequential range run: ${start}:${end}, chunk_size=${CHUNK_SIZE}"
  echo "==> LLM_BACKEND=${LLM_BACKEND}"
  echo "==> MODEL_NAME=${MODEL_NAME}"
  echo "==> API_BASE=${API_BASE:-<backend-default>}"

  local cur next
  cur="$start"
  while (( cur < end )); do
    next=$((cur + CHUNK_SIZE))
    echo
    echo "============================================================"
    echo "==> Running chunk ${cur}:${next}"
    echo "============================================================"
    run_one_slice_and_fetch_cleanup "$cur" "$next"
    cur="$next"
  done

  echo "==> All chunks completed for range ${start}:${end}"
}

main() {
  if [[ $# -lt 1 ]]; then
    usage
    exit 1
  fi

  local cmd="$1"
  shift

  case "$cmd" in
    submit)
      need_args "$@"
      submit_job "$1" "$2"
      ;;
    status)
      need_args "$@"
      status_job "$1" "$2"
      ;;
    tail)
      need_args "$@"
      tail_job "$1" "$2"
      ;;
    fetch)
      need_args "$@"
      fetch_results "$1" "$2"
      ;;
    cleanup)
      need_args "$@"
      cleanup_remote "$1" "$2"
      ;;
    run)
      need_args "$@"
      run_range_sequential "$1" "$2"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
EOF

chmod +x ~/server_swe_batch.sh
bash -n ~/server_swe_batch.sh