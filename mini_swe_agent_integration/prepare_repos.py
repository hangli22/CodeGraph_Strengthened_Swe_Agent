"""
prepare_repos.py — 提前下载 SWE-bench instances 对应的 Git 仓库

用途：
  只提前准备 repos_dir/instance_id 目录，不构建代码图，不生成 embedding cache。

新增设计：
  - 支持把一个较大的 slice 自动拆成多个小 slice，例如 50:80 -> 50:60, 60:70, 70:80
  - 每个小 slice 下载到项目目录下的 repos_dir
  - 下载完成后移动到 archive_root/slice_START_END
  - 如果目标 slice_START_END 目录已存在且非空，则跳过该小 slice
  - 每个小 slice 结束后都会清空项目目录下的 repos_dir，避免不同 slice 混在一起

默认行为：
  - 如果使用 --slice 且没有使用 --instance_ids_file，则默认启用分段归档模式
  - 默认每段大小为 10
  - 默认归档到 ~/save/repos

示例：
  python mini_swe_agent_integration/prepare_repos.py \
    --subset lite \
    --split test \
    --slice 50:80 \
    --repos_dir ./repos \
    --workers 1

等价于依次准备：
  50:60 -> ~/save/repos/slice_50_60
  60:70 -> ~/save/repos/slice_60_70
  70:80 -> ~/save/repos/slice_70_80

如果想保留旧行为，即只下载到 ./repos，不归档、不清空：
  python mini_swe_agent_integration/prepare_repos.py \
    --subset lite \
    --split test \
    --slice 50:60 \
    --repos_dir ./repos \
    --workers 1 \
    --no_archive_slices
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
      0
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


def load_all_instances(subset: str, split: str) -> list[dict[str, Any]]:
    """
    加载完整 SWE-bench 数据集。
    """
    from datasets import load_dataset

    dataset_name = DATASET_MAPPING.get(subset, subset)

    logger.info("加载数据集: %s split=%s", dataset_name, split)
    ds = load_dataset(dataset_name, split=split)
    return list(ds)


def select_instances(
    all_instances: list[dict[str, Any]],
    slice_text: str = "",
    instance_ids_file: str = "",
) -> list[dict[str, Any]]:
    """
    从完整 instances 中按 slice 或 instance_ids_file 过滤。
    """
    instances = list(all_instances)

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
    logger.info(
        "使用 slice %s -> [%d:%d]，共 %d 个 instance",
        slice_text or "全部",
        start,
        end,
        len(selected),
    )
    return selected


def load_instances(
    subset: str,
    split: str,
    slice_text: str = "",
    instance_ids_file: str = "",
) -> list[dict[str, Any]]:
    """
    兼容旧调用：加载数据集，并按 slice 或 instance_ids_file 过滤。
    """
    all_instances = load_all_instances(subset, split)
    return select_instances(
        all_instances,
        slice_text=slice_text,
        instance_ids_file=instance_ids_file,
    )


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


def ensure_empty_dir(path: str) -> None:
    """
    保证目录存在且为空。
    """
    safe_rmtree(path)
    os.makedirs(path, exist_ok=True)


def is_dir_nonempty(path: str) -> bool:
    """
    判断目录是否存在且非空。
    """
    return os.path.isdir(path) and any(os.scandir(path))


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
    *,
    slice_text: str | None = None,
    archive_dir: str | None = None,
) -> None:
    manifest = {
        "subset": args.subset,
        "split": args.split,
        "slice": slice_text if slice_text is not None else args.slice,
        "instance_ids_file": args.instance_ids_file,
        "repos_dir": os.path.abspath(args.repos_dir),
        "archive_dir": os.path.abspath(archive_dir) if archive_dir else "",
        "total": len(results),
        "cloned": sum(1 for r in results if r["status"] == "cloned"),
        "reused": sum(1 for r in results if r["status"] == "reused"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "results": results,
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    logger.info("manifest 已写入: %s", output_path)


def prepare_instances(
    instances: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    clone_timeout: int | None,
) -> list[dict[str, Any]]:
    """
    准备一组 instances 到 args.repos_dir。
    """
    logger.info(
        "准备 repos: count=%d repos_dir=%s workers=%d",
        len(instances),
        args.repos_dir,
        args.workers,
    )

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
    return results


def move_repos_dir_contents_to_archive(repos_dir: str, archive_dir: str) -> None:
    """
    将 repos_dir 下的内容移动到 archive_dir。

    注意：
      - archive_dir 必须为空或不存在
      - 不移动 repos_dir 自身，只移动其中的 instance_id 子目录和 manifest 等文件
    """
    repos_abs = os.path.abspath(os.path.expanduser(repos_dir))
    archive_abs = os.path.abspath(os.path.expanduser(archive_dir))

    if not os.path.isdir(repos_abs):
        raise RuntimeError(f"repos_dir 不存在，无法归档: {repos_abs}")

    if os.path.exists(archive_abs) and is_dir_nonempty(archive_abs):
        raise RuntimeError(f"archive_dir 已存在且非空，拒绝覆盖: {archive_abs}")

    os.makedirs(archive_abs, exist_ok=True)

    for name in os.listdir(repos_abs):
        src = os.path.join(repos_abs, name)
        dst = os.path.join(archive_abs, name)

        if os.path.exists(dst):
            raise RuntimeError(f"目标路径已存在，拒绝覆盖: {dst}")

        shutil.move(src, dst)

    logger.info("已移动 repos 内容: %s -> %s", repos_abs, archive_abs)


def validate_archive_not_inside_repos(repos_dir: str, archive_root: str) -> None:
    """
    防止把 archive_root 放在 repos_dir 里面。
    否则清空 repos_dir 时可能误删归档。
    """
    repos_abs = os.path.abspath(os.path.expanduser(repos_dir))
    archive_abs = os.path.abspath(os.path.expanduser(archive_root))

    try:
        common = os.path.commonpath([repos_abs, archive_abs])
    except ValueError:
        return

    if common == repos_abs:
        raise RuntimeError(
            "archive_root 不能放在 repos_dir 里面，否则清空 repos_dir 时可能误删归档。\n"
            f"repos_dir: {repos_abs}\n"
            f"archive_root: {archive_abs}"
        )


def iter_chunks(start: int, end: int, chunk_size: int) -> list[tuple[int, int]]:
    """
    生成 [start:end] 内按 chunk_size 切分的小段。
    """
    if chunk_size <= 0:
        raise ValueError("--chunk_size 必须大于 0")

    chunks: list[tuple[int, int]] = []
    cur = start
    while cur < end:
        nxt = min(cur + chunk_size, end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks


def run_archive_slices_mode(
    args: argparse.Namespace,
    *,
    clone_timeout: int | None,
) -> None:
    """
    分段准备并归档 repos。

    例如 --slice 50:80 --chunk_size 10：
      50:60 -> archive_root/slice_50_60
      60:70 -> archive_root/slice_60_70
      70:80 -> archive_root/slice_70_80
    """
    if args.instance_ids_file:
        raise RuntimeError(
            "分段归档模式不支持 --instance_ids_file。\n"
            "如果要使用 instance_ids_file，请加 --no_archive_slices 使用旧模式。"
        )

    all_instances = load_all_instances(args.subset, args.split)
    start, end = parse_slice(args.slice, len(all_instances))

    if start == end:
        raise RuntimeError(f"没有选中任何 instance。请检查 --slice: {args.slice}")

    archive_root = os.path.abspath(os.path.expanduser(args.archive_root))
    repos_dir = os.path.abspath(os.path.expanduser(args.repos_dir))

    validate_archive_not_inside_repos(repos_dir, archive_root)

    os.makedirs(archive_root, exist_ok=True)

    chunks = iter_chunks(start, end, args.chunk_size)
    logger.info(
        "启用分段归档模式: slice=[%d:%d] chunk_size=%d archive_root=%s，共 %d 段",
        start,
        end,
        args.chunk_size,
        archive_root,
        len(chunks),
    )

    all_chunk_results: list[dict[str, Any]] = []
    failed_any = False

    for chunk_start, chunk_end in chunks:
        slice_text = f"{chunk_start}:{chunk_end}"
        archive_dir = os.path.join(archive_root, f"slice_{chunk_start}_{chunk_end}")

        logger.info("=" * 80)
        logger.info("处理 slice %s -> %s", slice_text, archive_dir)

        if is_dir_nonempty(archive_dir):
            logger.info(
                "目标目录已存在且非空，跳过本段: %s",
                archive_dir,
            )
            all_chunk_results.append({
                "slice": slice_text,
                "archive_dir": archive_dir,
                "status": "skipped",
                "reason": "archive_dir exists and is non-empty",
            })
            continue

        # 每一段开始前，确保项目目录下 repos 是空的。
        logger.info("清空项目 repos_dir: %s", repos_dir)
        ensure_empty_dir(repos_dir)

        instances = all_instances[chunk_start:chunk_end]
        if not instances:
            logger.warning("slice %s 没有选中 instance，跳过", slice_text)
            all_chunk_results.append({
                "slice": slice_text,
                "archive_dir": archive_dir,
                "status": "skipped",
                "reason": "empty slice",
            })
            continue

        results = prepare_instances(
            instances,
            args,
            clone_timeout=clone_timeout,
        )

        failed = sum(1 for r in results if r["status"] == "failed")

        # 先把 manifest 写入 repos_dir，随后一起移动到 archive_dir。
        manifest_path = os.path.join(repos_dir, "prepare_repos_manifest.json")
        write_manifest(
            manifest_path,
            args,
            results,
            slice_text=slice_text,
            archive_dir=archive_dir,
        )

        if failed:
            failed_any = True
            logger.warning(
                "slice %s 存在失败项，不移动到归档目录。保留 repos_dir 供排查: %s",
                slice_text,
                repos_dir,
            )
            all_chunk_results.append({
                "slice": slice_text,
                "archive_dir": archive_dir,
                "status": "failed",
                "failed": failed,
                "manifest": manifest_path,
            })
            break

        # 如果 archive_dir 存在但为空，直接使用；如果不存在，创建。
        if os.path.exists(archive_dir) and not os.path.isdir(archive_dir):
            raise RuntimeError(f"archive_dir 已存在但不是目录: {archive_dir}")

        if os.path.exists(archive_dir) and not is_dir_nonempty(archive_dir):
            logger.info("目标目录已存在但为空，将使用该目录: %s", archive_dir)

        move_repos_dir_contents_to_archive(repos_dir, archive_dir)

        # 移动后再次清空 repos_dir，保证下一段从干净状态开始。
        logger.info("再次清空项目 repos_dir: %s", repos_dir)
        ensure_empty_dir(repos_dir)

        cloned = sum(1 for r in results if r["status"] == "cloned")
        reused = sum(1 for r in results if r["status"] == "reused")

        all_chunk_results.append({
            "slice": slice_text,
            "archive_dir": archive_dir,
            "status": "done",
            "cloned": cloned,
            "reused": reused,
            "failed": failed,
            "total": len(results),
        })

        logger.info(
            "slice %s 完成: cloned=%d reused=%d failed=%d total=%d archive=%s",
            slice_text,
            cloned,
            reused,
            failed,
            len(results),
            archive_dir,
        )

    summary_path = os.path.join(
        archive_root,
        f"prepare_repos_archive_summary_{start}_{end}.json",
    )
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "subset": args.subset,
                "split": args.split,
                "slice": f"{start}:{end}",
                "chunk_size": args.chunk_size,
                "repos_dir": repos_dir,
                "archive_root": archive_root,
                "chunks": all_chunk_results,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    logger.info("分段归档 summary 已写入: %s", summary_path)

    if failed_any:
        raise SystemExit(1)

    logger.info("全部分段处理完成")


def run_legacy_mode(
    args: argparse.Namespace,
    *,
    clone_timeout: int | None,
) -> None:
    """
    旧模式：只把 repos 准备到 args.repos_dir，不归档、不自动清空。
    """
    instances = load_instances(
        subset=args.subset,
        split=args.split,
        slice_text=args.slice,
        instance_ids_file=args.instance_ids_file,
    )

    if not instances:
        raise RuntimeError("没有选中任何 instance。请检查 --slice 或 --instance_ids_file。")

    os.makedirs(args.repos_dir, exist_ok=True)

    results = prepare_instances(
        instances,
        args,
        clone_timeout=clone_timeout,
    )

    manifest_path = args.manifest or os.path.join(args.repos_dir, "prepare_repos_manifest.json")
    write_manifest(manifest_path, args, results)

    cloned = sum(1 for r in results if r["status"] == "cloned")
    reused = sum(1 for r in results if r["status"] == "reused")
    failed = sum(1 for r in results if r["status"] == "failed")

    logger.info("完成: cloned=%d reused=%d failed=%d total=%d", cloned, reused, failed, len(results))

    if failed:
        logger.warning("存在失败项，请查看 manifest: %s", manifest_path)
        raise SystemExit(1)


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
        help="manifest 输出路径。旧模式默认写到 repos_dir/prepare_repos_manifest.json；分段归档模式会忽略该参数并把 manifest 写入每个归档目录。",
    )

    # 新增参数
    parser.add_argument(
        "--archive_root",
        default="~/save/repos",
        help="分段归档根目录，默认 ~/save/repos。每段会保存到 archive_root/slice_START_END。",
    )
    parser.add_argument(
        "--chunk_size",
        type=int,
        default=10,
        help="分段大小，默认 10。例如 --slice 50:80 会拆成 50:60、60:70、70:80。",
    )
    parser.add_argument(
        "--no_archive_slices",
        action="store_true",
        help="关闭分段归档模式，使用旧行为：只准备到 repos_dir，不移动到 archive_root，也不自动清空 repos_dir。",
    )

    args = parser.parse_args()

    clone_timeout = args.clone_timeout if args.clone_timeout and args.clone_timeout > 0 else None

    # 默认启用新模式：
    #   - 有 --slice
    #   - 没有 --instance_ids_file
    #   - 没有显式 --no_archive_slices
    #
    # 如果你想保留旧行为，加 --no_archive_slices。
    use_archive_slices = (
        bool(args.slice)
        and not args.instance_ids_file
        and not args.no_archive_slices
    )

    if use_archive_slices:
        run_archive_slices_mode(args, clone_timeout=clone_timeout)
    else:
        run_legacy_mode(args, clone_timeout=clone_timeout)


if __name__ == "__main__":
    main()