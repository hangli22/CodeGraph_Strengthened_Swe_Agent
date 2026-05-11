"""
prepare_repos.py — 提前下载 SWE-bench instances 对应的 Git 仓库

用途：
  只提前准备 repos_dir/instance_id 目录，不构建代码图，不生成 embedding cache。

设计：
  - 每个 instance 一个独立仓库目录：repos_dir / instance_id
  - 如果目录不存在：git clone + checkout base_commit + clean
  - 如果目录已存在且是 Git 仓库：checkout base_commit + clean
  - 如果目录存在但不是 Git 仓库：默认报错；可用 --delete_invalid_repo 删除后重 clone
  - 不修改 run_swebench_batch.py

示例：
  python mini_swe_agent_integration/prepare_repos.py \
    --subset lite \
    --split test \
    --slice 0:30 \
    --repos_dir ./repos \
    --workers 1

之后运行：
  python mini_swe_agent_integration/run_swebench_batch.py \
    --mode retrieval \
    --subset lite \
    --split test \
    --slice 0:30 \
    --repos_dir ./repos \
    --cache_dir ./cache \
    ...
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


DATASET_MAPPING = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
    "full": "princeton-nlp/SWE-bench",
}


def parse_slice(slice_text: str, n: int) -> tuple[int, int]:
    """
    解析 --slice，例如：
      0:30
      10:20
      :50
      50:
    """
    if not slice_text:
        return 0, n

    if ":" not in slice_text:
        idx = int(slice_text)
        return idx, min(idx + 1, n)

    left, right = slice_text.split(":", 1)

    start = int(left) if left.strip() else 0
    end = int(right) if right.strip() else n

    start = max(0, start)
    end = min(n, end)

    if start > end:
        raise ValueError(f"无效 slice: {slice_text}")

    return start, end


def load_instances(
    subset: str,
    split: str,
    slice_text: str = "",
    instance_ids_file: str = "",
) -> list[dict[str, Any]]:
    """
    加载 SWE-bench 数据集，并按 slice 或 instance_ids_file 过滤。
    """
    from datasets import load_dataset

    dataset_name = DATASET_MAPPING.get(subset, subset)

    logger.info("加载数据集: %s split=%s", dataset_name, split)
    ds = load_dataset(dataset_name, split=split)
    instances = list(ds)

    if instance_ids_file:
        wanted = {
            line.strip()
            for line in Path(instance_ids_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        before = len(instances)
        instances = [x for x in instances if x.get("instance_id") in wanted]
        logger.info(
            "按 instance_ids_file 过滤: %d -> %d",
            before,
            len(instances),
        )
        return instances

    start, end = parse_slice(slice_text, len(instances))
    selected = instances[start:end]
    logger.info("使用 slice %s -> [%d:%d]，共 %d 个 instance", slice_text or "全部", start, end, len(selected))
    return selected


def safe_rmtree(path: str) -> None:
    """
    删除目录，尽量处理只读文件。
    """
    if not os.path.exists(path):
        return

    def onerror(func, p, exc_info):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=onerror)


def run_cmd(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """
    执行命令，失败时抛出带 stdout/stderr 的异常。
    """
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"命令失败: {' '.join(cmd)}\n"
            f"cwd: {cwd or os.getcwd()}\n"
            f"returncode: {result.returncode}\n"
            f"stdout:\n{result.stdout[-2000:]}\n"
            f"stderr:\n{result.stderr[-2000:]}"
        )

    return result


def run_with_retry(
    cmd: list[str],
    cwd: str | None = None,
    max_retries: int = 5,
    retry_delay: float = 5.0,
    timeout: int | None = None,
    cleanup_path: str = "",
) -> subprocess.CompletedProcess:
    """
    带重试执行命令。clone 网络不稳定时很有用。
    """
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("运行命令，第 %d/%d 次：%s", attempt, max_retries, " ".join(cmd))
            return run_cmd(cmd, cwd=cwd, timeout=timeout)
        except Exception as e:
            last_error = e
            logger.warning(
                "命令失败，第 %d/%d 次：%s\n%s",
                attempt,
                max_retries,
                " ".join(cmd),
                e,
            )

            if cleanup_path:
                safe_rmtree(cleanup_path)

            if attempt < max_retries:
                sleep_s = retry_delay * attempt
                logger.info("等待 %.1f 秒后重试", sleep_s)
                time.sleep(sleep_s)

    raise RuntimeError(f"命令重试 {max_retries} 次后仍失败: {' '.join(cmd)}") from last_error


def is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def get_current_commit(repo_path: str) -> str:
    result = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_path)
    return result.stdout.strip()


def prepare_one_repo(
    instance: dict[str, Any],
    repos_dir: str,
    *,
    delete_invalid_repo: bool = False,
    force_reclone: bool = False,
    max_retries: int = 5,
    retry_delay: float = 5.0,
    clone_timeout: int | None = None,
    no_filter_blob_none: bool = False,
) -> dict[str, Any]:
    """
    准备单个 instance 的 repo。

    返回 summary dict。
    """
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]

    repo_path = os.path.abspath(os.path.join(repos_dir, instance_id))
    git_dir = os.path.join(repo_path, ".git")

    summary = {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "repo_path": repo_path,
        "status": "",
        "error": "",
    }

    try:
        if force_reclone and os.path.exists(repo_path):
            logger.info("[%s] --force_reclone: 删除已有目录 %s", instance_id, repo_path)
            safe_rmtree(repo_path)

        if os.path.exists(repo_path) and not os.path.exists(git_dir):
            msg = f"[{instance_id}] repo_path 已存在但不是 Git 仓库: {repo_path}"
            if not delete_invalid_repo:
                raise RuntimeError(
                    msg
                    + "\n如确认可删除，请加 --delete_invalid_repo。"
                )

            logger.warning("%s，将删除后重新 clone", msg)
            safe_rmtree(repo_path)

        if os.path.exists(git_dir):
            logger.info("[%s] 仓库已存在，复用并 checkout %s", instance_id, base_commit[:8])

            run_with_retry(
                ["git", "checkout", "-f", base_commit],
                cwd=repo_path,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )
            run_cmd(["git", "clean", "-fdx"], cwd=repo_path)

            current = get_current_commit(repo_path)
            if current != base_commit:
                raise RuntimeError(
                    f"checkout 后 commit 不匹配: current={current}, expected={base_commit}"
                )

            summary["status"] = "reused"
            return summary

        os.makedirs(repos_dir, exist_ok=True)

        clone_url = f"https://github.com/{repo}.git"
        logger.info("[%s] 开始 clone: %s -> %s", instance_id, clone_url, repo_path)

        clone_cmd = ["git", "clone"]
        if not no_filter_blob_none:
            clone_cmd.append("--filter=blob:none")
        clone_cmd.extend([clone_url, repo_path])

        run_with_retry(
            clone_cmd,
            max_retries=max_retries,
            retry_delay=retry_delay,
            timeout=clone_timeout,
            cleanup_path=repo_path,
        )

        run_with_retry(
            ["git", "checkout", "-f", base_commit],
            cwd=repo_path,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )

        run_cmd(["git", "clean", "-fdx"], cwd=repo_path)

        current = get_current_commit(repo_path)
        if current != base_commit:
            raise RuntimeError(
                f"checkout 后 commit 不匹配: current={current}, expected={base_commit}"
            )

        summary["status"] = "cloned"
        return summary

    except Exception as e:
        logger.exception("[%s] repo 准备失败", instance_id)
        summary["status"] = "failed"
        summary["error"] = f"{type(e).__name__}: {e}"
        return summary


def write_manifest(
    output_path: str,
    args: argparse.Namespace,
    results: list[dict[str, Any]],
) -> None:
    manifest = {
        "subset": args.subset,
        "split": args.split,
        "slice": args.slice,
        "instance_ids_file": args.instance_ids_file,
        "repos_dir": os.path.abspath(args.repos_dir),
        "total": len(results),
        "cloned": sum(1 for r in results if r["status"] == "cloned"),
        "reused": sum(1 for r in results if r["status"] == "reused"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    logger.info("manifest 已写入: %s", output_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="提前 clone/checkout SWE-bench instances 的 repos，供 run_swebench_batch.py 复用。"
    )

    parser.add_argument(
        "--subset",
        default="lite",
        choices=["lite", "verified", "full"],
        help="SWE-bench 子集，默认 lite",
    )
    parser.add_argument(
        "--split",
        default="test",
        help="数据集 split，默认 test",
    )
    parser.add_argument(
        "--slice",
        default="",
        help="实例切片，例如 0:30、30:60。不填表示全部。",
    )
    parser.add_argument(
        "--instance_ids_file",
        default="",
        help="可选。每行一个 instance_id；若提供，则优先按文件过滤，而不是 --slice。",
    )
    parser.add_argument(
        "--repos_dir",
        default="./repos",
        help="仓库存放目录。每个 instance 会放到 repos_dir/instance_id。",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发 clone 数。网络/磁盘压力较大，建议 1 或 2。",
    )
    parser.add_argument(
        "--max_retries",
        type=int,
        default=5,
        help="git clone/checkout 最大重试次数。",
    )
    parser.add_argument(
        "--retry_delay",
        type=float,
        default=5.0,
        help="重试基础等待秒数，会随 attempt 线性增加。",
    )
    parser.add_argument(
        "--clone_timeout",
        type=int,
        default=0,
        help="git clone 超时秒数。0 表示不设置。",
    )
    parser.add_argument(
        "--delete_invalid_repo",
        action="store_true",
        help="如果 repos_dir/instance_id 存在但不是 Git 仓库，则删除后重 clone。",
    )
    parser.add_argument(
        "--force_reclone",
        action="store_true",
        help="无论仓库是否存在，都删除后重新 clone。",
    )
    parser.add_argument(
        "--no_filter_blob_none",
        action="store_true",
        help="不使用 git clone --filter=blob:none。某些网络/代理环境不兼容 partial clone 时可开启。",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="manifest 输出路径。默认写到 repos_dir/prepare_repos_manifest.json。",
    )

    args = parser.parse_args()

    instances = load_instances(
        subset=args.subset,
        split=args.split,
        slice_text=args.slice,
        instance_ids_file=args.instance_ids_file,
    )

    if not instances:
        raise RuntimeError("没有选中任何 instance。请检查 --slice 或 --instance_ids_file。")

    os.makedirs(args.repos_dir, exist_ok=True)

    clone_timeout = args.clone_timeout if args.clone_timeout and args.clone_timeout > 0 else None

    logger.info("准备 repos: count=%d repos_dir=%s workers=%d", len(instances), args.repos_dir, args.workers)

    results: list[dict[str, Any]] = []

    if args.workers <= 1:
        for inst in instances:
            results.append(
                prepare_one_repo(
                    inst,
                    args.repos_dir,
                    delete_invalid_repo=args.delete_invalid_repo,
                    force_reclone=args.force_reclone,
                    max_retries=args.max_retries,
                    retry_delay=args.retry_delay,
                    clone_timeout=clone_timeout,
                    no_filter_blob_none=args.no_filter_blob_none,
                )
            )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_id = {
                executor.submit(
                    prepare_one_repo,
                    inst,
                    args.repos_dir,
                    delete_invalid_repo=args.delete_invalid_repo,
                    force_reclone=args.force_reclone,
                    max_retries=args.max_retries,
                    retry_delay=args.retry_delay,
                    clone_timeout=clone_timeout,
                    no_filter_blob_none=args.no_filter_blob_none,
                ): inst["instance_id"]
                for inst in instances
            }

            for future in concurrent.futures.as_completed(future_to_id):
                instance_id = future_to_id[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.exception("[%s] unexpected failure", instance_id)
                    results.append({
                        "instance_id": instance_id,
                        "repo": "",
                        "base_commit": "",
                        "repo_path": "",
                        "status": "failed",
                        "error": f"{type(e).__name__}: {e}",
                    })

    results.sort(key=lambda x: x.get("instance_id", ""))

    manifest_path = args.manifest or os.path.join(args.repos_dir, "prepare_repos_manifest.json")
    write_manifest(manifest_path, args, results)

    cloned = sum(1 for r in results if r["status"] == "cloned")
    reused = sum(1 for r in results if r["status"] == "reused")
    failed = sum(1 for r in results if r["status"] == "failed")

    logger.info("完成: cloned=%d reused=%d failed=%d total=%d", cloned, reused, failed, len(results))

    if failed:
        logger.warning("存在失败项，请查看 manifest: %s", manifest_path)
        raise SystemExit(1)


if __name__ == "__main__":
    main()