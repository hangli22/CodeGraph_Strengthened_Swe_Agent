from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


# =========================
# Config
# =========================

DEFAULT_OUTPUT_DIR = Path("./results/retrieval_lihang_11")
DEFAULT_SLICE = "10:30"

LOGS_ROOT = Path("./logs/run_evaluation")

# 全服务器共享锁。
# 目的：允许多个 server_swe_batch.sh / run_and_analyse.py 并行跑 agent，
# 但只允许一个进程同时进入 SWE-bench harness evaluation。
#
# 可通过环境变量覆盖：
#   export SWEBENCH_EVAL_LOCK_PATH=/tmp/swebench_eval.lock
EVAL_LOCK_PATH = Path(os.environ.get("SWEBENCH_EVAL_LOCK_PATH", "/tmp/swebench_eval.lock"))

BATCH_CMD = [
    sys.executable,
    "mini_swe_agent_integration/run_swebench_batch.py",
    "--mode", "retrieval",
    "--llm_backend", "uni",
    "--model_name", "openai/deepseek-v4-flash",
    "--subset", "lite",
    "--split", "test",
    "--slice", "10:30",
    "--output_dir", str(DEFAULT_OUTPUT_DIR),
    "--repos_dir", "./repos",
    "--cache_dir", "./cache",
    "--workers", "1",
    "--step_limit", "60",
    "--use_docker",
    "--docker_image", "sweagent-multipy:latest",
    "--redo",
]


# =========================
# Helpers
# =========================

def shell_quote_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--batch-cmd",
        default=None,
        help="完整的 run_swebench_batch.py 命令。传入后会覆盖脚本内默认 BATCH_CMD。",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="可选。手动指定 swebench harness 的 run_id；不指定则根据 output_dir 和 slice 自动生成。",
    )
    parser.add_argument(
        "--running-log",
        default="running.md",
        help="batch 运行日志输出文件，默认 running.md。",
    )
    parser.add_argument(
        "--analyse-log",
        default="annlyse_result.md",
        help="评测与逐条分析输出文件，默认 annlyse_result.md。",
    )
    parser.add_argument(
        "--eval-lock-path",
        default=None,
        help=(
            "SWE-bench harness evaluation 的全服务器锁文件路径。"
            "默认读取环境变量 SWEBENCH_EVAL_LOCK_PATH；否则使用 /tmp/swebench_eval.lock。"
        ),
    )
    parser.add_argument(
        "--no-eval-lock",
        action="store_true",
        help="禁用 evaluation 全局锁。不建议在同一服务器多进程评测时使用。",
    )
    return parser.parse_args()


def split_shell_command(command: str) -> list[str]:
    return shlex.split(command)


def get_option_value(cmd: list[str], option: str, default: str | None = None) -> str | None:
    """
    从命令列表中解析形如：
      --output_dir xxx
      --slice 10:30

    也兼容：
      --output_dir=xxx
      --slice=10:30
    """
    for i, token in enumerate(cmd):
        if token == option and i + 1 < len(cmd):
            return cmd[i + 1]

        prefix = option + "="
        if token.startswith(prefix):
            return token[len(prefix):]

    return default


def normalize_slice_name(slice_value: str | None) -> str:
    """
    10:30 -> 10_30
    0:10  -> 0_10
    unknown -> unknown
    """
    if not slice_value:
        return "unknown"
    return slice_value.replace(":", "_").replace("/", "_").replace("\\", "_")


def make_run_id(output_dir: Path, slice_value: str | None) -> str:
    """
    根据 output_dir 和 slice 自动生成较稳定的 run_id。
    例如：
      output_dir=./results/retrieval_lihang_11
      slice=10:30
    得到：
      retrieval_lihang_11_10_30
    """
    output_name = output_dir.name
    slice_name = normalize_slice_name(slice_value)
    return f"{output_name}_{slice_name}"


def build_eval_cmd(preds_path: Path, run_id: str) -> list[str]:
    return [
        sys.executable,
        "-m", "swebench.harness.run_evaluation",
        "--dataset_name", "princeton-nlp/SWE-bench_Lite",
        "--split", "test",
        "--predictions_path", str(preds_path),
        "--max_workers", "1",
        "--run_id", run_id,
    ]


def write_header(path: Path, title: str, cmd: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {title}\n\n"
        f"- Time: `{datetime.now().isoformat(timespec='seconds')}`\n"
        f"- Command:\n\n"
        "```bash\n"
        f"{shell_quote_cmd(cmd)}\n"
        "```\n\n"
        "## Output\n\n"
        "```text\n",
        encoding="utf-8",
    )


def append_text(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", errors="replace") as f:
        f.write(text)


def close_code_block(path: Path) -> None:
    append_text(path, "\n```\n")


def run_and_log(
    cmd: list[str],
    log_path: Path,
    title: str,
    pre_output_text: str = "",
) -> int:
    write_header(log_path, title, cmd)

    if pre_output_text:
        append_text(log_path, pre_output_text)
        if not pre_output_text.endswith("\n"):
            append_text(log_path, "\n")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    assert proc.stdout is not None
    for line in proc.stdout:
        append_text(log_path, line)

    returncode = proc.wait()
    close_code_block(log_path)

    append_text(
        log_path,
        f"\n## Exit status\n\n"
        f"```text\n{returncode}\n```\n"
    )
    return returncode


@contextmanager
def evaluation_lock(lock_path: Path):
    """
    服务器级互斥锁。

    这个锁只保护 SWE-bench harness evaluation 阶段。
    不锁 agent batch 阶段，避免把多个进程完全串行化。

    为什么用 fcntl.flock：
    - Linux 原生可用；
    - 同一台服务器所有项目目录共享同一个锁文件；
    - 进程异常退出时，文件锁会随 fd 关闭自动释放。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as f:
        f.write(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"pid={os.getpid()} waiting for evaluation lock\n"
        )
        f.flush()

        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

        f.write(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"pid={os.getpid()} acquired evaluation lock\n"
        )
        f.flush()

        try:
            yield
        finally:
            f.write(
                f"[{datetime.now().isoformat(timespec='seconds')}] "
                f"pid={os.getpid()} released evaluation lock\n"
            )
            f.flush()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_preds(preds_path: Path):
    if not preds_path.exists():
        raise FileNotFoundError(f"preds.json not found: {preds_path}")
    return json.loads(preds_path.read_text(encoding="utf-8"))


def normalize_pred_items(preds) -> dict:
    """
    支持两种常见格式：
    1. {instance_id: {...}}
    2. [{instance_id: ..., model_patch: ...}, ...]
    """
    if isinstance(preds, dict):
        return preds

    if isinstance(preds, list):
        return {
            item["instance_id"]: item
            for item in preds
            if isinstance(item, dict) and "instance_id" in item
        }

    raise ValueError(f"Unsupported preds format: {type(preds)}")


def collect_eval_status_from_json(
    obj,
    resolved: set[str],
    failed: set[str],
    errored: set[str],
) -> None:
    """
    尽量兼容 swebench 不同版本的 report/results json 结构。
    """
    if isinstance(obj, dict):
        # 结构 1：{"resolved": ["id1", "id2"], "unresolved": [...]}
        for key, value in obj.items():
            lk = str(key).lower()

            if lk in {"resolved", "resolved_ids", "pass_ids", "passed_ids"}:
                if isinstance(value, list):
                    resolved.update(map(str, value))
                elif isinstance(value, dict):
                    resolved.update(map(str, value.keys()))

            elif lk in {"unresolved", "unresolved_ids", "failed", "fail_ids", "failed_ids"}:
                if isinstance(value, list):
                    failed.update(map(str, value))
                elif isinstance(value, dict):
                    failed.update(map(str, value.keys()))

            elif lk in {"error", "errors", "error_ids", "errored", "errored_ids"}:
                if isinstance(value, list):
                    errored.update(map(str, value))
                elif isinstance(value, dict):
                    errored.update(map(str, value.keys()))

        # 结构 2：{"instance_id": "...", "resolved": true/false}
        iid = obj.get("instance_id")
        if isinstance(iid, str):
            if obj.get("resolved") is True:
                resolved.add(iid)
            elif obj.get("resolved") is False:
                failed.add(iid)

            status = str(obj.get("status", "")).lower()
            if status in {"resolved", "passed", "pass"}:
                resolved.add(iid)
            elif status in {"failed", "fail", "unresolved"}:
                failed.add(iid)
            elif status in {"error", "errored"}:
                errored.add(iid)

        # 结构 3：{"django__xxx": {"resolved": true}}
        for key, value in obj.items():
            if isinstance(key, str) and "__" in key and isinstance(value, dict):
                if value.get("resolved") is True:
                    resolved.add(key)
                elif value.get("resolved") is False:
                    failed.add(key)

        for value in obj.values():
            collect_eval_status_from_json(value, resolved, failed, errored)

    elif isinstance(obj, list):
        for item in obj:
            collect_eval_status_from_json(item, resolved, failed, errored)


def parse_eval_jsons(run_id: str) -> tuple[set[str], set[str], set[str], list[Path]]:
    resolved: set[str] = set()
    failed: set[str] = set()
    errored: set[str] = set()

    json_files = [
        p for p in LOGS_ROOT.rglob("*.json")
        if run_id in str(p)
    ]

    for path in json_files:
        try:
            obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        collect_eval_status_from_json(obj, resolved, failed, errored)

    return resolved, failed, errored, json_files


def infer_from_test_output(instance_id: str, run_id: str) -> str | None:
    """
    兜底：如果没能从 json 解析到状态，就从 test_output.txt 粗略判断。
    注意这只是兜底，不如 harness report 准确。
    """
    candidates = [
        p for p in LOGS_ROOT.rglob("test_output.txt")
        if run_id in str(p) and instance_id in str(p)
    ]
    if not candidates:
        return None

    text = candidates[0].read_text(encoding="utf-8", errors="replace")
    lowered = text.lower()

    if "resolved" in lowered and "unresolved" not in lowered:
        return "RESOLVED_MAYBE"

    if any(x in text for x in ["FAILURES", "FAILED", "ERROR", "Traceback"]):
        return "FAILED_OR_ERROR"

    return "RAN"


def append_per_instance_summary(
    preds_path: Path,
    output_dir: Path,
    run_id: str,
    analyse_log: Path,
) -> None:
    preds = normalize_pred_items(load_preds(preds_path))

    resolved, failed, errored, json_files = parse_eval_jsons(run_id)

    rows = []
    for iid in sorted(preds):
        item = preds[iid]
        patch = item.get("model_patch", "") or ""

        if not patch.strip():
            patch_status = "EMPTY_PATCH"
            eval_status = "EMPTY_PATCH"
        else:
            patch_status = "NON_EMPTY"
            if iid in resolved:
                eval_status = "RESOLVED"
            elif iid in failed:
                eval_status = "FAILED"
            elif iid in errored:
                eval_status = "ERROR"
            else:
                eval_status = infer_from_test_output(iid, run_id) or "UNKNOWN"

        rows.append({
            "instance_id": iid,
            "patch_status": patch_status,
            "eval_status": eval_status,
        })

    counts_patch = Counter(row["patch_status"] for row in rows)
    counts_eval = Counter(row["eval_status"] for row in rows)

    append_text(
        analyse_log,
        "\n\n# Per-instance evaluation summary\n\n"
        f"- Time: `{datetime.now().isoformat(timespec='seconds')}`\n"
        f"- preds: `{preds_path}`\n"
        f"- run_id: `{run_id}`\n"
        f"- parsed_json_files: `{len(json_files)}`\n\n"
    )

    if json_files:
        append_text(analyse_log, "## Parsed harness JSON files\n\n")
        for p in sorted(json_files):
            append_text(analyse_log, f"- `{p}`\n")
        append_text(analyse_log, "\n")

    append_text(analyse_log, "## Summary\n\n")
    append_text(analyse_log, "```text\n")
    append_text(analyse_log, f"patch_status: {dict(counts_patch)}\n")
    append_text(analyse_log, f"eval_status : {dict(counts_eval)}\n")
    append_text(analyse_log, "```\n\n")

    append_text(analyse_log, "## Per-instance table\n\n")
    append_text(analyse_log, "| instance_id | patch_status | eval_status |\n")
    append_text(analyse_log, "|---|---:|---:|\n")
    for row in rows:
        append_text(
            analyse_log,
            f"| `{row['instance_id']}` | {row['patch_status']} | {row['eval_status']} |\n"
        )

    csv_path = output_dir / "eval_summary.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8") as f:
        f.write("instance_id,patch_status,eval_status\n")
        for row in rows:
            f.write(f"{row['instance_id']},{row['patch_status']},{row['eval_status']}\n")

    append_text(
        analyse_log,
        f"\n\nCSV also written to: `{csv_path}`\n"
    )


def main() -> int:
    args = parse_args()

    batch_cmd = BATCH_CMD
    if args.batch_cmd:
        batch_cmd = split_shell_command(args.batch_cmd)

    output_dir_value = get_option_value(batch_cmd, "--output_dir", str(DEFAULT_OUTPUT_DIR))
    slice_value = get_option_value(batch_cmd, "--slice", DEFAULT_SLICE)

    if output_dir_value is None:
        output_dir = DEFAULT_OUTPUT_DIR
    else:
        output_dir = Path(output_dir_value)

    preds_path = output_dir / "preds.json"

    run_id = args.run_id or make_run_id(output_dir, slice_value)

    running_log = Path(args.running_log)
    analyse_log = Path(args.analyse_log)

    eval_cmd = build_eval_cmd(preds_path, run_id)

    if args.eval_lock_path:
        eval_lock_path = Path(args.eval_lock_path)
    else:
        eval_lock_path = EVAL_LOCK_PATH

    print("Resolved config:")
    print(f"- slice        : {slice_value}")
    print(f"- output_dir   : {output_dir}")
    print(f"- preds_path   : {preds_path}")
    print(f"- run_id       : {run_id}")
    print(f"- running_log  : {running_log}")
    print(f"- analyse_log  : {analyse_log}")
    print(f"- eval_lock    : {'disabled' if args.no_eval_lock else eval_lock_path}")

    print("\nStep 1/3: running SWE-agent batch. Output ->", running_log)
    batch_code = run_and_log(batch_cmd, running_log, "SWE-agent batch running log")

    if not preds_path.exists():
        append_text(
            running_log,
            f"\n\n## ERROR\n\n`{preds_path}` was not generated. Stop before evaluation.\n"
        )
        print(f"ERROR: preds.json not found: {preds_path}")
        return batch_code or 1

    print("Step 2/3: running SWE-bench harness evaluation. Output ->", analyse_log)

    eval_prelude = (
        f"[run_and_analyse] pid={os.getpid()}\n"
        f"[run_and_analyse] eval_lock={'disabled' if args.no_eval_lock else str(eval_lock_path)}\n"
        f"[run_and_analyse] started_eval_step_at={datetime.now().isoformat(timespec='seconds')}\n"
    )

    if args.no_eval_lock:
        eval_code = run_and_log(
            eval_cmd,
            analyse_log,
            "SWE-bench evaluation log",
            pre_output_text=eval_prelude,
        )
    else:
        print(f"Waiting for SWE-bench evaluation lock: {eval_lock_path}")
        with evaluation_lock(eval_lock_path):
            print(f"Acquired SWE-bench evaluation lock: {eval_lock_path}")
            eval_code = run_and_log(
                eval_cmd,
                analyse_log,
                "SWE-bench evaluation log",
                pre_output_text=(
                    eval_prelude
                    + f"[run_and_analyse] acquired_eval_lock_at={datetime.now().isoformat(timespec='seconds')}\n"
                ),
            )
        print(f"Released SWE-bench evaluation lock: {eval_lock_path}")

    print("Step 3/3: appending per-instance summary ->", analyse_log)
    append_per_instance_summary(
        preds_path=preds_path,
        output_dir=output_dir,
        run_id=run_id,
        analyse_log=analyse_log,
    )

    print("Done.")
    print(f"- Running log: {running_log}")
    print(f"- Analyse log: {analyse_log}")
    print(f"- CSV summary: {output_dir / 'eval_summary.csv'}")

    return eval_code


if __name__ == "__main__":
    raise SystemExit(main())