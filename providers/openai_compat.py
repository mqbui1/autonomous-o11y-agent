"""
OpenAI-compatible provider.

Works with any endpoint that implements the OpenAI Chat Completions API:
  - Galileo Luna (self-hosted)
  - Azure OpenAI
  - Google Vertex AI (via openai compatibility layer)
  - Ollama (local models)
  - Any OpenAI-API-compatible server
"""

import json
import logging

from .base import LLMProvider

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False


class OpenAICompatProvider(LLMProvider):
    """
    LLM provider for any OpenAI-compatible Chat Completions endpoint.

    Handles schema conversion from Bedrock toolSpec format to OpenAI function format,
    and maps message/response shapes between the two APIs.
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        if not _OPENAI_AVAILABLE:
            raise ImportError(
                "openai package is required for LLM_PROVIDER=openai. "
                "Install with: pip install openai>=1.0.0"
            )
        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key)

    def convert_tools(self, tools: list[dict]) -> list:
        """Convert Bedrock toolSpec format → OpenAI function format."""
        converted = []
        for tool in tools:
            spec = tool.get("toolSpec", {})
            schema = spec.get("inputSchema", {}).get("json", {})
            converted.append({
                "type": "function",
                "function": {
                    "name": spec.get("name", ""),
                    "description": spec.get("description", ""),
                    "parameters": schema,
                },
            })
        return converted

    def converse(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> dict:
        openai_messages = [{"role": "system", "content": system_prompt}]

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", [])

            if isinstance(content, str):
                openai_messages.append({"role": role, "content": content})
                continue

            # Bedrock content blocks → OpenAI messages
            for block in content:
                if "text" in block:
                    openai_messages.append({"role": role, "content": block["text"]})
                elif "toolUse" in block:
                    tu = block["toolUse"]
                    openai_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": tu["toolUseId"],
                            "type": "function",
                            "function": {
                                "name": tu["name"],
                                "arguments": json.dumps(tu.get("input", {})),
                            },
                        }],
                    })
                elif "toolResult" in block:
                    tr = block["toolResult"]
                    text = " ".join(
                        c.get("text", "") for c in tr.get("content", []) if "text" in c
                    )
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["toolUseId"],
                        "content": text,
                    })

        kwargs = {
            "model": self.model,
            "messages": openai_messages,
        }
        if tools:
            kwargs["tools"] = self.convert_tools(tools)
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        finish_reason = choice.finish_reason

        if finish_reason == "tool_calls":
            tool_uses = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json.loads(tc.function.arguments or "{}"),
                }
                for tc in (choice.message.tool_calls or [])
            ]
            return {
                "stop_reason": "tool_use",
                "text": "",
                "tool_uses": tool_uses,
                "raw_message": choice.message,
            }

        text = choice.message.content or ""
        return {"stop_reason": "end_turn", "text": text, "tool_uses": [], "raw_message": choice.message}

    def format_tool_result(self, tool_use_id: str, content: str) -> dict:
        # OpenAI tool results use Bedrock's toolResult block shape so agent_loop
        # can handle both providers uniformly — the conversion happens in converse()
        return {"toolResult": {"toolUseId": tool_use_id, "content": [{"text": content}]}}
