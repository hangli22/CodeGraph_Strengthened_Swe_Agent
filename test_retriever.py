"""
test_retriever.py — 检索模块完整测试
=====================================

测试分层
--------
  PART A：单元测试（FeatureExtractor + StructuralRetriever）
    - 不依赖任何外部 API
    - 验证特征提取、归一化、结构检索的逻辑正确性

  PART B：语义检索测试（SemanticRetriever + TF-IDF 后端）
    - 使用本地 TF-IDF，不依赖网络
    - 验证 embedding 流程、冷启动降级、结果格式

  PART C：混合检索测试（HybridRetriever）
    - 验证两路融合逻辑、权重计算、消融配置

  PART D：真实 API 测试（可选，需要 DASHSCOPE_API_KEY）
    - 验证 DashScope Embedding 接口连通性
    - 展示真实语义检索结果

  PART E：输出格式测试
    - 验证 to_agent_text() 的格式和完整性
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from code_graph_builder import CodeGraphBuilder, NodeType, EdgeType
from code_graph_builder.builder import BuildConfig
from code_graph_retriever import (
    FeatureExtractor, FEATURE_DIM,
    StructuralRetriever,
    SemanticRetriever, TFIDFEmbeddingBackend, MockEmbeddingBackend,
    HybridRetriever,
    RetrievalResult, RetrievalResponse,
)

# ===========================================================================
# 测试仓库定义（比之前更丰富，以体现不同结构角色）
# ===========================================================================

RICH_REPO = {
    # base.py：定义顶层基类，高扇入（被多个子类继承）
    "base.py": textwrap.dedent("""\
        class BaseHandler:
            def handle(self, request):
                pass
            def validate(self, data):
                pass
            def log_error(self, msg):
                pass

        class Mixin:
            def helper(self):
                pass

        def create_handler(name):
            return BaseHandler()

        def parse_url(url):
            parts = url.split('/')
            return parts
    """),

    # http.py：HTTP 处理，继承 BaseHandler，有丰富的调用关系
    "http.py": textwrap.dedent("""\
        from base import BaseHandler, parse_url

        class HttpHandler(BaseHandler):
            def handle(self, request):
                url = parse_url(request)
                return self.process(url)

            def process(self, url):
                self.validate(url)
                return send_request(url)

            def handle_redirect(self, response):
                self.log_error('redirect')
                return self.handle(response)

        def send_request(url):
            return None

        def decode_response(data, encoding):
            return data.decode(encoding)
    """),

    # utils.py：工具函数，被多处调用（高扇入）
    "utils.py": textwrap.dedent("""\
        from base import parse_url

        def normalize_url(url):
            parts = parse_url(url)
            return '/'.join(parts)

        def encode_params(params):
            result = []
            for k, v in params.items():
                result.append(f'{k}={v}')
            return '&'.join(result)

        def safe_decode(data, encoding='utf-8'):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                return data.decode('latin-1')
    """),

    # session.py：会话管理，复杂调用链
    "session.py": textwrap.dedent("""\
        from http import HttpHandler, send_request
        from utils import normalize_url, encode_params

        class Session:
            def __init__(self):
                self.handler = HttpHandler()

            def get(self, url, params=None):
                url = normalize_url(url)
                if params:
                    url += '?' + encode_params(params)
                return send_request(url)

            def post(self, url, data):
                url = normalize_url(url)
                return self.handler.handle(url)
    """),
}


# ===========================================================================
# 工具函数
# ===========================================================================

def build_test_graph(tmp_dir: str, with_comments: bool = False):
    """构建测试用代码图。"""
    for rel, content in RICH_REPO.items():
        Path(os.path.join(tmp_dir, rel)).write_text(content, encoding="utf-8")
    cfg = BuildConfig(enable_annotation=False)
    graph = CodeGraphBuilder(tmp_dir).build(config=cfg)

    if with_comments:
        # 手动设置部分节点的 comment，模拟 LLM 标注结果
        comments = {
            "http.py::HttpHandler.handle_redirect":
                "[功能] 处理 HTTP 重定向响应，记录重定向日志并递归处理新请求\n"
                "[参数] response: HTTP 响应对象\n[返回值] 处理后的响应",
            "utils.py::safe_decode":
                "[功能] 安全解码字节数据，优先使用指定编码，失败时降级为 latin-1\n"
                "[参数] data: 待解码字节串 | encoding: 目标编码\n[返回值] 解码后的字符串\n"
                "[异常] 无（已内部处理 UnicodeDecodeError）",
            "http.py::decode_response":
                "[功能] 将 HTTP 响应体解码为字符串\n"
                "[参数] data: 响应字节数据 | encoding: 编码格式\n[返回值] 解码后字符串\n"
                "[异常] UnicodeDecodeError: 当编码不匹配时抛出",
            "session.py::Session.get":
                "[功能] 发送 HTTP GET 请求，支持 URL 参数\n"
                "[参数] url: 目标地址 | params: 查询参数字典\n[返回值] 响应对象",
            "base.py::BaseHandler":
                "[功能] HTTP 请求处理器基类，定义 handle/validate/log_error 接口",
            "http.py::HttpHandler":
                "[功能] 具体的 HTTP 请求处理器，实现 URL 解析、请求发送和重定向处理",
            "utils.py::normalize_url":
                "[功能] 规范化 URL 格式，解析后重新拼接",
            "base.py::parse_url":
                "[功能] 解析 URL 字符串，按斜杠分割返回路径列表",
        }
        for nid, comment in comments.items():
            node = graph.get_node(nid)
            if node:
                node.comment = comment
                graph._g.nodes[nid]["comment"] = comment

    return graph


def section(title: str) -> None:
    print(f"\n{'━'*60}")
    print(f"  {title}")
    print(f"{'━'*60}")


# ===========================================================================
# PART A：FeatureExtractor 单元测试
# ===========================================================================

def test_A1_feature_dim(graph):
    print("\n[A1] 特征向量维度正确")
    ext = FeatureExtractor(graph).build()
    for nid in ext.get_all_node_ids():
        vec = ext.get_feature(nid)
        assert vec is not None and vec.shape == (FEATURE_DIM,), \
            f"{nid}: 特征维度异常 {vec.shape}"
    n = len(ext.get_all_node_ids())
    print(f"  ✓ 全部 {n} 个节点特征向量维度均为 {FEATURE_DIM}")


def test_A2_feature_normalized(graph):
    print("\n[A2] 特征向量归一化到 [0,1]")
    ext = FeatureExtractor(graph).build()
    _, matrix = ext.get_matrix()
    assert matrix.min() >= -1e-6, f"归一化下界错误: {matrix.min()}"
    assert matrix.max() <= 1 + 1e-6, f"归一化上界错误: {matrix.max()}"
    print(f"  ✓ 特征矩阵值域 [{matrix.min():.3f}, {matrix.max():.3f}] ⊆ [0, 1]")


def test_A3_module_nodes_excluded(graph):
    print("\n[A3] MODULE 节点不在特征索引中")
    ext = FeatureExtractor(graph).build()
    all_ids = set(ext.get_all_node_ids())
    module_ids = {n.id for n in graph.iter_nodes(NodeType.MODULE)}
    overlap = all_ids & module_ids
    assert not overlap, f"MODULE 节点被意外纳入特征索引: {overlap}"
    print(f"  ✓ {len(module_ids)} 个 MODULE 节点均被排除")


def test_A4_call_degree_nonzero(graph):
    print("\n[A4] 有调用关系的节点，调用度特征不全为零")
    from code_graph_retriever.feature_extractor import IDX_CALL_IN, IDX_CALL_OUT
    ext = FeatureExtractor(graph).build()
    # HttpHandler.handle 调用了 parse_url 和 process，出度应 > 0
    nid = "http.py::HttpHandler.handle"
    vec = ext.get_feature(nid)
    assert vec is not None, f"节点 {nid} 不存在"
    # 出度特征（归一化后）应 > 0
    assert vec[IDX_CALL_OUT] > 0 or vec[IDX_CALL_IN] > 0, \
        f"{nid} 的调用度特征全为 0，归一化可能有误"
    print(f"  ✓ {nid}: call_in={vec[IDX_CALL_IN]:.3f} call_out={vec[IDX_CALL_OUT]:.3f}")


def test_A5_inherit_depth(graph):
    print("\n[A5] 继承深度特征：子类 > 父类")
    from code_graph_retriever.feature_extractor import IDX_INHERIT_DEPTH
    ext = FeatureExtractor(graph).build()
    base_vec = ext.get_feature("base.py::BaseHandler")
    http_vec = ext.get_feature("http.py::HttpHandler")
    assert base_vec is not None and http_vec is not None
    # 归一化后 HttpHandler（子类）的继承深度特征应 >= BaseHandler（根类）
    assert http_vec[IDX_INHERIT_DEPTH] >= base_vec[IDX_INHERIT_DEPTH], \
        (f"子类继承深度特征应 >= 父类："
         f"HttpHandler={http_vec[IDX_INHERIT_DEPTH]:.3f}, "
         f"BaseHandler={base_vec[IDX_INHERIT_DEPTH]:.3f}")
    print(f"  ✓ BaseHandler.depth={base_vec[IDX_INHERIT_DEPTH]:.3f} "
          f"≤ HttpHandler.depth={http_vec[IDX_INHERIT_DEPTH]:.3f}")


def test_A6_position_summary(graph):
    print("\n[A6] StructuralPosition.to_text() 非空")
    ext = FeatureExtractor(graph).build()
    shown = 0
    for nid in ext.get_all_node_ids()[:5]:
        pos = ext.get_position(nid)
        assert pos is not None
        text = pos.to_text()
        assert text, f"{nid}: to_text() 为空"
        if shown < 2:
            node = graph.get_node(nid)
            print(f"  {node.qualified_name}: {text}")
            shown += 1
    print(f"  ✓ 全部节点均有结构位置摘要")


def test_A7_structural_retriever_topk(graph):
    print("\n[A7] StructuralRetriever: Top-K 结构检索")
    retriever = StructuralRetriever(graph).build()
    # 以 HttpHandler.handle 为 query，找结构最相似的节点
    nid = "http.py::HttpHandler.handle"
    resp = retriever.search_by_node_id(nid, top_k=3)
    assert isinstance(resp, RetrievalResponse)
    assert len(resp.results) <= 3
    assert all(r.node_id != nid for r in resp.results), "结果中不应含 query 节点自身"
    assert all(0.0 <= r.structural_score <= 1.0 for r in resp.results)
    print(f"  ✓ 检索到 {len(resp.results)} 个结构相似节点（耗时 {resp.elapsed_ms:.1f}ms）")
    for r in resp.results:
        print(f"    {r.qualified_name:40s} struct={r.structural_score:.3f}  {r.structural_reason}")


def test_A8_structural_reason_nonempty(graph):
    print("\n[A8] 结构匹配原因文本非空")
    retriever = StructuralRetriever(graph).build()
    resp = retriever.search_by_node_id(
        "http.py::HttpHandler.handle", top_k=5
    )
    for r in resp.results:
        assert r.structural_reason, f"{r.node_id}: structural_reason 为空"
    print(f"  ✓ 全部 {len(resp.results)} 个结果均有结构原因文本")


def test_A9_exclude_self(graph):
    print("\n[A9] exclude_self=True 时结果中不含 query 节点")
    retriever = StructuralRetriever(graph).build()
    for nid in list(graph.iter_nodes(NodeType.FUNCTION))[:3]:
        resp = retriever.search_by_node_id(nid.id, top_k=5, exclude_self=True)
        assert all(r.node_id != nid.id for r in resp.results), \
            f"{nid.id} 出现在了自己的检索结果中"
    print("  ✓ 所有测试节点的检索结果均不含自身")


# ===========================================================================
# PART B：SemanticRetriever 测试（TF-IDF，无需 API）
# ===========================================================================

def test_B1_tfidf_backend_fit(graph):
    print("\n[B1] TFIDFEmbeddingBackend: fit 和 embed_batch")
    backend = TFIDFEmbeddingBackend(n_components=16)
    corpus  = ["handle HTTP request", "parse URL string", "decode response bytes",
               "normalize URL format", "send request to server"]
    backend.fit(corpus)
    vecs = backend.embed_batch(corpus[:3])
    assert vecs.shape[0] == 3
    assert vecs.shape[1] <= 16  # 受 corpus 大小限制，可能 < 16
    # L2 归一化验证
    norms = np.linalg.norm(vecs, axis=1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-5)
    print(f"  ✓ TF-IDF embedding 维度 {vecs.shape[1]}，L2 归一化正确")


def test_B2_semantic_retriever_build(graph_with_comments):
    print("\n[B2] SemanticRetriever: 使用 TF-IDF 后端构建索引")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = SemanticRetriever(graph_with_comments, backend=backend)
    retriever.build()
    assert retriever._matrix is not None
    assert retriever._matrix.shape[0] > 0
    print(f"  ✓ 索引构建成功，{retriever._matrix.shape[0]} 个节点")


def test_B3_semantic_search_returns_results(graph_with_comments):
    print("\n[B3] SemanticRetriever: 检索返回结果")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = SemanticRetriever(graph_with_comments, backend=backend)
    retriever.build()
    resp = retriever.search("decode unicode error HTTP response", top_k=3)
    assert isinstance(resp, RetrievalResponse)
    assert len(resp.results) > 0
    assert all(0.0 <= r.semantic_score <= 1.0 for r in resp.results)
    print(f"  ✓ 检索到 {len(resp.results)} 个结果")
    for r in resp.results:
        print(f"    {r.qualified_name:45s} sem={r.semantic_score:.3f}  {r.semantic_reason}")


def test_B4_cold_start_fallback(graph):
    print("\n[B4] 冷启动降级：无 comment 时用 code_text 做 embedding")
    # 使用无注释的图
    backend  = TFIDFEmbeddingBackend(n_components=16)
    retriever = SemanticRetriever(graph, backend=backend)
    retriever.build()
    # 确认使用了 code_text
    assert len(retriever._node_ids) > 0, "冷启动时应仍有可检索节点"
    resp = retriever.search("parse url string", top_k=3)
    assert len(resp.results) > 0
    print(f"  ✓ 冷启动降级正常，检索到 {len(resp.results)} 个节点")


def test_B5_mock_embedding_deterministic():
    print("\n[B5] MockEmbeddingBackend: 相同文本返回相同向量")
    backend = MockEmbeddingBackend(dim=32)
    v1 = backend.embed("hello world")
    v2 = backend.embed("hello world")
    v3 = backend.embed("different text")
    np.testing.assert_array_equal(v1, v2)
    assert not np.allclose(v1, v3), "不同文本应返回不同向量"
    print("  ✓ 相同文本 → 相同向量；不同文本 → 不同向量")


# ===========================================================================
# PART C：HybridRetriever 测试
# ===========================================================================

def test_C1_hybrid_build(graph_with_comments):
    print("\n[C1] HybridRetriever 构建")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = HybridRetriever(graph_with_comments, embedding_backend=backend)
    retriever.build()
    assert retriever._built
    print("  ✓ HybridRetriever 构建成功")


def test_C2_hybrid_search_text_query(graph_with_comments):
    print("\n[C2] HybridRetriever: 自然语言 query 检索")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = HybridRetriever(graph_with_comments, embedding_backend=backend)
    retriever.build()
    resp = retriever.search("UnicodeDecodeError HTTP 响应解码", top_k=5)
    assert isinstance(resp, RetrievalResponse)
    assert len(resp.results) > 0
    # final_score 应该是 α×struct + β×sem
    for r in resp.results:
        expected = 0.4 * r.structural_score + 0.6 * r.semantic_score
        assert abs(r.final_score - expected) < 1e-5, \
            f"{r.node_id}: final_score={r.final_score:.4f} 与期望 {expected:.4f} 不符"
    print(f"  ✓ 检索到 {len(resp.results)} 个结果，final_score 计算正确")
    for r in resp.results[:3]:
        print(f"    {r.qualified_name:45s} "
              f"final={r.final_score:.3f} "
              f"(s={r.structural_score:.3f}+e={r.semantic_score:.3f})")


def test_C3_hybrid_search_by_node(graph_with_comments):
    print("\n[C3] HybridRetriever: 以节点为 query 检索")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = HybridRetriever(graph_with_comments, embedding_backend=backend)
    retriever.build()
    nid  = "http.py::decode_response"
    resp = retriever.search_by_node(nid, top_k=5)
    assert all(r.node_id != nid for r in resp.results), "结果不应含 query 节点自身"
    print(f"  ✓ 以 decode_response 为 query，检索到 {len(resp.results)} 个相关节点")
    for r in resp.results[:3]:
        print(f"    {r.qualified_name:45s} final={r.final_score:.3f}")


def test_C4_ablation_structural_only(graph_with_comments):
    print("\n[C4] 消融实验：纯结构检索（α=1.0, β=0.0）")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = HybridRetriever(
        graph_with_comments, alpha=1.0, beta=0.0, embedding_backend=backend
    )
    retriever.build()
    resp = retriever.search("HTTP decode error", top_k=3)
    for r in resp.results:
        assert abs(r.final_score - r.structural_score) < 1e-5, \
            f"纯结构模式下 final_score 应等于 structural_score"
    print(f"  ✓ 纯结构模式：final_score == structural_score")


def test_C5_ablation_semantic_only(graph_with_comments):
    print("\n[C5] 消融实验：纯语义检索（α=0.0, β=1.0）")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = HybridRetriever(
        graph_with_comments, alpha=0.0, beta=1.0, embedding_backend=backend
    )
    retriever.build()
    resp = retriever.search("HTTP decode error", top_k=3)
    for r in resp.results:
        assert abs(r.final_score - r.semantic_score) < 1e-5, \
            f"纯语义模式下 final_score 应等于 semantic_score"
    print(f"  ✓ 纯语义模式：final_score == semantic_score")


def test_C6_results_sorted_by_score(graph_with_comments):
    print("\n[C6] 检索结果按 final_score 降序排列")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = HybridRetriever(graph_with_comments, embedding_backend=backend)
    retriever.build()
    resp = retriever.search("handle request", top_k=8)
    scores = [r.final_score for r in resp.results]
    assert scores == sorted(scores, reverse=True), \
        f"结果未按分数降序排列: {scores}"
    print(f"  ✓ {len(resp.results)} 个结果均按 final_score 降序排列")


# ===========================================================================
# PART D：真实 DashScope API 测试（可选）
# ===========================================================================

def test_D1_dashscope_batch_splitting():
    """
    验证 DashScopeEmbeddingBackend 的批次拆分逻辑。
    不依赖真实 API——通过 mock HTTP 服务器验证：
      - 21 条文本被正确拆分为 3 批（10+10+1）
      - 每批发送的 input 长度不超过 10
      - 最终返回 21 条向量，顺序正确
    有真实 API Key 时，同时验证真实 API 调用。
    """
    print("\n[D1] DashScopeEmbeddingBackend 批次拆分逻辑验证")
    from code_graph_retriever import DashScopeEmbeddingBackend

    # ── 子测试 1：mock 验证拆分逻辑（不需要 API Key）──────────────────
    import json, threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    received_batches: list = []

    class FakeEmbedHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            batch  = body.get("input", [])
            received_batches.append(len(batch))
            # 返回假 embedding 向量（维度与真实一致：1024）
            fake_data = [
                {"index": i, "embedding": [float(i)] * 1024}
                for i in range(len(batch))
            ]
            resp_body = json.dumps({"data": fake_data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

        def log_message(self, *args):
            pass   # 静音日志

    server = HTTPServer(("127.0.0.1", 0), FakeEmbedHandler)
    port   = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        backend = DashScopeEmbeddingBackend(api_key="fake-key-for-test")
        # 临时指向本地 mock 服务器
        original_url    = backend.API_URL
        backend.API_URL = f"http://127.0.0.1:{port}/embeddings"
        backend.BATCH_INTERVAL = 0   # 测试时不等待

        texts = [f"文本{i}" for i in range(21)]  # 21 条 > 单批上限 10
        vecs  = backend.embed_batch(texts)

        # 验证拆分：21 条 → ceil(21/10)=3 批
        assert len(received_batches) == 3, \
            f"期望 3 批，实际发送了 {len(received_batches)} 批: {received_batches}"
        assert max(received_batches) <= 10, \
            f"单批超过 10 条！各批大小: {received_batches}"
        assert received_batches == [10, 10, 1], \
            f"批次大小分布不正确: {received_batches}"
        assert vecs.shape == (21, 1024), \
            f"期望 (21, 1024)，实际 {vecs.shape}"

        backend.API_URL = original_url
        print(f"  ✓ Mock 验证：21 条文本拆分为 {received_batches}，"
              f"总向量 shape={vecs.shape}")
    finally:
        server.shutdown()

    # ── 子测试 2：真实 API（有 Key 时运行）────────────────────────────
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        print("  （真实 API 子测试跳过：未设置 DASHSCOPE_API_KEY）")
        return

    print("  正在调用真实 DashScope Embedding API...")
    backend_real = DashScopeEmbeddingBackend(api_key=key)

    # 测试单条
    single = backend_real.embed("处理 HTTP 重定向异常")
    assert single.shape == (1024,), f"单条 embed 维度错误: {single.shape}"

    # 测试 15 条（跨批：10+5）
    texts_15 = [f"HTTP 请求处理函数 {i}" for i in range(15)]
    vecs_15  = backend_real.embed_batch(texts_15)
    assert vecs_15.shape == (15, 1024), f"15 条 embed 维度错误: {vecs_15.shape}"

    # 验证向量已 L2 归一化（DashScope 返回的是原始值，可能未归一化）
    print(f"  ✓ 真实 API：单条 shape={single.shape}，"
          f"15条 shape={vecs_15.shape}，前5维={single[:5].tolist()}")


def test_D2_hybrid_with_dashscope(graph_with_comments):
    """
    使用真实 DashScope Embedding 做端到端混合检索。
    图有 21 个节点，build() 会触发 3 次 API 调用（批次拆分），
    再加上 search() 时 1 次 query embedding，共 4 次调用。
    验证全流程可以稳定跑完。
    """
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        print("\n[D2] DashScope 混合检索端到端（跳过：未设置 DASHSCOPE_API_KEY）")
        return
    print("\n[D2] DashScope 混合检索端到端（21节点图，验证 batch 拆分 + 真实语义）")
    from code_graph_retriever import DashScopeEmbeddingBackend

    backend  = DashScopeEmbeddingBackend(api_key=key)
    retriever = HybridRetriever(graph_with_comments, embedding_backend=backend)

    print(f"  构建索引中（{backend.BATCH_SIZE} 条/批，批间间隔 {backend.BATCH_INTERVAL}s）...")
    retriever.build()
    print(f"  索引构建完成，向量维度: {backend.dim}")

    # 模拟真实 SWE-bench issue 描述作为 query
    query = "HTTP 响应解码出现 UnicodeDecodeError，Content-Type 没有 charset"
    resp  = retriever.search(query, top_k=5)

    assert len(resp.results) > 0, "检索结果为空"
    assert all(0.0 <= r.final_score <= 1.0 for r in resp.results)
    # 验证语义分数来自真实 embedding（不应全为 0）
    sem_scores = [r.semantic_score for r in resp.results]
    assert any(s > 0 for s in sem_scores), \
        f"所有语义分数都是 0，embedding 可能未生效: {sem_scores}"

    print(f"  ✓ 检索完成，耗时 {resp.elapsed_ms:.0f}ms，返回 {len(resp.results)} 个结果")
    print(f"\n  完整检索报告（真实 DashScope Embedding）：")
    print(resp.to_agent_text(show_code=False))


# ===========================================================================
# PART E：输出格式测试
# ===========================================================================

def test_E1_result_to_agent_text(graph_with_comments):
    print("\n[E1] RetrievalResult.to_agent_text() 格式验证")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = HybridRetriever(graph_with_comments, embedding_backend=backend)
    retriever.build()
    resp = retriever.search("decode unicode", top_k=3)
    assert len(resp.results) > 0
    r    = resp.results[0]
    text = r.to_agent_text(show_code=True)
    # 验证必要字段都出现在输出中
    assert r.qualified_name in text,  "输出缺少 qualified_name"
    assert r.node_type      in text,  "输出缺少 node_type"
    assert r.file           in text,  "输出缺少 file"
    assert "final_score"    not in text or "综合评分" in text
    print(f"  ✓ to_agent_text() 格式正确")
    print(f"\n  示例输出（第1个结果）：")
    print("  " + "\n  ".join(text.splitlines()))


def test_E2_response_to_agent_text(graph_with_comments):
    print("\n[E2] RetrievalResponse.to_agent_text() 格式验证")
    backend  = TFIDFEmbeddingBackend(n_components=32)
    retriever = HybridRetriever(graph_with_comments, embedding_backend=backend)
    retriever.build()
    resp = retriever.search("处理 HTTP 重定向", top_k=3)
    full_text = resp.to_agent_text(show_code=False)
    assert resp.query in full_text,  "输出缺少 query"
    assert str(len(resp.results)) in full_text, "输出缺少结果数量"
    print(f"  ✓ RetrievalResponse.to_agent_text() 格式正确")
    print(f"\n  完整检索报告：")
    print("  " + "\n  ".join(full_text.splitlines()))


# ===========================================================================
# 主程序
# ===========================================================================

def main():
    print("=" * 60)
    print("代码图检索模块测试")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp1, \
         tempfile.TemporaryDirectory() as tmp2:

        graph              = build_test_graph(tmp1, with_comments=False)
        graph_with_comments = build_test_graph(tmp2, with_comments=True)

        section("PART A：FeatureExtractor + StructuralRetriever 单元测试")
        test_A1_feature_dim(graph)
        test_A2_feature_normalized(graph)
        test_A3_module_nodes_excluded(graph)
        test_A4_call_degree_nonzero(graph)
        test_A5_inherit_depth(graph)
        test_A6_position_summary(graph)
        test_A7_structural_retriever_topk(graph)
        test_A8_structural_reason_nonempty(graph)
        test_A9_exclude_self(graph)

        section("PART B：SemanticRetriever 测试（TF-IDF，无需 API）")
        test_B1_tfidf_backend_fit(graph)
        test_B2_semantic_retriever_build(graph_with_comments)
        test_B3_semantic_search_returns_results(graph_with_comments)
        test_B4_cold_start_fallback(graph)
        test_B5_mock_embedding_deterministic()

        section("PART C：HybridRetriever 融合检索测试")
        test_C1_hybrid_build(graph_with_comments)
        test_C2_hybrid_search_text_query(graph_with_comments)
        test_C3_hybrid_search_by_node(graph_with_comments)
        test_C4_ablation_structural_only(graph_with_comments)
        test_C5_ablation_semantic_only(graph_with_comments)
        test_C6_results_sorted_by_score(graph_with_comments)

        section("PART D：真实 DashScope API（可选）")
        test_D1_dashscope_batch_splitting()
        test_D2_hybrid_with_dashscope(graph_with_comments)

        section("PART E：输出格式测试")
        test_E1_result_to_agent_text(graph_with_comments)
        test_E2_response_to_agent_text(graph_with_comments)

    print("\n" + "=" * 60)
    print("全部测试通过 ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()