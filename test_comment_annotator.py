"""
test_comment_annotator.py — 注释生成模块测试
=============================================
使用 MockBackend 验证完整流程，不消耗真实 API。

测试覆盖：
  1. comment 字段初始为空
  2. MockBackend 正确触发并填充 comment
  3. 注释内容写回节点且持久化到图结构
  4. skip_if_exists 断点续传逻辑
  5. annotate_nodes 增量标注接口
  6. enable_annotation=True 时 build() 集成路径
  7. enable_annotation=True 但 llm_backend=None 时的警告路径
  8. 序列化后 comment 字段不丢失
  9. 只标注指定类型（跳过 MODULE）
  10. 真实 API 调用示例（需要 ANTHROPIC_API_KEY，可选跑）
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from code_graph_builder import (
    CodeGraph, CodeGraphBuilder, NodeType,
    CommentAnnotator, AnnotatorConfig, MockBackend,
    DashScopeBackend, get_default_backend,
)
from code_graph_builder.builder import BuildConfig

# ---------------------------------------------------------------------------
# 测试仓库（复用之前的黄金标准仓库）
# ---------------------------------------------------------------------------

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


def build_test_graph(tmp_dir: str) -> CodeGraph:
    for rel, content in MOCK_REPO.items():
        Path(os.path.join(tmp_dir, rel)).write_text(content, encoding="utf-8")
    return CodeGraphBuilder(tmp_dir).build()


# ---------------------------------------------------------------------------
# 测试函数
# ---------------------------------------------------------------------------

def test_comment_field_initially_empty(graph: CodeGraph) -> None:
    print("\n[TEST 1] comment 字段初始为空")
    for node in graph.iter_nodes():
        assert node.comment == "", \
            f"节点 {node.id} 的 comment 初始不为空: '{node.comment}'"
    print("  ✓ 所有节点 comment 字段初始为空")


def test_mock_backend_fills_comment(graph: CodeGraph) -> None:
    print("\n[TEST 2] MockBackend 填充 comment 字段")
    annotator = CommentAnnotator(MockBackend())
    result = annotator.annotate(graph)

    assert result.succeeded > 0, "应有成功标注的节点"
    assert result.failed == 0,   f"不应有失败节点，实际 {result.failed} 个"

    # 检查每个非 MODULE 的有 code_text 的节点都有了注释
    for node in graph.iter_nodes():
        if node.type != NodeType.MODULE and node.code_text.strip():
            assert node.comment, f"节点 {node.id} 的 comment 仍为空"
            assert "[功能]" in node.comment or "Mock" in node.comment, \
                f"节点 {node.id} 的注释格式异常: {node.comment[:60]}"

    print(f"  ✓ 成功标注 {result.succeeded} 个节点")
    print(f"  样本注释（Dog.speak）: {graph.get_node('dog.py::Dog.speak').comment[:80]}")


def test_comment_written_to_graph_internal(graph: CodeGraph) -> None:
    print("\n[TEST 3] comment 写入 NetworkX 内部图结构")
    for node in graph.iter_nodes():
        if node.comment:
            # 验证 _g 内部属性与 CodeNode 对象一致
            internal = graph._g.nodes[node.id].get("comment", "")
            assert internal == node.comment, \
                f"{node.id}: CodeNode.comment 与内部图属性不一致"
    print("  ✓ NetworkX 内部节点属性与 CodeNode 对象同步")


def test_skip_if_exists(graph: CodeGraph) -> None:
    print("\n[TEST 4] skip_if_exists 断点续传")
    # 先手动给一个节点设置注释
    target_id = "dog.py::bark"
    target = graph.get_node(target_id)
    target.comment = "手动设置的注释，不应被覆盖"
    graph._g.nodes[target_id]["comment"] = target.comment

    cfg = AnnotatorConfig(skip_if_exists=True)
    annotator = CommentAnnotator(MockBackend())
    annotator.annotate(graph, config=cfg)

    # 验证手动注释没有被覆盖
    assert graph.get_node(target_id).comment == "手动设置的注释，不应被覆盖", \
        "skip_if_exists=True 时不应覆盖已有注释"
    print(f"  ✓ skip_if_exists 有效，已有注释的节点被跳过")


def test_annotate_nodes_partial(graph: CodeGraph) -> None:
    print("\n[TEST 5] annotate_nodes 增量标注指定节点")
    # 先清空所有注释
    for node in graph.iter_nodes():
        node.comment = ""
        graph._g.nodes[node.id]["comment"] = ""

    # 只标注两个节点
    target_ids = ["base.py::Animal", "base.py::create_animal"]
    annotator  = CommentAnnotator(MockBackend())
    result     = annotator.annotate_nodes(graph, target_ids)

    assert result.succeeded == 2, f"应成功标注 2 个节点，实际 {result.succeeded}"

    for nid in target_ids:
        assert graph.get_node(nid).comment, f"{nid} 注释仍为空"
    # 其他节点应该仍然为空
    for node in graph.iter_nodes():
        if node.id not in target_ids and node.type != NodeType.MODULE:
            assert node.comment == "", \
                f"未指定节点 {node.id} 不应被标注，但 comment='{node.comment[:30]}'"

    print(f"  ✓ 只标注了指定的 {len(target_ids)} 个节点")


def test_build_with_annotation_integration(tmp_dir: str) -> None:
    print("\n[TEST 6] build() 集成路径：enable_annotation=True")
    for rel, content in MOCK_REPO.items():
        Path(os.path.join(tmp_dir, rel)).write_text(content, encoding="utf-8")

    cfg = BuildConfig(
        enable_annotation=True,
        llm_backend=MockBackend(),
    )
    graph = CodeGraphBuilder(tmp_dir).build(config=cfg)

    annotated = sum(
        1 for n in graph.iter_nodes()
        if n.comment and n.type != NodeType.MODULE
    )
    assert annotated > 0, "build() 集成路径应产生注释"
    print(f"  ✓ build() 集成路径正常，共标注 {annotated} 个节点")

    # 展示几个样本
    for node in list(graph.iter_nodes(NodeType.METHOD))[:2]:
        print(f"  节点: {node.qualified_name}")
        print(f"  注释: {node.comment[:100]}")


def test_build_annotation_without_backend(tmp_dir: str) -> None:
    print("\n[TEST 7] enable_annotation=True 且 llm_backend=None 时自动兜底 MockBackend")
    for rel, content_ in MOCK_REPO.items():
        Path(os.path.join(tmp_dir, rel)).write_text(content_, encoding="utf-8")

    # llm_backend=None → get_default_backend() 自动选择
    # 无任何 API Key 时降级到 MockBackend，仍然会产生注释
    cfg = BuildConfig(enable_annotation=True, llm_backend=None)
    graph = CodeGraphBuilder(tmp_dir).build(config=cfg)

    annotated = sum(1 for n in graph.iter_nodes() if n.comment)
    # MockBackend 兜底时应仍然产生注释（Mock 注释）
    assert annotated > 0, f"兜底 MockBackend 也应产生注释，实际 {annotated} 个"
    print(f"  ✓ 自动兜底 MockBackend 正常运行，产生 {annotated} 个注释")
    print("  （有真实 API Key 时会自动使用对应后端）")


def test_comment_survives_serialization(graph: CodeGraph, tmp_dir: str) -> None:
    print("\n[TEST 8] 序列化/反序列化后 comment 不丢失")
    # 先确保有注释
    annotator = CommentAnnotator(MockBackend())
    annotator.annotate(graph)

    pkl_path  = os.path.join(tmp_dir, "graph_with_comments.pkl")
    json_path = os.path.join(tmp_dir, "graph_with_comments.json")

    graph.save_pickle(pkl_path)
    graph.save_json(json_path)

    g_pkl  = CodeGraph.load_pickle(pkl_path)
    g_json = CodeGraph.load_json(json_path)

    for node in graph.iter_nodes():
        if not node.comment:
            continue
        pkl_node  = g_pkl.get_node(node.id)
        json_node = g_json.get_node(node.id)
        assert pkl_node  and pkl_node.comment  == node.comment, \
            f"Pickle 恢复后 {node.id}.comment 不一致"
        assert json_node and json_node.comment == node.comment, \
            f"JSON 恢复后 {node.id}.comment 不一致"

    print("  ✓ Pickle 和 JSON 序列化后 comment 字段完整保留")


def test_only_annotates_target_types(graph: CodeGraph) -> None:
    print("\n[TEST 9] 只标注指定类型，MODULE 节点不被标注")
    # 清空注释
    for node in graph.iter_nodes():
        node.comment = ""
        graph._g.nodes[node.id]["comment"] = ""

    cfg = AnnotatorConfig(
        annotate_types={NodeType.FUNCTION}  # 只标注顶层函数
    )
    annotator = CommentAnnotator(MockBackend())
    annotator.annotate(graph, config=cfg)

    for node in graph.iter_nodes():
        if node.type == NodeType.FUNCTION and node.code_text.strip():
            assert node.comment, f"FUNCTION 节点 {node.id} 应有注释"
        elif node.type in (NodeType.MODULE, NodeType.CLASS, NodeType.METHOD):
            assert node.comment == "", \
                f"{node.type.value} 节点 {node.id} 不应有注释"

    print("  ✓ 只有 FUNCTION 节点被标注，MODULE/CLASS/METHOD 均跳过")


def test_dashscope_backend_real() -> None:
    """
    可选测试：真实阿里云 DashScope API 调用。
    需要设置环境变量 DASHSCOPE_API_KEY。
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("\n[TEST 10-DS] DashScope 真实 API 测试（跳过：未设置 DASHSCOPE_API_KEY）")
        return

    print("\n[TEST 10-DS] 真实 DashScope API 调用（阿里云）")
    with tempfile.TemporaryDirectory() as tmp:
        for rel, content_ in MOCK_REPO.items():
            Path(os.path.join(tmp, rel)).write_text(content_, encoding="utf-8")
        graph = CodeGraphBuilder(tmp).build()

    target_id = "dog.py::bark"
    backend   = DashScopeBackend(api_key=api_key)
    annotator = CommentAnnotator(backend)
    result    = annotator.annotate_nodes(graph, [target_id])

    assert result.succeeded == 1, f"DashScope API 调用失败: {result}"
    node = graph.get_node(target_id)
    assert node.comment and "[注释生成失败]" not in node.comment
    print(f"  ✓ DashScope API 调用成功，模型: {backend.model}")
    print(f"  节点: {target_id}")
    print(f"  注释:\n{node.comment}")


def test_get_default_backend_priority() -> None:
    """
    验证 get_default_backend() 按优先级选择后端：
    有 DASHSCOPE_API_KEY 时返回 DashScopeBackend，否则降级。
    """
    print("\n[TEST 11] get_default_backend 优先级验证")
    import os

    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    backend = get_default_backend(verbose=False)

    if dashscope_key:
        assert isinstance(backend, DashScopeBackend), \
            f"有 DASHSCOPE_API_KEY 时应返回 DashScopeBackend，实际: {type(backend).__name__}"
        print(f"  ✓ 正确选择 DashScopeBackend（DASHSCOPE_API_KEY 已设置）")
    elif anthropic_key:
        from code_graph_builder import AnthropicBackend
        assert isinstance(backend, AnthropicBackend), \
            f"无 DashScope Key 时应返回 AnthropicBackend，实际: {type(backend).__name__}"
        print(f"  ✓ 正确降级到 AnthropicBackend")
    else:
        assert isinstance(backend, MockBackend), \
            f"无任何 Key 时应返回 MockBackend，实际: {type(backend).__name__}"
        print(f"  ✓ 正确降级到 MockBackend（无任何 API Key）")


def test_dashscope_build_integration() -> None:
    """
    验证 BuildConfig 中 enable_annotation=True 且有 DASHSCOPE_API_KEY 时
    build() 能自动选择 DashScopeBackend。
    （无 Key 时用 MockBackend 兜底，也能跑通）
    """
    print("\n[TEST 12] build() 自动选择 DashScope 后端集成")
    with tempfile.TemporaryDirectory() as tmp:
        for rel, c in MOCK_REPO.items():
            Path(os.path.join(tmp, rel)).write_text(c, encoding="utf-8")

        # llm_backend=None → 自动调用 get_default_backend()
        cfg   = BuildConfig(enable_annotation=True, llm_backend=None)
        graph = CodeGraphBuilder(tmp).build(config=cfg)

    annotated = sum(1 for n in graph.iter_nodes() if n.comment)
    # 无论哪个后端，只要 enable_annotation=True 就应产生注释
    assert annotated > 0, "build() 自动后端选择应产生注释"
    backend_type = type(get_default_backend(verbose=False)).__name__
    print(f"  ✓ 自动选择后端: {backend_type}，共标注 {annotated} 个节点")


def test_real_api_optional() -> None:
    """
    可选测试：真实 Anthropic API 调用。
    需要设置环境变量 ANTHROPIC_API_KEY=sk-ant-xxx
    跳过条件：未设置 API Key。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("\n[TEST 10] 真实 API 测试（跳过：未设置 ANTHROPIC_API_KEY）")
        return

    print("\n[TEST 10] 真实 Anthropic API 调用")
    from code_graph_builder import AnthropicBackend

    with tempfile.TemporaryDirectory() as tmp:
        for rel, content in MOCK_REPO.items():
            Path(os.path.join(tmp, rel)).write_text(content, encoding="utf-8")
        graph = CodeGraphBuilder(tmp).build()

    # 只标注 1 个节点，节省费用
    target_id = "dog.py::bark"
    backend   = AnthropicBackend(api_key=api_key)
    annotator = CommentAnnotator(backend)
    result    = annotator.annotate_nodes(graph, [target_id])

    assert result.succeeded == 1, f"真实 API 调用失败: {result}"
    node = graph.get_node(target_id)
    assert node.comment and "[注释生成失败]" not in node.comment, \
        f"注释内容异常: {node.comment}"

    print(f"  ✓ 真实 API 调用成功")
    print(f"  节点: {target_id}")
    print(f"  注释:\n{node.comment}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("注释生成模块逻辑测试")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        # 建仓库、构建图（无注释）
        graph = build_test_graph(tmp)

        test_comment_field_initially_empty(graph)
        test_mock_backend_fills_comment(graph)
        test_comment_written_to_graph_internal(graph)
        test_skip_if_exists(graph)
        test_annotate_nodes_partial(graph)

    # 重建图（上面测试改动了 comment，需要干净的图）
    with tempfile.TemporaryDirectory() as tmp2:
        graph2 = build_test_graph(tmp2)
        test_build_with_annotation_integration(tmp2)

    with tempfile.TemporaryDirectory() as tmp3:
        test_build_annotation_without_backend(tmp3)

    with tempfile.TemporaryDirectory() as tmp4:
        graph4 = build_test_graph(tmp4)
        test_comment_survives_serialization(graph4, tmp4)

    with tempfile.TemporaryDirectory() as tmp5:
        graph5 = build_test_graph(tmp5)
        test_only_annotates_target_types(graph5)

    test_dashscope_backend_real()
    test_get_default_backend_priority()
    test_dashscope_build_integration()
    test_real_api_optional()

    print("\n" + "=" * 60)
    print("全部测试通过 ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()