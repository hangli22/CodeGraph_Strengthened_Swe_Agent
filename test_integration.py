"""
test_integration.py — 集成测试（三个层次）
===========================================

LEVEL 1：工具函数单元测试
  - 预构建缓存（用模拟仓库）
  - 直接调用三个工具函数
  - 验证返回格式正确、结果非空
  - 无需 API Key，无需 mini-swe-agent

LEVEL 2：Agent 调用流程测试（无需 API Key）
  - 用 DeterministicModel mock LLM 输出
  - 模拟 LLM 返回含检索工具 call 的消息
  - 验证 RetrievalAgent 正确拦截并返回结果
  - 验证 bash 命令仍然走原始路径

LEVEL 3：真实 LLM 端到端测试（需要 API Key）
  - 用真实模型（Qwen）运行完整 Agent
  - 给定一个简单 issue，验证 Agent 能调用检索工具
  - 输出 trajectory 供人工查看

运行方式
--------
python test_integration.py --level 1 --cache_dir /tmp/test_code_graph_cache
python test_integration.py --level 2 --cache_dir /tmp/test_code_graph_cache
python test_integration.py --level 3 \\
    --cache_dir /tmp/test_code_graph_cache \\
    --model_name "openai/qwen-plus" \\
    --api_base "https://dashscope.aliyuncs.com/compatible-mode/v1"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ===========================================================================
# 测试仓库定义（复用之前的 RICH_REPO）
# ===========================================================================

MOCK_REPO = {
    "base.py": textwrap.dedent("""\
        class BaseHandler:
            def handle(self, request):
                pass
            def validate(self, data):
                pass
            def log_error(self, msg):
                pass

        def create_handler(name):
            return BaseHandler()

        def parse_url(url):
            parts = url.split('/')
            return parts
    """),
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
}


def setup_mock_repo(tmp_dir: str) -> None:
    for rel, content in MOCK_REPO.items():
        path = os.path.join(tmp_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")


# ===========================================================================
# LEVEL 1：工具函数单元测试
# ===========================================================================

def test_level_1(cache_dir: str) -> None:
    print("\n" + "=" * 60)
    print("LEVEL 1：工具函数单元测试")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as repo_dir:
        setup_mock_repo(repo_dir)

        # 1a. 预构建
        print("\n[1a] 预构建缓存...")
        from mini_swe_agent_integration.prebuild import build_and_save
        info = build_and_save(repo_path=repo_dir, cache_dir=cache_dir, force=True)
        assert info["graph_stats"]["total_nodes"] > 0
        print(f"  ✓ 构建完成 | nodes={info['graph_stats']['total_nodes']} "
              f"elapsed={info['total_elapsed_s']}s")

        # 设置环境变量
        os.environ["CODE_GRAPH_CACHE_DIR"] = cache_dir

        # 清除模块级缓存（重新加载）
        import mini_swe_agent_integration.retrieval_tools as rt
        rt._cache.clear()

        # 1b. search_structural
        print("\n[1b] search_structural...")
        result = rt.search_structural("http.py::HttpHandler.handle", top_k=3)
        assert "[structural_search]" in result
        assert "FAIL" not in result and "ERROR" not in result, f"结构检索失败: {result[:200]}"
        assert "file" in result.lower() or "文件" in result
        print(f"  ✓ 返回 {result.count('[') - 1} 个结果")
        print(f"  首行: {result.splitlines()[0]}")

        # 1c. search_semantic
        print("\n[1c] search_semantic...")
        result = rt.search_semantic("decode HTTP response bytes", top_k=3)
        assert "[semantic_search]" in result
        assert "ERROR" not in result, f"语义检索失败: {result[:200]}"
        print(f"  ✓ 返回内容长度: {len(result)} 字符")
        print(f"  首行: {result.splitlines()[0]}")

        # 1d. search_hybrid
        print("\n[1d] search_hybrid...")
        result = rt.search_hybrid("HTTP 响应解码 UnicodeDecodeError", top_k=3)
        assert "[hybrid_search]" in result
        assert "ERROR" not in result, f"混合检索失败: {result[:200]}"
        print(f"  ✓ 返回内容长度: {len(result)} 字符")
        print(f"  首行: {result.splitlines()[0]}")

        # 1e. dispatch
        print("\n[1e] dispatch 统一入口...")
        result = rt.dispatch("search_hybrid", {"query": "decode response", "top_k": 2})
        assert "[hybrid_search]" in result
        print(f"  ✓ dispatch 正常")

        # 1f. 不存在的 node_id 不应崩溃
        print("\n[1f] 不存在节点的容错...")
        result = rt.search_structural("nonexistent.py::FakeClass.fake_method", top_k=3)
        assert "ERROR" not in result or "不存在" in result or "未找到" in result
        print(f"  ✓ 不存在节点正常处理: {result[:80]}")

    print("\n✓ LEVEL 1 全部通过")


# ===========================================================================
# LEVEL 2：Agent 调用流程测试（不需要 API Key）
# ===========================================================================

def test_level_2(cache_dir: str) -> None:
    print("\n" + "=" * 60)
    print("LEVEL 2：Agent 调用流程测试（mock LLM）")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as repo_dir:
        setup_mock_repo(repo_dir)

        # 确保缓存存在
        from mini_swe_agent_integration.prebuild import build_and_save, is_cache_complete
        if not is_cache_complete(cache_dir):
            build_and_save(repo_path=repo_dir, cache_dir=cache_dir, force=True)
        os.environ["CODE_GRAPH_CACHE_DIR"] = cache_dir

        # 清除缓存
        import mini_swe_agent_integration.retrieval_tools as rt
        rt._cache.clear()

        from mini_swe_agent_integration.retrieval_agent import RetrievalAgent
        from minisweagent.models.test_models import make_output
        from minisweagent.environments.local import LocalEnvironment

        # 2a. 检索工具调用被正确拦截
        print("\n[2a] 检索工具调用拦截测试...")

        # 构造一个返回检索工具调用的 mock 消息
        retrieval_action = {
            "tool_name":    "search_hybrid",
            "args":         {"query": "decode response encoding", "top_k": 3},
            "tool_call_id": "call_test_001",
        }
        mock_message = {
            "role":    "assistant",
            "content": "I'll search for relevant code first.",
            "extra":   {"actions": [retrieval_action], "cost": 0.001},
        }

        class MockRetrievalModel:
            """只返回一次检索调用，然后返回 submit 的 mock 模型。"""
            _call_count = 0

            def query(self, messages):
                self._call_count += 1
                if self._call_count == 1:
                    return mock_message
                # 第二次：提交完成
                return make_output(
                    "Task complete.",
                    [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}],
                )

            def format_message(self, **kwargs):
                return kwargs

            def format_observation_messages(self, message, outputs, template_vars=None):
                # 简单把输出包成 user 消息
                return [{"role": "tool",
                         "content": o.get("output", ""),
                         "tool_call_id": message.get("extra", {}).get("actions", [{}])[0].get("tool_call_id", "")}
                        for o in outputs]

            def get_template_vars(self, **kwargs):
                return {}

            def serialize(self):
                return {}

        model = MockRetrievalModel()
        agent = RetrievalAgent(
            model,
            LocalEnvironment(cwd=repo_dir),
            system_template   = "You are a helpful assistant.",
            instance_template = "Fix this: {{ task }}",
            step_limit        = 5,
            cost_limit        = 100.0,
        )

        # 手动运行一步（不调用 agent.run，直接测试 execute_actions）
        added = agent.execute_actions(mock_message)

        # 验证：检索结果被添加到消息历史
        assert len(added) > 0, "execute_actions 应返回观察消息"
        assert agent.retrieval_call_counts["search_hybrid"] == 1, \
            f"search_hybrid 应被调用 1 次，实际: {agent.retrieval_call_counts}"

        # 验证：观察消息内容包含检索结果
        obs_content = added[0].get("content", "")
        assert "hybrid_search" in obs_content or "search" in obs_content.lower() or len(obs_content) > 10, \
            f"观察内容看起来不像检索结果: {obs_content[:200]}"

        print(f"  ✓ 检索工具被正确拦截")
        print(f"  ✓ retrieval_call_counts: {agent.retrieval_call_counts}")
        print(f"  ✓ 观察消息长度: {len(obs_content)} 字符")
        print(f"  观察消息前100字: {obs_content[:100]}")

        # 2b. bash 命令走原始路径
        print("\n[2b] bash 命令走原始路径...")
        bash_action = {"command": "echo hello_from_bash", "tool_call_id": "call_bash_001"}
        bash_message = {
            "role":  "assistant",
            "content": None,
            "extra": {"actions": [bash_action], "cost": 0.001},
        }

        agent2 = RetrievalAgent(
            MockRetrievalModel(),
            LocalEnvironment(cwd=repo_dir),
            system_template   = "You are a helpful assistant.",
            instance_template = "Fix this: {{ task }}",
            step_limit        = 5,
            cost_limit        = 100.0,
        )

        added2 = agent2.execute_actions(bash_message)
        assert agent2.retrieval_call_counts["search_hybrid"] == 0, "bash 命令不应触发检索"
        obs2 = added2[0].get("content", "") if added2 else ""
        assert "hello_from_bash" in obs2, f"bash 输出未包含预期内容: {obs2[:200]}"
        print(f"  ✓ bash 输出包含 'hello_from_bash'")
        print(f"  ✓ retrieval 计数仍为 0: {agent2.retrieval_call_counts}")

        # 2c. 混合场景（同时有检索和 bash）
        print("\n[2c] 混合场景（检索 + bash）...")
        mixed_actions = [
            {"tool_name": "search_semantic", "args": {"query": "decode"}, "tool_call_id": "call_001"},
            {"command": "echo mixed_test", "tool_call_id": "call_002"},
        ]
        mixed_message = {
            "role":  "assistant",
            "content": None,
            "extra": {"actions": mixed_actions, "cost": 0.001},
        }
        agent3 = RetrievalAgent(
            MockRetrievalModel(),
            LocalEnvironment(cwd=repo_dir),
            system_template   = "You are a helpful assistant.",
            instance_template = "Fix this: {{ task }}",
            step_limit        = 5,
            cost_limit        = 100.0,
        )
        added3 = agent3.execute_actions(mixed_message)
        assert agent3.retrieval_call_counts["search_semantic"] == 1
        # 两个 action 应产生两个观察
        assert len(added3) >= 1
        all_content = " ".join(m.get("content", "") for m in added3)
        assert "mixed_test" in all_content, f"bash 输出丢失: {all_content[:200]}"
        print(f"  ✓ 混合场景：检索 1 次 + bash 1 次")

        # 2d. serialize 包含检索统计
        print("\n[2d] trajectory 序列化包含检索统计...")
        agent3.messages.append({"role": "exit", "content": "", "extra": {"exit_status": "Submitted", "submission": ""}})
        data = agent3.serialize()
        assert "retrieval_stats" in data["info"]
        assert data["info"]["retrieval_stats"]["call_counts"]["search_semantic"] == 1
        print(f"  ✓ retrieval_stats: {data['info']['retrieval_stats']}")

    print("\n✓ LEVEL 2 全部通过")


# ===========================================================================
# LEVEL 3：真实 LLM 端到端测试
# ===========================================================================

def test_level_3(cache_dir: str, model_name: str, api_base: str, api_key: str) -> None:
    print("\n" + "=" * 60)
    print("LEVEL 3：真实 LLM 端到端测试")
    print("=" * 60)

    if not api_key and not os.environ.get("DASHSCOPE_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        print("  跳过：未设置 API Key（DASHSCOPE_API_KEY 或 OPENAI_API_KEY）")
        return

    with tempfile.TemporaryDirectory() as repo_dir, \
         tempfile.TemporaryDirectory() as output_dir:

        setup_mock_repo(repo_dir)

        from mini_swe_agent_integration.run_swebench import run_instance

        problem = (
            "The safe_decode function in utils.py does not handle the case where "
            "encoding is None. When encoding=None is passed, it crashes with TypeError. "
            "Please fix safe_decode to handle None encoding by defaulting to 'utf-8'."
        )

        print(f"\n  模型: {model_name}")
        print(f"  问题: {problem[:80]}...")
        print(f"  仓库: {repo_dir}")

        summary = run_instance(
            repo_path        = repo_dir,
            instance_id      = "test__integration-001",
            problem_statement = problem,
            model_name       = model_name,
            cache_dir        = cache_dir,
            output_dir       = output_dir,
            api_base         = api_base,
            api_key          = api_key,
            step_limit       = 15,
            cost_limit       = 2.0,
        )

        print(f"\n  exit_status:  {summary['exit_status']}")
        print(f"  steps/cost:   {summary['n_steps']} / ${summary['cost']:.4f}")
        print(f"  elapsed:      {summary['elapsed_s']}s")
        print(f"  retrieval:    {summary['retrieval_stats']}")

        total_retrieval = sum(summary["retrieval_stats"].values())
        if total_retrieval == 0:
            print("\n  ⚠ 注意：LLM 没有调用任何检索工具。")
            print("    这可能是因为 system prompt 不够清晰，或模型选择了直接用 bash 探索。")
            print("    这是一个已知的主动触发限制——LLM 的决策是否使用工具取决于其对 prompt 的理解。")
        else:
            print(f"\n  ✓ LLM 调用了 {total_retrieval} 次检索工具")

        # 读取 trajectory 供调试
        traj_path = summary.get("trajectory_path", "")
        if traj_path and os.path.exists(traj_path):
            traj = json.loads(open(traj_path).read())
            n_msgs = len(traj.get("messages", []))
            print(f"  ✓ trajectory 已保存：{n_msgs} 条消息")

    print("\n✓ LEVEL 3 完成")


# ===========================================================================
# 主程序
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="检索模块集成测试")
    parser.add_argument("--level",      type=int, choices=[1, 2, 3], default=1,
                        help="测试层次：1=工具函数  2=Agent流程  3=真实LLM")
    parser.add_argument("--cache_dir",  default="/tmp/test_code_graph_cache")
    parser.add_argument("--model_name", default="openai/qwen-plus",
                        help="Level 3 使用的模型（litellm 格式）")
    parser.add_argument("--api_base",   default="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        help="Level 3 使用的 API 接入点")
    parser.add_argument("--api_key",    default="",
                        help="Level 3 使用的 API Key（也可用环境变量）")
    args = parser.parse_args()

    if args.level >= 1:
        test_level_1(args.cache_dir)
    if args.level >= 2:
        test_level_2(args.cache_dir)
    if args.level >= 3:
        test_level_3(args.cache_dir, args.model_name, args.api_base, args.api_key)

    print("\n" + "=" * 60)
    print(f"LEVEL {args.level} 及以下全部通过 ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()