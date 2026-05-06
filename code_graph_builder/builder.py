"""
builder.py — 统一构建入口
支持两种模式：
  - skeleton_mode=True  → 骨架图（Prebuild 使用）
  - skeleton_mode=False → 完整图（原有行为）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Set

from .graph_schema import CodeGraph

logger = logging.getLogger(__name__)


@dataclass
class BuildConfig:
    enable_file_relations: bool = True
    enable_ast_relations:  bool = True
    enable_call_graph:     bool = True
    enable_inheritance:    bool = True
    enable_annotation:     bool = True
    skeleton_mode:         bool = False       # 新增：骨架模式
    llm_backend:           object = None
    annotation_config:     object = None
    exclude_dirs:          Set[str] = field(default_factory=set)
    save_path:             Optional[str] = None
    save_format:           str = "pickle"


class CodeGraphBuilder:
    def __init__(self, repo_root: str):
        import os
        self.repo_root = os.path.abspath(repo_root)

    def build(self, config: Optional[BuildConfig] = None) -> CodeGraph:
        if config is None:
            config = BuildConfig()

        if config.skeleton_mode:
            return self._build_skeleton(config)
        else:
            return self._build_full(config)

    # ------------------------------------------------------------------
    # 骨架模式
    # ------------------------------------------------------------------

    def _build_skeleton(self, config: BuildConfig) -> CodeGraph:
        from .skeleton_builder import SkeletonBuilder

        graph = CodeGraph(repo_root=self.repo_root)
        total_start = time.perf_counter()

        skeleton = SkeletonBuilder(self.repo_root, exclude_dirs=config.exclude_dirs)
        skeleton.build(graph)

        elapsed = time.perf_counter() - total_start
        stats = graph.stats()
        logger.info(
            "Skeleton CodeGraph built in %.2fs | nodes=%d edges=%d skeleton_files=%d",
            elapsed, stats["total_nodes"], stats["total_edges"], stats["skeleton_files"],
        )

        if config.save_path:
            if config.save_format == "json":
                graph.save_json(config.save_path)
            else:
                graph.save_pickle(config.save_path)
            logger.info("Graph saved to %s (%s)", config.save_path, config.save_format)

        return graph

    # ------------------------------------------------------------------
    # 完整模式（原有逻辑）
    # ------------------------------------------------------------------

    def _build_full(self, config: BuildConfig) -> CodeGraph:
        from .file_relations import FileRelationBuilder
        from .ast_relations import ASTRelationBuilder
        from .call_graph import CallGraphBuilder
        from .inheritance import InheritanceBuilder

        graph = CodeGraph(repo_root=self.repo_root)
        total_start = time.perf_counter()

        if config.enable_file_relations:
            self._run_step(
                "FileRelationBuilder",
                FileRelationBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )
        if config.enable_ast_relations:
            self._run_step(
                "ASTRelationBuilder",
                ASTRelationBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )
        if config.enable_call_graph:
            self._run_step(
                "CallGraphBuilder",
                CallGraphBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )
        if config.enable_inheritance:
            self._run_step(
                "InheritanceBuilder",
                InheritanceBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )
        if config.enable_annotation:
            self._run_annotation(graph, config)

        # 所有文件标记为 full
        for node in graph.iter_nodes(node_type=None):
            if node.type.value == "MODULE":
                graph.set_file_depth(node.file, "full")

        elapsed = time.perf_counter() - total_start
        stats = graph.stats()
        logger.info(
            "CodeGraph built in %.2fs | nodes=%d  edges=%d",
            elapsed, stats["total_nodes"], stats["total_edges"],
        )

        if config.save_path:
            if config.save_format == "json":
                graph.save_json(config.save_path)
            else:
                graph.save_pickle(config.save_path)
            logger.info("Graph saved to %s (%s)", config.save_path, config.save_format)

        return graph

    @staticmethod
    def _run_step(name: str, builder_obj, graph: CodeGraph) -> None:
        t0 = time.perf_counter()
        builder_obj.build(graph)
        elapsed = time.perf_counter() - t0
        stats = graph.stats()
        logger.info(
            "[%s] done in %.2fs | nodes=%d  edges=%d",
            name, elapsed, stats["total_nodes"], stats["total_edges"],
        )

    @staticmethod
    def _run_annotation(graph: CodeGraph, config: BuildConfig) -> None:
        from .comment_annotator import CommentAnnotator, AnnotatorConfig, get_default_backend
        backend = config.llm_backend or get_default_backend(verbose=True)
        t0        = time.perf_counter()
        annotator = CommentAnnotator(backend=backend)
        ann_cfg   = config.annotation_config or AnnotatorConfig()
        result    = annotator.annotate(graph, config=ann_cfg)
        elapsed   = time.perf_counter() - t0
        logger.info("[CommentAnnotator] done in %.2fs | %s", elapsed, result)