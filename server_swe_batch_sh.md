#!/usr/bin/env bash
set -euo pipefail

# =========================
# Local -> Server SWE-bench batch controller
# Run this script on your LOCAL WSL machine.
# =========================

# ---- Basic config ----
SERVER="${SERVER:-root@8.136.135.101}"
REMOTE_PROJECT="${REMOTE_PROJECT:-/root/CodeAgent/files}"

LOCAL_SAVE_ROOT="${LOCAL_SAVE_ROOT:-$HOME/save}"
LOCAL_RESULT_ROOT="${LOCAL_RESULT_ROOT:-$HOME/save/server_results}"

MODE="${MODE:-retrieval}"
MODEL_NAME="${MODEL_NAME:-openai/deepseek-v4-flash}"
API_BASE="${API_BASE:-https://uni-api.cstcloud.cn/v1}"
SUBSET="${SUBSET:-lite}"
SPLIT="${SPLIT:-test}"
WORKERS="${WORKERS:-1}"
STEP_LIMIT="${STEP_LIMIT:-60}"
DOCKER_IMAGE="${DOCKER_IMAGE:-sweagent-multipy:latest}"

RUN_PREFIX="${RUN_PREFIX:-retrieval_server}"

SSH_OPTS="${SSH_OPTS:-}"
RSYNC_SSH="ssh ${SSH_OPTS}"

usage() {
  cat <<EOF
Usage:
  $0 submit  START END     # upload repos/cache and start remote job in background
  $0 status  START END     # show remote pid/status
  $0 tail    START END     # tail remote server log
  $0 fetch   START END     # download results/logs from server
  $0 cleanup START END     # remove remote repos/cache and this batch results/log hints
  $0 run     START END     # submit, wait until done, fetch, then cleanup

Examples:
  $0 submit 20 30
  $0 tail 20 30
  $0 status 20 30
  $0 fetch 20 30
  $0 cleanup 20 30

Env overrides:
  SERVER=root@8.136.135.101
  REMOTE_PROJECT=/root/CodeAgent/files
  LOCAL_SAVE_ROOT=~/save
  LOCAL_RESULT_ROOT=~/save/server_results
  RUN_PREFIX=retrieval_server
EOF
}

need_args() {
  if [[ $# -ne 3 ]]; then
    usage
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

check_local_dirs() {
  local start="$1"
  local end="$2"
  local s
  s="$(slice_name "$start" "$end")"

  local repo_src="${LOCAL_SAVE_ROOT}/repos/slice_${s}/"
  local cache_src="${LOCAL_SAVE_ROOT}/cache/slice_${s}/"

  if [[ ! -d "$repo_src" ]]; then
    echo "ERROR: local repo slice not found: $repo_src"
    exit 1
  fi

  if [[ ! -d "$cache_src" ]]; then
    echo "ERROR: local cache slice not found: $cache_src"
    exit 1
  fi
}

ssh_remote() {
  ssh ${SSH_OPTS} "$SERVER" "$@"
}

submit_job() {
  local start="$1"
  local end="$2"
  local s out_name run_id
  s="$(slice_name "$start" "$end")"
  out_name="$(remote_out_name "$start" "$end")"
  run_id="$(remote_run_id "$start" "$end")"

  local repo_src="${LOCAL_SAVE_ROOT}/repos/slice_${s}/"
  local cache_src="${LOCAL_SAVE_ROOT}/cache/slice_${s}/"

  check_local_dirs "$start" "$end"

  echo "==> Preparing remote directories"
  ssh_remote "bash -lc '
    set -e
    mkdir -p ${REMOTE_PROJECT}/repos ${REMOTE_PROJECT}/cache ${REMOTE_PROJECT}/results ${REMOTE_PROJECT}/_server_runs
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
  ssh_remote "bash -s" <<EOF
set -euo pipefail

REMOTE_PROJECT="${REMOTE_PROJECT}"
OUT_NAME="${out_name}"
RUN_ID="${run_id}"
START="${start}"
END="${end}"
MODE="${MODE}"
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

source /root/miniforge3/etc/profile.d/conda.sh
conda activate sweagent

# Keep HF offline/mirror settings from ~/.bashrc when available.
if [[ -f /root/.bashrc ]]; then
  set +u
  source /root/.bashrc || true
  set -u
fi

export PYTHONUNBUFFERED=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

OUT_NAME="__OUT_NAME__"
RUN_ID="__RUN_ID__"
START="__START__"
END="__END__"

BATCH="python mini_swe_agent_integration/run_swebench_batch.py \
  --mode __MODE__ \
  --model_name __MODEL_NAME__ \
  --api_base __API_BASE__ \
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

mkdir -p "./results/\${OUT_NAME}"

echo "[server] started at \$(date -Is)"
echo "[server] cwd: \$(pwd)"
echo "[server] out_name: \${OUT_NAME}"
echo "[server] run_id: \${RUN_ID}"
echo "[server] batch: \${BATCH}"

python run_and_analyse.py \
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
EOF

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

tail_job() {
  local start="$1"
  local end="$2"
  local out_name
  out_name="$(remote_out_name "$start" "$end")"

  ssh_remote "bash -lc '
    tail -f ${REMOTE_PROJECT}/results/${out_name}/server_nohup.log
  '"
}

fetch_results() {
  local start="$1"
  local end="$2"
  local s out_name local_dst
  s="$(slice_name "$start" "$end")"
  out_name="$(remote_out_name "$start" "$end")"
  local_dst="${LOCAL_RESULT_ROOT}/slice_${s}"

  mkdir -p "${local_dst}/results" "${local_dst}/logs"

  echo "==> Fetching result dir"
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

cleanup_remote() {
  local start="$1"
  local end="$2"
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

run_wait_fetch_cleanup() {
  local start="$1"
  local end="$2"
  submit_job "$start" "$end"

  echo "==> Waiting for remote job to finish..."
  while true; do
    sleep 60
    local out_name pid_status
    out_name="$(remote_out_name "$start" "$end")"

    pid_status="$(ssh_remote "bash -lc '
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
    '")"

    echo "remote status: ${pid_status}"

    if [[ "$pid_status" != "RUNNING" ]]; then
      break
    fi
  done

  fetch_results "$start" "$end"

  echo
  echo "Job is finished and results fetched."
  echo "Remote cleanup is optional. Run manually:"
  echo "  $0 cleanup $start $end"
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
      need_args "$cmd" "$@"
      submit_job "$1" "$2"
      ;;
    status)
      need_args "$cmd" "$@"
      status_job "$1" "$2"
      ;;
    tail)
      need_args "$cmd" "$@"
      tail_job "$1" "$2"
      ;;
    fetch)
      need_args "$cmd" "$@"
      fetch_results "$1" "$2"
      ;;
    cleanup)
      need_args "$cmd" "$@"
      cleanup_remote "$1" "$2"
      ;;
    run)
      need_args "$cmd" "$@"
      run_wait_fetch_cleanup "$1" "$2"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"