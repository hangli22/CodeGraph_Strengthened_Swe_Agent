cp ~/server_swe_batch.sh ~/server_swe_batch.sh.bak.$(date +%Y%m%d_%H%M%S) && \
cat > ~/server_swe_batch.sh <<'EOF'
#!/usr/bin/env bash
# server_swe_batch.sh  —— 异步离线批跑版
# 四个子命令互不依赖：submit（提交即走） / status / pull（不删远程） / cleanup
# 兼容旧的 run（顺序同步，等全跑完一次拉回并清掉）
set -euo pipefail

# ============ Config（按需改，或用环境变量覆盖）============
REMOTE_USER="${REMOTE_USER:-your_user}"
REMOTE_HOST="${REMOTE_HOST:-your.server.com}"
REMOTE_PROJECT="${REMOTE_PROJECT:-/home/your_user/swe_proj}"
LOCAL_SAVE="${LOCAL_SAVE:-$HOME/save}"
CHUNK_SIZE="${CHUNK_SIZE:-10}"
SSH_OPTS="${SSH_OPTS:--o ServerAliveInterval=30 -o ServerAliveCountMax=3}"
SLURM_SCRIPT="${SLURM_SCRIPT:-run_swe.sbatch}"   # 在 REMOTE_PROJECT 下
JOB_PREFIX="${JOB_PREFIX:-swe_slice_}"

# 远程各类产物的父目录（按你原本脚本里的实际位置改）
REMOTE_RESULTS_ROOT="${REMOTE_RESULTS_ROOT:-${REMOTE_PROJECT}/results}"
REMOTE_CACHE_ROOT="${REMOTE_CACHE_ROOT:-${REMOTE_PROJECT}/cache}"

# ============ Helpers ============
ssh_remote() { ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "$@"; }

rsync_from_remote() {
  rsync -a --info=progress2 -e "ssh $SSH_OPTS" \
        "${REMOTE_USER}@${REMOTE_HOST}:$1" "$2"
}

rsync_to_remote() {
  rsync -a --info=progress2 -e "ssh $SSH_OPTS" \
        "$1" "${REMOTE_USER}@${REMOTE_HOST}:$2"
}

remote_out_name() { echo "${JOB_PREFIX}$1_$2"; }          # 如 swe_slice_10_20
local_result_dir() { echo "${LOCAL_SAVE}/results/$(remote_out_name "$1" "$2")"; }
local_cache_dir()  { echo "${LOCAL_SAVE}/cache/$(remote_out_name "$1" "$2")"; }

is_dir_empty() {
  local d="$1"
  [[ -d "$d" ]] || return 0
  [[ -z "$(ls -A "$d" 2>/dev/null)" ]]
}

validate_single_slice() {
  local s="$1" e="$2"
  [[ "$s" =~ ^[0-9]+$ && "$e" =~ ^[0-9]+$ ]] \
    || { echo "ERROR: start/end must be integers" >&2; exit 2; }
  (( e > s )) || { echo "ERROR: end must > start" >&2; exit 2; }
}

ensure_local_dirs_for_submit() {
  local s="$1" e="$2"
  mkdir -p "$(local_result_dir "$s" "$e")"
  mkdir -p "$(local_cache_dir  "$s" "$e")"
}

# ============ 远程原子操作（不同子命令共用）============

# 提交一个 slice（不 wait）。如果你原脚本里 sbatch 行有特殊参数，把下面这条改成你原来的即可。
submit_job() {
  local s="$1" e="$2"
  local out; out="$(remote_out_name "$s" "$e")"
  ssh_remote "cd '${REMOTE_PROJECT}' && \
    mkdir -p '${REMOTE_RESULTS_ROOT}/${out}' '${REMOTE_CACHE_ROOT}/${out}' && \
    sbatch --job-name='${out}' \
           --export=ALL,SLICE_START=${s},SLICE_END=${e},OUT_NAME=${out} \
           '${SLURM_SCRIPT}'"
}

fetch_results() {
  local s="$1" e="$2"
  local out; out="$(remote_out_name "$s" "$e")"
  local dst; dst="$(local_result_dir "$s" "$e")"
  mkdir -p "${dst}"
  rsync_from_remote "${REMOTE_RESULTS_ROOT}/${out}/" "${dst}/"
}

# 注意：按你意愿保留"本地空才回传 cache"的原逻辑。
# 你选择的工作模式是"远程就地存 cache，本地择时回传一次"，这个条件天然成立。
fetch_remote_cache_to_local_if_needed() {
  local s="$1" e="$2"
  local out; out="$(remote_out_name "$s" "$e")"
  local dst; dst="$(local_cache_dir "$s" "$e")"
  mkdir -p "${dst}"

  # 如果远程该 slice 的 cache 根本不存在，就跳过（可能是代码没产生 cache）
  if ! ssh_remote "test -d '${REMOTE_CACHE_ROOT}/${out}'"; then
    echo "  (remote cache ${out} not found, skip)"
    return 0
  fi

  if is_dir_empty "${dst}"; then
    echo "  >> pulling cache ${out}"
    rsync_from_remote "${REMOTE_CACHE_ROOT}/${out}/" "${dst}/"
  else
    echo "  (local cache ${out} non-empty, skip per policy)"
  fi
}

cleanup_remote_no_prompt() {
  local s="$1" e="$2"
  local out; out="$(remote_out_name "$s" "$e")"
  ssh_remote "rm -rf '${REMOTE_RESULTS_ROOT}/${out}' '${REMOTE_CACHE_ROOT}/${out}'"
  echo "  cleaned remote ${out}"
}

# ============ 子命令 ============

submit_range() {
  local s="$1" e="$2"
  validate_single_slice "$s" "$e"
  local cur="$s" nxt
  while (( cur < e )); do
    nxt=$((cur + CHUNK_SIZE)); (( nxt > e )) && nxt=$e
    validate_single_slice "$cur" "$nxt"
    ensure_local_dirs_for_submit "$cur" "$nxt"
    submit_job "$cur" "$nxt"
    echo "SUBMITTED ${cur}:${nxt}"
    cur="$nxt"
  done
  echo "== all submitted. 可以安全关本地。=="
}

status_range() {
  local s="$1" e="$2"
  validate_single_slice "$s" "$e"
  echo "== squeue (current user) =="
  ssh_remote "squeue -u \$USER -o '%.12i %.40j %.8T %.10M %R'" || true
  echo
  echo "== per-slice state =="
  local cur="$s" nxt
  while (( cur < e )); do
    nxt=$((cur + CHUNK_SIZE)); (( nxt > e )) && nxt=$e
    local out; out="$(remote_out_name "$cur" "$nxt")"
    if ssh_remote "test -f '${REMOTE_RESULTS_ROOT}/${out}/preds.json'"; then
      echo "  [DONE]    ${cur}:${nxt}"
    elif ssh_remote "test -d '${REMOTE_RESULTS_ROOT}/${out}'"; then
      echo "  [RUNNING] ${cur}:${nxt}"
    else
      echo "  [PENDING] ${cur}:${nxt}"
    fi
    cur="$nxt"
  done
}

pull_range() {
  local s="$1" e="$2"
  validate_single_slice "$s" "$e"
  local cur="$s" nxt pulled=0 skipped=0
  while (( cur < e )); do
    nxt=$((cur + CHUNK_SIZE)); (( nxt > e )) && nxt=$e
    local out; out="$(remote_out_name "$cur" "$nxt")"
    if ssh_remote "test -f '${REMOTE_RESULTS_ROOT}/${out}/preds.json'"; then
      echo ">> pulling ${cur}:${nxt}"
      fetch_results "$cur" "$nxt"
      fetch_remote_cache_to_local_if_needed "$cur" "$nxt"
      pulled=$((pulled+1))
    else
      echo "SKIP ${cur}:${nxt}  preds.json 尚未生成（还没跑完）"
      skipped=$((skipped+1))
    fi
    cur="$nxt"
  done
  echo "== pull done. pulled=${pulled} skipped=${skipped} =="
}

cleanup_range() {
  local s="$1" e="$2"
  validate_single_slice "$s" "$e"
  local cur="$s" nxt cleaned=0 refused=0
  while (( cur < e )); do
    nxt=$((cur + CHUNK_SIZE)); (( nxt > e )) && nxt=$e
    local out; out="$(remote_out_name "$cur" "$nxt")"
    local local_preds; local_preds="$(local_result_dir "$cur" "$nxt")/preds.json"
    if [[ -f "$local_preds" ]]; then
      cleanup_remote_no_prompt "$cur" "$nxt"
      cleaned=$((cleaned+1))
    else
      echo "REFUSE ${cur}:${nxt}  local preds.json missing (${local_preds})"
      echo "       请先 $0 pull ${cur} ${nxt}"
      refused=$((refused+1))
    fi
    cur="$nxt"
  done
  echo "== cleanup done. cleaned=${cleaned} refused=${refused} =="
}

# 旧的同步模式：提交 → 本地 while 轮询 → 全拉回 → 清远程。
# 需要本地一直在线（可以包 tmux/nohup）。
run_range_sequential() {
  local s="$1" e="$2"
  submit_range "$s" "$e"
  echo "== polling until all ${JOB_PREFIX}* jobs finish =="
  while :; do
    local remaining
    remaining=$(ssh_remote "squeue -u \$USER -h -o '%j' | grep -c '^${JOB_PREFIX}' || true" | tr -dc '0-9')
    [[ -z "$remaining" ]] && remaining=0
    if (( remaining == 0 )); then break; fi
    echo "  still ${remaining} running... sleep 60s"
    sleep 60
  done
  pull_range    "$s" "$e"
  cleanup_range "$s" "$e"
}

# ============ main ============
usage() {
  cat <<EOF
Usage:
  $0 submit  START END        # 提交 [START,END) 全部 chunk 的 sbatch 后立刻返回
  $0 status  START END        # squeue + 每个 slice 的 DONE/RUNNING/PENDING
  $0 pull    START END        # rsync 回 results + cache（不删远程）
  $0 cleanup START END        # 本地已有 preds.json 的 slice → 删远程；否则拒绝
  $0 run     START END        # 旧行为：submit+wait+pull+cleanup，一条龙但需本地在线

Env:
  CHUNK_SIZE (default 10)
  REMOTE_USER REMOTE_HOST REMOTE_PROJECT LOCAL_SAVE
  REMOTE_RESULTS_ROOT REMOTE_CACHE_ROOT SLURM_SCRIPT JOB_PREFIX

Typical async workflow:
  $0 submit 0 100         # 提交完就可以关电脑
  $0 status 0 100         # 下次上线时看看
  $0 pull   0 100         # 把已完成的都拉回本地
  # 人工确认 preds.json / 本地评测都 OK 之后：
  $0 cleanup 0 100        # 释放服务器磁盘
EOF
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    submit)  shift; submit_range        "$@" ;;
    status)  shift; status_range        "$@" ;;
    pull)    shift; pull_range          "$@" ;;
    cleanup) shift; cleanup_range       "$@" ;;
    run)     shift; run_range_sequential "$@" ;;
    ""|-h|--help|help) usage ;;
    *) echo "Unknown command: $cmd" >&2; usage; exit 1 ;;
  esac
}

main "$@"
EOF
chmod +x ~/server_swe_batch.sh