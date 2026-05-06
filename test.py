from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from openai import OpenAI


DEFAULT_BASE_URL = "https://uni-api.cstcloud.cn/v1"
DEFAULT_MODEL = "deepseek-v3:671b"


def build_client(api_key: str, base_url: str) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key)


def run_chat_test(
    client: OpenAI,
    model: str,
    message: str,
    temperature: float = 0.0,
) -> dict[str, Any]:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": message}],
        temperature=temperature,
    )

    choice = resp.choices[0]
    msg = choice.message

    result: dict[str, Any] = {
        "content": (msg.content or "").strip(),
        "finish_reason": choice.finish_reason,
        "tool_calls": [],
        "refusal": getattr(msg, "refusal", None),
        "usage": resp.usage.model_dump() if getattr(resp, "usage", None) else None,
    }

    if getattr(msg, "tool_calls", None):
        for tc in msg.tool_calls:
            result["tool_calls"].append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
            )

    try:
        result["raw_message"] = msg.model_dump()
    except Exception:
        result["raw_message"] = str(msg)

    return result


def run_tool_test(
    client: OpenAI,
    model: str,
    temperature: float = 0.0,
) -> dict[str, Any]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo_tool",
                "description": "回显输入字符串。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要回显的文本"}
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": "请不要直接回答文字，而是调用 echo_tool，并传入 text='deepseek tool test'。",
            }
        ],
        tools=tools,
        tool_choice="auto",
        temperature=temperature,
    )

    choice = resp.choices[0]
    msg = choice.message

    result: dict[str, Any] = {
        "content": msg.content,
        "finish_reason": choice.finish_reason,
        "tool_calls": [],
        "refusal": getattr(msg, "refusal", None),
        "usage": resp.usage.model_dump() if getattr(resp, "usage", None) else None,
    }

    if msg.tool_calls:
        for tc in msg.tool_calls:
            result["tool_calls"].append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
            )

    try:
        result["raw_message"] = msg.model_dump()
    except Exception:
        result["raw_message"] = str(msg)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="最小化测试 Uni-API 上的 DeepSeek 聊天与工具调用能力")
    parser.add_argument("--base_url", default=DEFAULT_BASE_URL, help="OpenAI 兼容 base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="要测试的聊天模型")
    parser.add_argument(
        "--message",
        default="请只回复这四个字：测试成功。不要输出其他内容。",
        help="聊天测试消息",
    )
    parser.add_argument(
        "--api_key",
        default=None,
        help="显式传入 API Key；为空时读取环境变量 UNI_API_KEY",
    )
    parser.add_argument(
        "--tool_test",
        action="store_true",
        help="额外测试 tool calling 是否可用",
    )
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("UNI_API_KEY", "")
    if not api_key:
        print("[ERROR] 未找到 API Key。请设置 UNI_API_KEY，或使用 --api_key 显式传入。", file=sys.stderr)
        return 2

    print(f"[INFO] base_url={args.base_url}")
    print(f"[INFO] model={args.model}")
    print(f"[INFO] has_api_key={bool(api_key)} key_len={len(api_key)}")

    try:
        client = build_client(api_key=api_key, base_url=args.base_url)
        chat_result = run_chat_test(client, args.model, args.message)

        print("[CHAT_RESULT]")
        print(json.dumps(chat_result, ensure_ascii=False, indent=2))

        content = chat_result.get("content", "")
        finish_reason = chat_result.get("finish_reason")
        tool_calls = chat_result.get("tool_calls", [])

        if content:
            print("[CHAT_OK] 模型返回了非空文本内容")
        elif tool_calls:
            print("[CHAT_WARN] 模型没有返回文本，但返回了 tool_calls；这说明请求成功，但不是普通文本回复")
        else:
            print("[CHAT_WARN] 请求成功返回，但 content 为空，且没有 tool_calls；这次测试不能算通过")
    except Exception as e:
        print(f"[CHAT_FAIL] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.tool_test:
        try:
            tool_result = run_tool_test(client, args.model)
            print("[TOOL_TEST_RESULT]")
            print(json.dumps(tool_result, ensure_ascii=False, indent=2))
            if tool_result.get("tool_calls"):
                print("[TOOL_OK] 模型返回了 tool_calls")
            else:
                print("[TOOL_WARN] 模型未返回 tool_calls，请检查该模型是否支持函数调用")
        except Exception as e:
            print(f"[TOOL_FAIL] {type(e).__name__}: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())