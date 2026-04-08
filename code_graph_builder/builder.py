"""
builder.py — 统一构建入口
对应论文 3.2 整体：多层代码图构建

职责
----
按顺序调用五个子模块，完整构建多层代码图：
  Step 1 (3.2.2) FileRelationBuilder  — MODULE 节点 + IMPORTS 边
  Step 2 (3.2.3) ASTRelationBuilder   — CLASS/FUNCTION/METHOD 节点 + CONTAINS/PARENT_CHILD/SIBLING 边
  Step 3 (3.2.4) CallGraphBuilder     — CALLS 边
  Step 4 (3.2.5) InheritanceBuilder   — INHERITS + OVERRIDES 边

每一步的输出作为下一步的输入，所有结果统一写入同一个 CodeGraph 对象。

对外暴露两个接口：
  build()        — 完整构建并返回 CodeGraph
  build_partial()— 按开关选择性构建（用于消融实验 4.3.3）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Set

from .graph_schema import CodeGraph
from .file_relations import FileRelationBuilder
from .ast_relations import ASTRelationBuilder
from .call_graph import CallGraphBuilder
from .inheritance import InheritanceBuilder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 构建配置（对应消融实验 4.3.3）
# ---------------------------------------------------------------------------

@dataclass
class BuildConfig:
    """
    控制各关系层的开关，用于消融实验或快速调试。

    Attributes
    ----------
    enable_file_relations : bool   — 是否构建文件导入关系（MODULE + IMPORTS）
    enable_ast_relations  : bool   — 是否构建 AST 父子/兄弟关系
    enable_call_graph     : bool   — 是否构建函数调用图
    enable_inheritance    : bool   — 是否构建类继承图
    exclude_dirs          : set    — 额外需要排除的目录名
    save_path             : str    — 构建完成后自动保存的路径（None 则不保存）
    save_format           : str    — 保存格式，'json' 或 'pickle'
    """
    enable_file_relations: bool = True
    enable_ast_relations:  bool = True
    enable_call_graph:     bool = True
    enable_inheritance:    bool = True
    exclude_dirs:          Set[str] = field(default_factory=set)
    save_path:             Optional[str] = None
    save_format:           str = "pickle"   # 'json' | 'pickle'


# ---------------------------------------------------------------------------
# 主入口类
# ---------------------------------------------------------------------------

class CodeGraphBuilder:
    """
    多层代码图统一构建器。

    快速上手
    --------
    >>> from code_graph_builder import CodeGraphBuilder
    >>> builder = CodeGraphBuilder("/path/to/repo")
    >>> graph = builder.build()
    >>> print(graph)
    CodeGraph(repo='/path/to/repo', nodes=512, edges=1024)

    保存 / 加载
    -----------
    >>> graph.save_pickle("code_graph.pkl")
    >>> from code_graph_builder import CodeGraph
    >>> graph = CodeGraph.load_pickle("code_graph.pkl")

    消融实验（仅 AST + 调用图，不含继承）
    ----------------------------------------
    >>> from code_graph_builder.builder import BuildConfig
    >>> cfg = BuildConfig(enable_inheritance=False)
    >>> graph = builder.build(config=cfg)
    """

    def __init__(self, repo_root: str):
        """
        Parameters
        ----------
        repo_root : 仓库根目录的绝对路径或相对路径。
        """
        import os
        self.repo_root = os.path.abspath(repo_root)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def build(self, config: Optional[BuildConfig] = None) -> CodeGraph:
        """
        完整构建多层代码图。

        Parameters
        ----------
        config : BuildConfig，可选。默认开启所有关系层。

        Returns
        -------
        CodeGraph : 包含所有节点与多类型边的完整代码图。
        """
        if config is None:
            config = BuildConfig()

        graph = CodeGraph(repo_root=self.repo_root)
        total_start = time.perf_counter()

        # Step 1：文件结构关系
        if config.enable_file_relations:
            self._run_step(
                "FileRelationBuilder",
                FileRelationBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )

        # Step 2：AST 关系
        if config.enable_ast_relations:
            self._run_step(
                "ASTRelationBuilder",
                ASTRelationBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )

        # Step 3：函数调用图
        if config.enable_call_graph:
            self._run_step(
                "CallGraphBuilder",
                CallGraphBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )

        # Step 4：类继承层次
        if config.enable_inheritance:
            self._run_step(
                "InheritanceBuilder",
                InheritanceBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )

        elapsed = time.perf_counter() - total_start
        stats = graph.stats()
        logger.info(
            "CodeGraph built in %.2fs | nodes=%d  edges=%d",
            elapsed, stats["total_nodes"], stats["total_edges"],
        )

        # 自动保存
        if config.save_path:
            if config.save_format == "json":
                graph.save_json(config.save_path)
            else:
                graph.save_pickle(config.save_path)
            logger.info("Graph saved to %s (%s)", config.save_path, config.save_format)

        return graph

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

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
