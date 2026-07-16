"""
LLM tool-calling loop — provider-agnostic.

Supports AWS Bedrock and any OpenAI-compatible endpoint (Luna, Azure, Vertex, Ollama).
Provider is selected via AgentConfig.llm_provider ("bedrock" | "openai").

When the model returns multiple tool_use blocks in a single turn, all are
executed concurrently via ThreadPoolExecutor.
"""

import json
import logging
import os
import pathlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

logger = logging.getLogger(__name__)

_CAPTURE_DIR = pathlib.Path(os.getenv("TRAINING_DATA_DIR", "/root/.o11y-agent/training"))


def run_agent(
    system_prompt: str,
    tools: list[dict],
    tool_fns: dict[str, Callable],
    initial_message: str,
    provider=None,
    # Legacy kwargs kept for backward compatibility
    model_id: str = None,
    region: str = None,
    max_turns: int = 8,
) -> str:
    """
    Run a tool-calling loop against any supported LLM provider.

    provider: an LLMProvider instance. If None, a BedrockProvider is created
              using model_id and region (backward-compatible path).
    """
    if provider is None:
        from providers.bedrock import BedrockProvider
        from botocore.config import Config
        provider = BedrockProvider(model_id=model_id, region=region)

    # Qwen (and some other multilingual models) may respond in Chinese without this.
    _lang = "IMPORTANT: You MUST respond ONLY in English. Do NOT write any Chinese, Japanese, Korean, or other non-English characters under any circumstances.\n\n"
    system_prompt = _lang + system_prompt

    capture = os.getenv("CAPTURE_TRAINING_DATA", "").lower() in ("1", "true", "yes")
    _start = time.time()

    # Append language reminder to the user message too (recency bias in attention)
    initial_message = initial_message + "\n\n[REMINDER: Respond in English only.]"
    messages = [{"role": "user", "content": [{"text": initial_message}]}]
    native_tools = provider.convert_tools(tools)

    for turn in range(max_turns):
        result = provider.converse(
            system_prompt=system_prompt,
            messages=messages,
            tools=native_tools,
        )
        stop_reason = result["stop_reason"]

        # Append the assistant turn to history
        raw = result["raw_message"]
        # Normalise to Bedrock message shape for history
        if hasattr(raw, "model_dump"):
            # OpenAI response object — convert to Bedrock-like dict for history
            messages.append({"role": "assistant", "content": [{"text": result["text"]}]} if stop_reason == "end_turn"
                            else _openai_msg_to_bedrock(result))
        else:
            messages.append(raw)

        logger.debug("Turn %d: stop_reason=%s", turn + 1, stop_reason)

        if stop_reason == "end_turn":
            final_text = result["text"]
            if capture:
                _save_conversation(system_prompt, initial_message, messages, final_text, tools, _start)
            return final_text

        if stop_reason == "tool_use":
            tool_uses = result["tool_uses"]
            logger.info(
                "Turn %d: executing %d tool(s) in parallel: %s",
                turn + 1,
                len(tool_uses),
                [t["name"] for t in tool_uses],
            )
            results = _execute_parallel(tool_uses, tool_fns, provider)
            messages.append({"role": "user", "content": results})
            continue

        logger.warning("Unexpected stop_reason: %s — stopping", stop_reason)
        break

    return "Agent reached max turns without completing."


def _execute_parallel(
    tool_uses: list[dict], tool_fns: dict[str, Callable], provider
) -> list[dict]:
    """Execute tool calls concurrently; return toolResult content blocks."""
    id_to_result: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(tool_uses)) as pool:
        futures = {
            pool.submit(_invoke, tool_fns, tu["name"], tu.get("input", {})): tu["id"]
            for tu in tool_uses
        }
        for future in as_completed(futures):
            tool_use_id = futures[future]
            try:
                id_to_result[tool_use_id] = future.result()
            except Exception as exc:
                id_to_result[tool_use_id] = f"Tool execution error: {exc}"

    return [
        provider.format_tool_result(tid, text)
        for tid, text in id_to_result.items()
    ]


def _invoke(tool_fns: dict[str, Callable], name: str, inputs: dict) -> str:
    fn = tool_fns.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    try:
        import inspect
        if inputs:
            sig = inspect.signature(fn)
            valid = set(sig.parameters)
            inputs = {k: v for k, v in inputs.items() if k in valid}
        return fn(**inputs) if inputs else fn()
    except Exception as exc:
        logger.error("Tool %s failed: %s", name, exc, exc_info=True)
        return f"Tool {name} error: {exc}"


def _save_conversation(system: str, user: str, messages: list, final_text: str, tools: list, start: float) -> None:
    """Persist a completed conversation as a JSONL training example."""
    try:
        _CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "id": uuid.uuid4().hex[:12],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_seconds": round(time.time() - start, 1),
            "system": system,
            "user": user,
            "messages": messages,
            "final_text": final_text,
            "tool_names": [t.get("name", t.get("toolSpec", {}).get("name", "")) for t in tools],
        }
        path = _CAPTURE_DIR / f"{record['id']}.jsonl"
        path.write_text(json.dumps(record))
    except Exception as exc:
        logger.debug("Training data capture failed: %s", exc)


def _openai_msg_to_bedrock(result: dict) -> dict:
    """Convert an OpenAI tool_use result into a Bedrock-compatible history entry."""
    content = []
    for tu in result.get("tool_uses", []):
        content.append({
            "toolUse": {
                "toolUseId": tu["id"],
                "name": tu["name"],
                "input": tu.get("input", {}),
            }
        })
    return {"role": "assistant", "content": content}
