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
_TIMEOUT_RE = re.compile(r"timed out after \d+ seconds", re.IGNORECASE)
# Every tool-execution failure funnels through one of these prefixes (see
# _invoke/_execute_parallel below) regardless of the underlying cause (HTTP
# error, missing required arg, unknown tool name). Used to detect a run where
# EVERY investigative tool call failed, so a model's confident conclusion at
# submit_findings time can be flagged as unsupported rather than trusted as-is.
_TOOL_ERROR_RE = re.compile(r"^(Tool .+ error:|Unknown tool:|Tool execution error:)")
# Confirmed 2026-07-23 (round 8 live validation): the local fine-tuned model
# sometimes emits a stray Misc Symbols/Dingbats character (e.g. U+2696 BALANCE
# SCALE "⚖") where a bullet point or newline was intended, producing prose
# like "...for flagd⚖ Enable db.statement.capture⚖ The APM traces...". Replace
# with a sentence break rather than leaving the garbled glyph in the report.
# Confirmed 2026-07-26: also observed U+2299 "⊙" (CIRCLED DOT OPERATOR) used the
# same way, outside the \u2600-\u27bf range. Added the narrow "circled operators"
# sub-range \u2295-\u229f (⊕⊖⊗⊘⊙⊚⊛...) rather than the whole Mathematical
# Operators block (\u2200-\u22ff), since that block also contains ≤/≥/∈/∑ etc.
# which are legitimate in technical prose (e.g. "P99 ≤ 100ms") and must not be
# stripped.
_STRAY_SYMBOL_RE = re.compile(r"[\u2600-\u27bf\u2295-\u229f]")
_DOUBLE_PUNCT_RE = re.compile(r"\.\s*\.")
_MULTI_SPACE_RE = re.compile(r" {2,}")
# Confirmed 2026-07-26 (astroshop-local live validation): the model sometimes
# drops the ". " separator between a numbered-list marker and the item text,
# e.g. "2APM and log linkage has missing attributes..." instead of "2. APM
# and log linkage...". Only matches a short (1-2 digit) numeral immediately
# followed by 2+ uppercase letters — real observability acronyms (APM, RCA,
# DB, RUM, etc.) in this domain, not generic shorthand like "5xx"/"100ms"/
# "4xx" (lowercase after the digit) which must be left untouched.
_MANGLED_LIST_NUMERAL_RE = re.compile(r"(?<![\d.])(\d{1,2})([A-Z]{2,})")
# Confirmed 2026-07-26 (Bedrock-vs-Ollama comparison): the synthetics specialist
# sometimes wraps its whole summary in a ChatML-style special-token delimiter pair
# — e.g. '<| AstroShop-local contains coverage gaps... |>' — reminiscent of
# <|im_start|>/<|im_end|> but a different, malformed marker. Strip a leading "<|"
# and/or trailing "|>" if they wrap the text (the closing tag is sometimes absent
# on its own, e.g. when a caller truncates the text before it, so each side is
# stripped independently rather than requiring both).
_CHATML_WRAP_LEADING_RE = re.compile(r"^\s*<\|\s*")
_CHATML_WRAP_TRAILING_RE = re.compile(r"\s*\|>\s*$")


def _strip_unbalanced_backticks(text: str) -> str:
    """Drop all backtick characters when there's an odd count — i.e. at least
    one is unpaired and leaking as a literal character instead of forming
    valid inline-code markdown. Confirmed 2026-07-26 (Bedrock-vs-Ollama
    comparison): 'In astroshop-local`, all services are showing...' — the
    model apparently intended to wrap the environment name in backticks
    (like the legitimate paired usage seen elsewhere, e.g. 'the `resolve`
    service') but only emitted one side. Balanced pairs (even count) render
    fine as inline code in the UI and are left untouched.
    """
    if text.count("`") % 2 == 1:
        return text.replace("`", "")
    return text


def _extract_fake_tool_call_summary(text: str) -> str | None:
    """Detect a JSON-encoded tool-call description masquerading as plain
    end_turn text (e.g. '[{"function_name": "submit_findings", "arguments":
    {"summary": "..."}}]') and pull out the human-readable summary instead
    of letting raw JSON leak into the final report. Confirmed 2026-07-22
    round 7: prompted by the end-turn-must-call-submit_findings nudge below,
    the model sometimes "complies" by describing the call as text instead
    of actually invoking the tool via the provider's tool-calling API.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        args = item.get("arguments") or item.get("input") or item.get("parameters")
        if isinstance(args, dict) and args.get("summary"):
            return str(args["summary"]).strip()
    return None


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n?(\{.*?\})\s*```", re.DOTALL)


def _extract_fenced_json_submit_call(text: str) -> dict | None:
    """Detect a fenced ```json code block containing a valid submit_findings-shaped
    object (has a "summary" or "issues" key) instead of a real tool call. Confirmed
    2026-07-23: the instrumentation specialist sometimes ends its turn with apology
    prose plus a fenced JSON block with the right shape, alongside a second malformed
    pseudo-call block (JS-object syntax, unquoted keys) it never manages to fix. Only
    the first well-formed JSON block is used; malformed blocks are ignored rather
    than repaired.
    """
    if not text or "```" not in text:
        return None
    for match in _FENCED_JSON_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        shape = _find_submit_shape(data)
        if shape is not None:
            return shape
    return None


def _find_submit_shape(data) -> dict | None:
    """Check a parsed JSON value for a submit_findings-shaped payload, either at
    the top level or nested one level under "arguments"/"input"/"parameters"
    (the shape the model uses when it mimics a tool-call envelope as text, e.g.
    `{"name": "submit_findings", "arguments": {"summary_issues": [...], ...}}`).
    Confirmed 2026-07-26: seen with the real issue/summary data nested under
    "arguments" while the top level only had "name"/"arguments"/"description" —
    a top-level-only key check missed it even after JSON parsing succeeded.
    """
    if not isinstance(data, dict):
        return None
    if "summary" in data or "issues" in data or "summary_issues" in data:
        return data
    nested = data.get("arguments") or data.get("input") or data.get("parameters")
    if isinstance(nested, dict) and ("summary" in nested or "issues" in nested or "summary_issues" in nested):
        return nested
    return None


def _extract_bare_json_submit_call(text: str) -> dict | None:
    """Detect an un-fenced JSON object describing submit_findings args, optionally
    wrapped in a custom tag (e.g. `<json>{...}</json>`) instead of a real tool call.
    Confirmed 2026-07-24: governance specialist emitted a raw JSON object using
    "summary_issues" (not "summary"/"issues") as plain end_turn text with no code
    fence at all -- _extract_fenced_json_submit_call requires ``` and never matched,
    so the whole JSON blob leaked verbatim into the report as the "summary". Accepts
    "summary_issues" too since make_submit_fn already normalizes that shape.

    Scans for balanced JSON objects one `{` at a time via json.JSONDecoder.raw_decode
    instead of a single greedy `\\{.*\\}` regex. Confirmed 2026-07-26: the greedy regex
    spanned from the FIRST `{` to the LAST `}` across an entire text containing two
    separate JSON blobs (e.g. `<tool_response>{...}\\n{...}</tool_response>`), producing
    one invalid concatenated blob that failed json.loads() and silently discarded both
    real objects. Also handles nested "arguments"-wrapped payloads via
    _find_submit_shape() -- works regardless of code-fence markers around the JSON,
    since the scan just looks for the next "{" character irrespective of surrounding
    ``` text.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        brace = text.find("{", idx)
        if brace == -1:
            return None
        try:
            data, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        shape = _find_submit_shape(data)
        if shape is not None:
            return shape
        idx = end
    return None


def _sanitize_final_text(text: str) -> str:
    if not text:
        return text
    cleaned = _TOOL_CALL_RE.sub("", text)
    cleaned = _TOOL_CALL_UNCLOSED_RE.sub("", cleaned)
    cleaned = _CJK_RE.sub("", cleaned)
    cleaned = _STRAY_SYMBOL_RE.sub(". ", cleaned)
    cleaned = _MANGLED_LIST_NUMERAL_RE.sub(r"\1. \2", cleaned)
    cleaned = _DOUBLE_PUNCT_RE.sub(".", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)
    cleaned = cleaned.strip()
    cleaned = _CHATML_WRAP_LEADING_RE.sub("", cleaned)
    cleaned = _CHATML_WRAP_TRAILING_RE.sub("", cleaned)
    cleaned = _strip_unbalanced_backticks(cleaned)
    cleaned = cleaned.strip()
    fake_summary = _extract_fake_tool_call_summary(cleaned)
    if fake_summary:
        cleaned = fake_summary
    return cleaned.strip()


def _call_signature(name: str, inputs: dict) -> str:
    """Stable string key for an exact (tool name, arguments) pair, used to
    detect the model repeating an identical call it already made this run."""
    try:
        return name + "|" + json.dumps(inputs, sort_keys=True, default=str)
    except Exception:
        return name + "|" + str(inputs)


def _converse_with_retry(
    provider, system_prompt: str, messages: list, native_tools: list, max_attempts: int = 3,
    force_tool: str = None,
) -> dict:
    """Retry a single converse() call if the model returns a fully degenerate
    turn (stop_reason=end_turn, no text, no tool calls). Confirmed on the
    local fine-tuned model (2026-07-22, round 6, after fixing the Ollama
    Modelfile TEMPLATE bug): with temperature=0.1 the model still
    occasionally emits a completely empty response on turn 1 (~1/3 of calls,
    non-deterministic) -- distinct from the <tool_call>-text-leak issue,
    which the TEMPLATE fix resolved. A cheap regeneration retry clears it in
    practice since it's not correlated with any specific prompt content.
    """
    for attempt in range(max_attempts):
        result = provider.converse(
            system_prompt=system_prompt, messages=messages, tools=native_tools, force_tool=force_tool,
        )
        if result["stop_reason"] == "end_turn" and not (result["text"] or "").strip():
            logger.warning("Empty end_turn response (attempt %d/%d) — retrying", attempt + 1, max_attempts)
            continue
        # Confirmed 2026-07-26 (governance false-negative investigation, run
        # 3a5f963baa): Ollama's OpenAI-compat tool_choice forcing is NOT reliably
        # honored by the local fine-tuned model — on the literal last turn with
        # force_tool="submit_findings" set, the model returned finish_reason="stop"
        # with confident plain-text prose FALSELY claiming "All findings have been
        # reported via `submit_findings`" instead of actually invoking the tool.
        # This silently discarded a real, structured governance assessment (critical
        # error-rate cardinality findings) in favor of an empty issues=[]/metrics={}
        # fallback. Retry the forced call the same way we retry empty responses above
        # — a fresh generation sometimes does honor the constraint.
        if force_tool and result["stop_reason"] != "tool_use":
            logger.warning(
                "force_tool=%s requested but model ignored tool_choice with stop_reason=%s "
                "(attempt %d/%d) — retrying",
                force_tool, result["stop_reason"], attempt + 1, max_attempts,
            )
            continue
        return result
    return result


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

    has_submit_tool = any(
        t.get("toolSpec", {}).get("name") == "submit_findings" for t in tools
    )
    submit_findings_called = False
    blank_submit_retries = 0
    already_timed_out_tools: set = set()
    seen_call_signatures: set = set()
    successful_investigative_calls = 0
    for turn in range(max_turns):
        # Force submit_findings (grammar-enforced by the provider) on the final turn
        # if it hasn't been called yet. Confirmed 2026-07-21/22: detector/synthetics/
        # rum/rca specialists sometimes cycle investigative tools without ever calling
        # submit_findings, ignoring the text-based budget nudge below (weak instruction-
        # following on the local fine-tuned model) and burning through max_turns with
        # nothing to show. A hard tool_choice constraint on the last turn guarantees
        # some structured output instead of "Agent reached max turns without completing."
        is_last_turn = turn == max_turns - 1
        force_tool = "submit_findings" if (has_submit_tool and not submit_findings_called and is_last_turn) else None
        result = _converse_with_retry(provider, system_prompt, messages, native_tools, force_tool=force_tool)
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
            turns_remaining = max_turns - (turn + 1)
            # If the model narrated the intended submit_findings call as a fenced
            # JSON block instead of actually invoking the tool, use it directly —
            # this recovers real structured findings instead of falling back to a
            # truncated raw_text[:500] summary. See _extract_fenced_json_submit_call.
            if has_submit_tool and not submit_findings_called:
                fenced_call = _extract_fenced_json_submit_call(result["text"] or "")
                submit_fn = tool_fns.get("submit_findings") if fenced_call is not None else None
                if submit_fn is not None:
                    try:
                        submit_result = submit_fn(**fenced_call)
                        submit_findings_called = True
                        final_text = _sanitize_final_text(str(fenced_call.get("summary") or submit_result))
                        if capture:
                            _save_conversation(system_prompt, initial_message, messages, final_text, tools, _start)
                        return final_text
                    except Exception as exc:
                        logger.warning("Fenced JSON submit_findings recovery failed: %s", exc)
            if has_submit_tool and not submit_findings_called:
                bare_call = _extract_bare_json_submit_call(result["text"] or "")
                submit_fn = tool_fns.get("submit_findings") if bare_call is not None else None
                if submit_fn is not None:
                    try:
                        submit_result = submit_fn(**bare_call)
                        submit_findings_called = True
                        final_text = _sanitize_final_text(str(bare_call.get("summary") or submit_result))
                        if capture:
                            _save_conversation(system_prompt, initial_message, messages, final_text, tools, _start)
                        return final_text
                    except Exception as exc:
                        logger.warning("Bare JSON submit_findings recovery failed: %s", exc)
            # Reject a plain-text end_turn and nudge the model to call
            # submit_findings instead, if it never has. Confirmed 2026-07-22
            # round 7: RCA specialist finishes investigating but responds with
            # rambling scratchpad-style plain text ("Let's take action now: ...")
            # instead of calling submit_findings — the specialist's raw_text[:500]
            # fallback then truncates this mid-sentence in the final report.
            # Was previously a one-shot retry (single nudge, then give up) — bumped
            # to nudge on EVERY plain-text end_turn while turns remain (2026-07-25):
            # confirmed live that detector/logs/synthetics specialists sometimes
            # ramble ("Let's move forward with submit_findings now...") on the
            # first end_turn, call an unrelated investigative tool instead of
            # submit_findings on the next turn, then ramble again on a second
            # end_turn — burning the single retry with nothing to show. Safe to
            # nudge repeatedly since max_turns bounds the loop and force_tool
            # (above) guarantees a real submit_findings call on the literal last
            # turn regardless of how many plain-text end_turns preceded it.
            if has_submit_tool and not submit_findings_called and turns_remaining > 0:
                messages.append({
                    "role": "user",
                    "content": [{
                        "text": "[REMINDER: Do not respond with plain text. You must call "
                                 "the submit_findings tool now with your structured results "
                                 "as your final action.]"
                    }]
                })
                continue
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
            # If a tool already timed out earlier this run, don't spend another full
            # timeout budget calling it again — the model sometimes ignores the prompt-
            # level "if it times out, do NOT retry" instruction (weak instruction-
            # following on the local fine-tuned model). Confirmed 2026-07-23: detector
            # specialist retry-looping provision_detectors after each 180s/480s timeout,
            # burning through specialist_max_turns with nothing to show.
            #
            # Separately, confirmed 2026-07-24: the detector specialist cycles
            # provision_detectors/audit_detectors/retune_detectors with the exact
            # same (empty) arguments turn after turn without ever incorporating the
            # prior result — not a timeout, each call genuinely succeeds in 1-2min,
            # it just never stops re-running the same tool with the same args. That
            # alone stretched a 12-turn run to ~8 minutes of real subprocess work.
            # Skip any exact-duplicate (name, args) call rather than paying for
            # another live re-run of identical work.
            to_skip: list[tuple[dict, str]] = []
            to_run: list[dict] = []
            for tu in tool_uses:
                if tu["name"] in already_timed_out_tools:
                    to_skip.append((tu, (
                        f"Tool {tu['name']} already timed out earlier this run — do NOT "
                        "call it again. Call submit_findings now with whatever you have."
                    )))
                    continue
                if tu["name"] != "submit_findings":
                    sig = _call_signature(tu["name"], tu.get("input", {}))
                    if sig in seen_call_signatures:
                        to_skip.append((tu, (
                            f"You already called {tu['name']} with these exact arguments "
                            "earlier this run — the result will be identical. Use the "
                            "existing result, try different arguments, or call "
                            "submit_findings now instead of repeating this call."
                        )))
                        continue
                    seen_call_signatures.add(sig)
                to_run.append(tu)

            # Confirmed 2026-07-26 (synthetics specialist, live capture): after every
            # investigative tool call this run failed (403 entitlement error, missing
            # required arg, etc.), the model still submitted a confident negative
            # conclusion ("no tests exist, no coverage gaps") with zero real supporting
            # data — directly contradicted by other runs' ground truth for the same
            # environment. If submit_findings is being called with zero successful
            # investigative calls so far this run, inject an explicit caveat into the
            # summary before it's executed, so the report reflects data unavailability
            # instead of a fabricated finding.
            if successful_investigative_calls == 0:
                submit_tu = next((tu for tu in to_run if tu["name"] == "submit_findings"), None)
                if submit_tu is not None:
                    caveat = (
                        "[DATA UNAVAILABLE — all investigative tool calls failed this "
                        "run; the following is NOT based on real data] "
                    )
                    raw_summary = submit_tu.get("input", {}).get("summary")
                    if isinstance(raw_summary, str) and raw_summary and not raw_summary.startswith(caveat):
                        submit_tu["input"]["summary"] = caveat + raw_summary

            results, id_to_result = _execute_parallel(to_run, tool_fns, provider) if to_run else ([], {})
            for tu, skip_msg in to_skip:
                id_to_result[tu["id"]] = skip_msg
                results.append(provider.format_tool_result(tu["id"], skip_msg))
            for tu in to_run:
                if _TIMEOUT_RE.search(id_to_result.get(tu["id"], "")):
                    already_timed_out_tools.add(tu["name"])
                if tu["name"] != "submit_findings" and not _TOOL_ERROR_RE.match(id_to_result.get(tu["id"], "")):
                    successful_investigative_calls += 1

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
                    submit_findings_called = True
                    raw_summary = submit_call.get("input", {}).get("summary")
                    if isinstance(raw_summary, dict):
                        # Model sometimes passes summary as a nested dict instead of a
                        # string. Confirmed 2026-07-24: this crashed the entire specialist
                        # thread with "'dict' object has no attribute 'strip'" BEFORE
                        # make_submit_fn's own dict-coercion logic (tools/findings.py) ever
                        # ran, since this line executes first. Coerce the same way here.
                        raw_summary = raw_summary.get("text") or raw_summary.get("summary") or json.dumps(raw_summary)
                    submitted_summary = str(raw_summary or "").strip()
                    # Reject a blank summary and give the model a chance to resubmit
                    # with real content, instead of silently falling through to the
                    # tool's generic "Findings recorded." string. Confirmed 2026-07-22
                    # round 7: governance/synthetics call submit_findings with summary=""
                    # (and often issues=[]) even after real, successful tool
                    # investigation — the generic fallback masked this as if nothing
                    # was wrong. Bumped from 1 to 2 retries (2026-07-25): a single
                    # retry still sometimes comes back blank a second time (db/
                    # governance/health all observed doing this live) — a 2nd chance
                    # is cheap relative to specialist_max_turns budget and gives real
                    # content a better chance to surface before falling back.
                    if not submitted_summary and blank_submit_retries < 2 and turns_remaining > 0:
                        blank_submit_retries += 1
                        results = results + [{
                            "text": "[REMINDER: Your submit_findings call had an empty "
                                     "summary. Call submit_findings again with a non-empty "
                                     "2-4 sentence summary of what you found.]"
                        }]
                        messages.append({"role": "user", "content": results})
                        continue
                    final_text = _sanitize_final_text(submitted_summary or submit_result)
                    if capture:
                        _save_conversation(system_prompt, initial_message, messages, final_text, tools, _start)
                    return final_text

            messages.append({"role": "user", "content": results})
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


_PLACEHOLDER_ARG_RE = re.compile(r"^<[^<>]{2,80}>$")


def _find_placeholder_arg(inputs: dict) -> str | None:
    """Detect the model echoing a schema hint/placeholder string back as a
    literal argument value instead of substituting a real value from a prior
    tool result. Confirmed 2026-07-23 (round 8 live validation): synthetics
    specialist called get_test_results(test_id='<test_id_value_from_previous_call>')
    three times in a row — the literal placeholder text can never return real
    data, wasting turns and a network call each time.
    """
    for key, value in inputs.items():
        if isinstance(value, str) and _PLACEHOLDER_ARG_RE.match(value.strip()):
            return key
    return None


def _invoke(tool_fns: dict[str, Callable], name: str, inputs: dict) -> str:
    fn = tool_fns.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    placeholder_key = _find_placeholder_arg(inputs)
    if placeholder_key is not None:
        return (
            f"Tool {name} error: argument '{placeholder_key}'={inputs[placeholder_key]!r} "
            "looks like an unfilled placeholder, not a real value. Use the actual "
            "value from a previous tool result's output instead."
        )
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
