"""
retrieval_result.py — 检索结果统一数据结构
============================================
对应论文 3.4 节：检索结果原因分析模块

每个检索结果包含四个维度的信息：
  1. 节点本体       : 节点 id、源码、所在文件等
  2. 分数           : 结构分、语义分、融合分
  3. 结构原因       : 在哪些拓扑特征上与 query 相似
  4. 结构位置摘要   : 该节点在仓库中的角色（被谁调用、依赖谁、继承关系）

这些信息直接拼入 SWE-Agent 的 search_similar_code 工具输出，
帮助 LLM 理解"为什么检索到这个结果"，而不只是看到代码本身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class StructuralPosition:
    """
    节点在代码图中的结构位置摘要。
    对应论文 3.4.2 的"结构位置维度"。
    """
    call_in_degree:   int = 0     # 被多少个函数/方法调用（入度）
    call_out_degree:  int = 0     # 调用了多少个函数/方法（出度）
    inherit_depth:    int = 0     # 继承层级深度（0=顶级基类，越大越靠叶子）
    n_subclasses:     int = 0     # 子类数量（仅 CLASS 节点有意义）
    n_methods:        int = 0     # 直接子方法数量（仅 CLASS 节点有意义）
    is_overriding:    bool = False # 是否重写了父类方法
    callers:          List[str] = field(default_factory=list)  # 调用者名称列表（前5个）
    callees:          List[str] = field(default_factory=list)  # 被调用者名称列表（前5个）

    def to_text(self) -> str:
        """生成自然语言摘要，直接放入 LLM prompt。"""
        parts = []
        if self.call_in_degree > 0:
            callers_str = ", ".join(self.callers[:3])
            parts.append(f"被 {self.call_in_degree} 个函数调用（含 {callers_str}）")
        else:
            parts.append("无调用者（叶子节点或入口函数）")

        if self.call_out_degree > 0:
            callees_str = ", ".join(self.callees[:3])
            parts.append(f"调用了 {self.call_out_degree} 个函数（含 {callees_str}）")

        if self.inherit_depth > 0:
            parts.append(f"位于继承链第 {self.inherit_depth} 层")
        if self.n_subclasses > 0:
            parts.append(f"有 {self.n_subclasses} 个子类")
        if self.is_overriding:
            parts.append("重写了父类方法")
        if self.n_methods > 0:
            parts.append(f"包含 {self.n_methods} 个方法")

        return "；".join(parts) if parts else "无特殊结构位置信息"


@dataclass
class RetrievalResult:
    """
    单个检索结果，包含节点信息、分数和原因分析。
    对应论文 3.4.3 的输出格式设计。
    """
    # ── 节点基本信息 ──────────────────────────────────────
    node_id:        str   = ""
    node_name:      str   = ""
    qualified_name: str   = ""
    node_type:      str   = ""   # CLASS / FUNCTION / METHOD
    file:           str   = ""
    start_line:     int   = 0
    end_line:       int   = 0
    code_text:      str   = ""
    comment:        str   = ""   # LLM 生成的注释

    # ── 检索分数 ──────────────────────────────────────────
    structural_score: float = 0.0   # 结构相似度 [0, 1]
    semantic_score:   float = 0.0   # 语义相似度 [0, 1]
    final_score:      float = 0.0   # 加权融合分 [0, 1]

    # ── 原因分析（对应论文 3.4.2）────────────────────────
    structural_reason:  str = ""   # 结构匹配依据
    semantic_reason:    str = ""   # 语义关联说明
    position_summary:   str = ""   # 结构位置摘要（自然语言）

    # ── 结构位置详情 ──────────────────────────────────────
    position: Optional[StructuralPosition] = None

    def to_agent_text(self, show_code: bool = True) -> str:
        """
        生成面向 SWE-Agent LLM 的结构化文本。
        这是 search_similar_code 工具的最终输出格式。
        """
        lines = [
            f"## {self.qualified_name}  [{self.node_type}]",
            f"文件: {self.file}  行: {self.start_line}~{self.end_line}",
            f"综合评分: {self.final_score:.3f}  "
            f"（结构: {self.structural_score:.3f} | 语义: {self.semantic_score:.3f}）",
        ]
        if self.structural_reason:
            lines.append(f"结构匹配依据: {self.structural_reason}")
        if self.semantic_reason:
            lines.append(f"语义关联说明: {self.semantic_reason}")
        if self.position_summary:
            lines.append(f"结构位置: {self.position_summary}")
        if self.comment:
            lines.append(f"功能注释: {self.comment}")
        if show_code and self.code_text:
            code_preview = self.code_text[:400] + ("..." if len(self.code_text) > 400 else "")
            lines.append(f"```python\n{code_preview}\n```")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (f"RetrievalResult({self.qualified_name!r}, "
                f"final={self.final_score:.3f}, "
                f"struct={self.structural_score:.3f}, "
                f"sem={self.semantic_score:.3f})")


@dataclass
class RetrievalResponse:
    """
    完整的检索响应，包含 Top-K 结果列表和检索元信息。
    """
    query:          str                  = ""
    results:        List[RetrievalResult] = field(default_factory=list)
    total_nodes:    int                  = 0   # 检索范围内的节点总数
    elapsed_ms:     float                = 0.0

    def to_agent_text(self, show_code: bool = True) -> str:
        """生成面向 Agent 的完整检索报告。"""
        header = (
            f"# 检索结果  query='{self.query}'\n"
            f"共找到 {len(self.results)} 个相关节点（从 {self.total_nodes} 个候选中检索）"
            f"  耗时 {self.elapsed_ms:.1f}ms\n"
            f"{'═' * 60}"
        )
        sections = [header]
        for i, r in enumerate(self.results, 1):
            sections.append(f"\n### [{i}] {r.to_agent_text(show_code=show_code)}")
        return "\n".join(sections)

    def __repr__(self) -> str:
        return (f"RetrievalResponse(query={self.query!r}, "
                f"results={len(self.results)}, elapsed={self.elapsed_ms:.1f}ms)")
    