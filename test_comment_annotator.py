"""
test_comment_annotator.py — 注释生成模块测试

测试分层设计
------------
  build_graph_no_annotation()   → 纯结构图，comment 全空
  build_graph_with_annotation() → 带真实注释的图（DashScope 或 MockBackend）

  为什么分开：
    结构测试需要"控制变量"——先有空白状态，再施加操作，才能验证操作效果。
    就像做实验需要对照组。
"""
from __future__ import annotations
import os, sys, tempfile, textwrap
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from code_graph_builder import (
    CodeGraph, CodeGraphBuilder, NodeType,
    CommentAnnotator, AnnotatorConfig, MockBackend,
    DashScopeBackend, get_default_backend,
)
from code_graph_builder.builder import BuildConfig

MOCK_REPO = {
    "base.py": textwrap.dedent("""\
        class Animal:
            def speak(self):
                pass
            def breathe(self):
                pass
        def create_animal(name):
            return Animal()
    """),
    "dog.py": textwrap.dedent("""\
        from base import Animal
        class Dog(Animal):
            def speak(self):
                bark()
            def fetch(self, item):
                return item
        def bark():
            pass
    """),
}

# ===========================================================================
# 两种图工厂 —— 测试分层的核心
# ===========================================================================

def build_graph_no_annotation(tmp_dir: str) -> CodeGraph:
    """纯结构图：comment 全空，行为不依赖环境变量。用于 PART A。"""
    for rel, content in MOCK_REPO.items():
        Path(os.path.join(tmp_dir, rel)).write_text(content, encoding="utf-8")
    return CodeGraphBuilder(tmp_dir).build(config=BuildConfig(enable_annotation=False))


def build_graph_with_annotation(tmp_dir: str, backend=None) -> CodeGraph:
    """带注释的图：调用真实后端。backend=None 时自动选择。用于 PART B。"""
    for rel, content in MOCK_REPO.items():
        Path(os.path.join(tmp_dir, rel)).write_text(content, encoding="utf-8")
    return CodeGraphBuilder(tmp_dir).build(
        config=BuildConfig(enable_annotation=True, llm_backend=backend)
    )

# ===========================================================================
# 展示工具
# ===========================================================================

def print_node_detail(graph: CodeGraph, node_id: str, show_code=False) -> None:
    from code_graph_builder import EdgeType
    node = graph.get_node(node_id)
    if not node:
        return
    print(f"\n  ┌─ {node.id}  [{node.type.value}]")
    print(f"  │  {node.file}  L{node.start_line}~{node.end_line}")
    # 关系
    children  = [graph.get_node(c).name for c in graph.successors(node.id, EdgeType.PARENT_CHILD) if graph.get_node(c)]
    callees   = [graph.get_node(c).name for c in graph.successors(node.id, EdgeType.CALLS) if graph.get_node(c)]
    bases     = [graph.get_node(i).name for i in graph.successors(node.id, EdgeType.INHERITS) if graph.get_node(i)]
    if children:  print(f"  │  子节点 : {', '.join(children)}")
    if bases:     print(f"  │  继承   : {', '.join(bases)}")
    if callees:   print(f"  │  调用   : {', '.join(callees)}")
    # 源码
    if show_code and node.code_text:
        lines = node.code_text.rstrip().splitlines()
        print(f"  │  源码   :")
        for line in lines[:6]:
            print(f"  │    {line}")
        if len(lines) > 6:
            print(f"  │    ... ({len(lines)-6} 行省略)")
    # 注释
    if node.comment:
        print(f"  │  注释   :")
        for line in node.comment.splitlines():
            print(f"  │    {line}")
    else:
        print(f"  │  注释   : (空)")
    print(f"  └{'─'*52}")


def print_graph_summary(graph: CodeGraph, title="") -> None:
    stats = graph.stats()
    annotated = sum(1 for n in graph.iter_nodes() if n.comment)
    non_mod   = sum(1 for n in graph.iter_nodes() if n.type != NodeType.MODULE)
    print(f"\n  ── {title}")
    print(f"  节点: {stats['total_nodes']}  边: {stats['total_edges']}  "
          f"已标注: {annotated}/{non_mod} 个非MODULE节点")

# ===========================================================================
# PART A：结构验证（使用无注释图）
# ===========================================================================

def test_1_comment_initially_empty(graph):
    print("\n[TEST 1] comment 字段初始全部为空")
    bad = [n.id for n in graph.iter_nodes() if n.comment != ""]
    assert not bad, f"以下节点 comment 不为空（不应该）: {bad}"
    total = sum(1 for _ in graph.iter_nodes())
    print(f"  ✓ 全部 {total} 个节点 comment 为空")
    print("  说明：build_graph_no_annotation() 明确禁用了注释生成，")
    print("        所以即使环境中有 DASHSCOPE_API_KEY 也不会触发")

def test_2_mock_fills_comment(graph):
    print("\n[TEST 2] MockBackend 在干净图上填充 comment")
    result = CommentAnnotator(MockBackend()).annotate(graph)
    assert result.succeeded > 0 and result.failed == 0
    print(f"  ✓ 成功标注 {result.succeeded} 个节点")
    sample = next(n for n in graph.iter_nodes(NodeType.METHOD) if n.comment)
    print(f"  样本: {sample.qualified_name}")
    print(f"  注释: {sample.comment[:80]}")

def test_3_networkx_sync(graph):
    print("\n[TEST 3] comment 同步写入 NetworkX 内部")
    bad = [n.id for n in graph.iter_nodes() if n.comment and
           graph._g.nodes[n.id].get("comment") != n.comment]
    assert not bad, f"不一致节点: {bad}"
    print("  ✓ CodeNode 与 NetworkX 内部属性完全一致")

def test_4_skip_if_exists(graph):
    print("\n[TEST 4] skip_if_exists 断点续传")
    nid, sentinel = "dog.py::bark", "手动注释，不可被覆盖"
    graph.get_node(nid).comment = sentinel
    graph._g.nodes[nid]["comment"] = sentinel
    CommentAnnotator(MockBackend()).annotate(graph, config=AnnotatorConfig(skip_if_exists=True))
    assert graph.get_node(nid).comment == sentinel
    print(f"  ✓ '{nid}' 的手动注释未被覆盖")

def test_5_partial_annotate(graph):
    print("\n[TEST 5] annotate_nodes 增量标注指定节点")
    for n in graph.iter_nodes():
        n.comment = ""; graph._g.nodes[n.id]["comment"] = ""
    targets = ["base.py::Animal", "base.py::create_animal"]
    result  = CommentAnnotator(MockBackend()).annotate_nodes(graph, targets)
    assert result.succeeded == 2
    for nid in targets:
        assert graph.get_node(nid).comment
    extras = [n.id for n in graph.iter_nodes() if n.id not in targets and n.comment]
    assert not extras, f"意外标注: {extras}"
    print(f"  ✓ 只标注了 {len(targets)} 个指定节点，其余未被影响")

def test_6_type_filter(graph):
    print("\n[TEST 6] annotate_types 类型过滤")
    for n in graph.iter_nodes():
        n.comment = ""; graph._g.nodes[n.id]["comment"] = ""
    CommentAnnotator(MockBackend()).annotate(
        graph, config=AnnotatorConfig(annotate_types={NodeType.FUNCTION})
    )
    for node in graph.iter_nodes():
        if node.type == NodeType.FUNCTION and node.code_text.strip():
            assert node.comment, f"FUNCTION {node.id} 应有注释"
        elif node.type in (NodeType.MODULE, NodeType.CLASS, NodeType.METHOD):
            assert not node.comment, f"{node.type.value} {node.id} 不应有注释"
    print("  ✓ 仅 FUNCTION 被标注，MODULE/CLASS/METHOD 均跳过")

def test_7_serialization(graph, tmp_dir):
    print("\n[TEST 7] 序列化后 comment 不丢失")
    for n in graph.iter_nodes():
        n.comment = ""; graph._g.nodes[n.id]["comment"] = ""
    CommentAnnotator(MockBackend()).annotate(graph)
    pkl = os.path.join(tmp_dir, "g.pkl")
    jsn = os.path.join(tmp_dir, "g.json")
    graph.save_pickle(pkl); graph.save_json(jsn)
    g2, g3 = CodeGraph.load_pickle(pkl), CodeGraph.load_json(jsn)
    bad = [n.id for n in graph.iter_nodes() if n.comment and
           (g2.get_node(n.id).comment != n.comment or
            g3.get_node(n.id).comment != n.comment)]
    assert not bad, f"序列化后不一致: {bad}"
    print("  ✓ Pickle 和 JSON 序列化后 comment 完整保留")

# ===========================================================================
# PART B：内容展示（使用真实后端，输出节点详情）
# ===========================================================================

def test_8_show_real_annotation(tmp_dir):
    print("\n[TEST 8] 真实后端注释内容展示")
    backend = get_default_backend(verbose=False)
    bname   = type(backend).__name__
    print(f"  使用后端: {bname}", end="")
    if isinstance(backend, DashScopeBackend):
        print(f"  模型: {backend.model}  接入点: {backend.API_URL}")
    elif isinstance(backend, MockBackend):
        print("  （无 API Key，用 Mock 演示格式）")
    else:
        print()

    graph = build_graph_with_annotation(tmp_dir, backend=backend)
    print_graph_summary(graph, f"图统计（后端: {bname}）")

    print("\n  【各类型节点示例（含注释）】")
    shown = {NodeType.CLASS: False, NodeType.FUNCTION: False, NodeType.METHOD: False}
    for node in graph.iter_nodes():
        if node.type in shown and not shown[node.type] and node.comment:
            print_node_detail(graph, node.id, show_code=True)
            shown[node.type] = True
        if all(shown.values()):
            break

    assert sum(1 for n in graph.iter_nodes() if n.comment) > 0
    print(f"  ✓ 注释生成成功")

def test_9_full_node_output(tmp_dir):
    print("\n[TEST 9] build() 一键集成 —— 全部节点详情输出")
    backend = get_default_backend(verbose=False)
    bname   = type(backend).__name__

    for rel, content in MOCK_REPO.items():
        Path(os.path.join(tmp_dir, rel)).write_text(content, encoding="utf-8")
    graph = CodeGraphBuilder(tmp_dir).build(
        config=BuildConfig(enable_annotation=True, llm_backend=backend)
    )
    print_graph_summary(graph, f"build() 完成（后端: {bname}）")

    for node_type in (NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD):
        nodes = list(graph.iter_nodes(node_type))
        if nodes:
            print(f"\n  ── {node_type.value}（{len(nodes)} 个）──")
            for node in nodes:
                print_node_detail(graph, node.id, show_code=True)

    assert sum(1 for n in graph.iter_nodes() if n.comment) > 0
    print(f"  ✓ 完整节点输出展示完成")

# ===========================================================================
# PART C：优先级与集成
# ===========================================================================

def test_10_backend_priority():
    print("\n[TEST 10] get_default_backend 优先级")
    ds_key = os.environ.get("DASHSCOPE_API_KEY", "")
    an_key = os.environ.get("ANTHROPIC_API_KEY", "")
    backend = get_default_backend(verbose=False)
    bname   = type(backend).__name__
    if ds_key:
        assert isinstance(backend, DashScopeBackend), f"应选 DashScopeBackend，实际: {bname}"
        print(f"  ✓ DashScopeBackend（DASHSCOPE_API_KEY 已设置）  模型: {backend.model}")
    elif an_key:
        from code_graph_builder import AnthropicBackend
        assert isinstance(backend, AnthropicBackend), f"应选 AnthropicBackend，实际: {bname}"
        print(f"  ✓ 降级到 AnthropicBackend")
    else:
        assert isinstance(backend, MockBackend), f"应选 MockBackend，实际: {bname}"
        print(f"  ✓ 降级到 MockBackend（无任何 API Key）")

def test_11_auto_backend(tmp_dir):
    print("\n[TEST 11] llm_backend=None 时 build() 自动选后端")
    for rel, content in MOCK_REPO.items():
        Path(os.path.join(tmp_dir, rel)).write_text(content, encoding="utf-8")
    graph = CodeGraphBuilder(tmp_dir).build(
        config=BuildConfig(enable_annotation=True, llm_backend=None)
    )
    annotated = sum(1 for n in graph.iter_nodes() if n.comment)
    bname = type(get_default_backend(verbose=False)).__name__
    assert annotated > 0
    print(f"  ✓ 后端: {bname}，标注 {annotated} 个节点")

# ===========================================================================
# 主程序
# ===========================================================================

def main():
    print("=" * 60)
    print("注释生成模块测试")
    print("=" * 60)

    print("\n━━━ PART A：结构验证（enable_annotation=False 的干净图）━━━")
    with tempfile.TemporaryDirectory() as tmp:
        g = build_graph_no_annotation(tmp)
        test_1_comment_initially_empty(g)
        test_2_mock_fills_comment(g)
        test_3_networkx_sync(g)
        test_4_skip_if_exists(g)
        test_5_partial_annotate(g)
        test_6_type_filter(g)
        test_7_serialization(g, tmp)

    print("\n━━━ PART B：内容展示（真实后端）━━━")
    with tempfile.TemporaryDirectory() as tmp:
        test_8_show_real_annotation(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_9_full_node_output(tmp)

    print("\n━━━ PART C：优先级与集成 ━━━")
    test_10_backend_priority()
    with tempfile.TemporaryDirectory() as tmp:
        test_11_auto_backend(tmp)

    print("\n" + "=" * 60)
    print("全部测试通过 ✓")
    print("=" * 60)

if __name__ == "__main__":
    main()