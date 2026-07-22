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
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

logger = logging.getLogger(__name__)

_CAPTURE_DIR = pathlib.Path(os.getenv("TRAINING_DATA_DIR", "/root/.o11y-agent/training"))

# Defense-in-depth against two failure modes confirmed on the local fine-tuned
# model (2026-07-21 live-test regression): raw <tool_call>...</tool_call> JSON
# leaking into final prose instead of a real structured tool call, and stray
# CJK characters leaking in despite the English-only system prompt. Cheaper
# than retraining and catches the symptom regardless of root cause.
_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)
# Unclosed variant (no matching </tool_call>) — strip from the tag to end of string.
_TOOL_CALL_UNCLOSED_RE = re.compile(r"<tool_call>.*", re.DOTALL)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")


def _sanitize_final_text(text: str) -> str:
    if not text:
        return text
    cleaned = _TOOL_CALL_RE.sub("", text)
    cleaned = _TOOL_CALL_UNCLOSED_RE.sub("", cleaned)
    cleaned = _CJK_RE.sub("", cleaned)
    return cleaned.strip()


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
            final_text = _sanitize_final_text(result["text"])
            if capture:
                _save_conversation(system_prompt, initial_message, messages, final_text, tools, _start)
            return final_text

        if stop_reason == "tool_use":
            tool_uses = result["tool_uses"]
            # Guard against hallucinated tool-call explosions (e.g. 73 calls in one turn)
            _MAX_PARALLEL = 12
            if len(tool_uses) > _MAX_PARALLEL:
                logger.warning(
                    "Turn %d: model requested %d tool calls — capping at %d",
                    turn + 1, len(tool_uses), _MAX_PARALLEL,
                )
                tool_uses = tool_uses[:_MAX_PARALLEL]
                # The assistant message already appended to history contains ALL tool_use
                # blocks. Patch it to only include the IDs we're actually executing so
                # Bedrock doesn't raise ValidationException for missing toolResult blocks.
                import copy as _copy
                executed_ids = {tu["id"] for tu in tool_uses}
                patched = _copy.deepcopy(messages[-1])
                patched["content"] = [
                    b for b in patched["content"]
                    if "toolUse" not in b or b["toolUse"]["toolUseId"] in executed_ids
                ]
                messages[-1] = patched
            logger.info(
                "Turn %d: executing %d tool(s) in parallel: %s",
                turn + 1,
                len(tool_uses),
                [t["name"] for t in tool_uses],
            )
            results, id_to_result = _execute_parallel(tool_uses, tool_fns, provider)

            # Budget nudge: if the model is looping on investigative tools without
            # ever calling submit_findings (confirmed 2026-07-21/22: detector/synthetics
            # specialists cycling audit_detectors/get_broken_detectors etc. and hitting
            # max_turns without submitting), force it to wrap up before the budget runs
            # out. Appended as an extra content block in the same tool-result message
            # (not a separate message) to keep clean user/assistant alternation.
            turns_remaining = max_turns - (turn + 1)
            if 0 < turns_remaining <= 2:
                results = results + [{
                    "text": f"[REMINDER: You have {turns_remaining} turn(s) left. "
                             "Call submit_findings NOW with whatever findings you have "
                             "so far — do not call any more investigative tools.]"
                }]

            messages.append({"role": "user", "content": results})

            # Hard stop once submit_findings succeeds. submit_findings is a
            # side-effecting tool (writes structured findings into the caller's
            # collector dict) — the model's job is done at that point, regardless
            # of what it does next. Confirmed root cause of a 2026-07-21 production
            # regression ("Agent reached max turns without completing"): the model
            # kept calling more tools after "Findings recorded. Assessment
            # complete.", eventually hitting max_turns and discarding a
            # perfectly good final report in favor of a useless raw_text.
            submit_call = next((tu for tu in tool_uses if tu.get("name") == "submit_findings"), None)
            if submit_call is not None:
                submit_result = id_to_result.get(submit_call["id"], "")
                if not submit_result.lower().startswith(("tool ", "unknown tool")):
                    final_text = _sanitize_final_text(
                        submit_call.get("input", {}).get("summary") or submit_result
                    )
                    if capture:
                        _save_conversation(system_prompt, initial_message, messages, final_text, tools, _start)
                    return final_text
            continue

        logger.warning("Unexpected stop_reason: %s — stopping", stop_reason)
        break

    return "Agent reached max turns without completing."


def _execute_parallel(
    tool_uses: list[dict], tool_fns: dict[str, Callable], provider
) -> tuple[list[dict], dict[str, str]]:
    """Execute tool calls concurrently; return (toolResult content blocks, id -> raw result text)."""
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

    results = [
        provider.format_tool_result(tid, text)
        for tid, text in id_to_result.items()
    ]
    return results, id_to_result


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
