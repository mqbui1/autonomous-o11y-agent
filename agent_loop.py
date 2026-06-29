"""
Bedrock Converse API tool-calling loop.

Replaces Strands with direct boto3 calls. When Claude returns multiple
tool_use blocks in a single turn, all are executed concurrently via
ThreadPoolExecutor — eliminating the sequential bottleneck.
"""

import logging
import boto3
from botocore.config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

_BEDROCK_CONFIG = Config(read_timeout=600, connect_timeout=10, retries={"max_attempts": 2})

logger = logging.getLogger(__name__)


def run_agent(
    model_id: str,
    region: str,
    system_prompt: str,
    tools: list[dict],
    tool_fns: dict[str, Callable],
    initial_message: str,
    max_turns: int = 30,
) -> str:
    """
    Run a tool-calling loop against Bedrock Claude.

    When Claude returns multiple tool_use blocks in one turn, all are
    executed concurrently. Returns the final text response.
    """
    client = boto3.client("bedrock-runtime", region_name=region, config=_BEDROCK_CONFIG)
    messages = [{"role": "user", "content": [{"text": initial_message}]}]

    for turn in range(max_turns):
        kwargs: dict = dict(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=messages,
        )
        if tools:
            kwargs["toolConfig"] = {"tools": tools}

        response = client.converse(**kwargs)
        stop_reason = response["stopReason"]
        output_msg = response["output"]["message"]
        messages.append(output_msg)

        logger.debug("Turn %d: stopReason=%s", turn + 1, stop_reason)

        if stop_reason == "end_turn":
            return "\n".join(
                b["text"] for b in output_msg["content"] if "text" in b
            )

        if stop_reason == "tool_use":
            tool_uses = [
                b["toolUse"] for b in output_msg["content"] if "toolUse" in b
            ]
            logger.info(
                "Turn %d: executing %d tool(s) in parallel: %s",
                turn + 1,
                len(tool_uses),
                [t["name"] for t in tool_uses],
            )
            results = _execute_parallel(tool_uses, tool_fns)
            messages.append({"role": "user", "content": results})
            continue

        logger.warning("Unexpected stop reason: %s — stopping", stop_reason)
        break

    return "Agent reached max turns without completing."


def _execute_parallel(
    tool_uses: list[dict], tool_fns: dict[str, Callable]
) -> list[dict]:
    """Execute tool calls concurrently; return toolResult content blocks."""
    id_to_result: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(tool_uses)) as pool:
        futures = {
            pool.submit(_invoke, tool_fns, tu["name"], tu.get("input", {})): tu["toolUseId"]
            for tu in tool_uses
        }
        for future in as_completed(futures):
            tool_use_id = futures[future]
            try:
                id_to_result[tool_use_id] = future.result()
            except Exception as exc:
                id_to_result[tool_use_id] = f"Tool execution error: {exc}"

    return [
        {"toolResult": {"toolUseId": tid, "content": [{"text": text}]}}
        for tid, text in id_to_result.items()
    ]


def _invoke(tool_fns: dict[str, Callable], name: str, inputs: dict) -> str:
    fn = tool_fns.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    try:
        return fn(**inputs) if inputs else fn()
    except Exception as exc:
        logger.error("Tool %s failed: %s", name, exc, exc_info=True)
        return f"Tool {name} error: {exc}"
