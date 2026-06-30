"""AWS Bedrock Converse API provider."""

import boto3
from botocore.config import Config

from .base import LLMProvider

_BEDROCK_CONFIG = Config(read_timeout=600, connect_timeout=10, retries={"max_attempts": 2})


class BedrockProvider(LLMProvider):
    def __init__(self, model_id: str, region: str):
        self.model_id = model_id
        self.region = region
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                "bedrock-runtime", region_name=self.region, config=_BEDROCK_CONFIG
            )
        return self._client

    def convert_tools(self, tools: list[dict]) -> list:
        # Bedrock toolSpec format is the internal canonical format — pass through unchanged
        return tools

    def converse(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> dict:
        kwargs = dict(
            modelId=self.model_id,
            system=[{"text": system_prompt}],
            messages=messages,
        )
        if tools:
            kwargs["toolConfig"] = {"tools": tools}

        response = self._get_client().converse(**kwargs)
        stop_reason = response["stopReason"]
        output_msg = response["output"]["message"]

        if stop_reason == "end_turn":
            text = "\n".join(b["text"] for b in output_msg["content"] if "text" in b)
            return {"stop_reason": "end_turn", "text": text, "tool_uses": [], "raw_message": output_msg}

        if stop_reason == "tool_use":
            tool_uses = [
                {"id": b["toolUse"]["toolUseId"], "name": b["toolUse"]["name"], "input": b["toolUse"].get("input", {})}
                for b in output_msg["content"]
                if "toolUse" in b
            ]
            return {"stop_reason": "tool_use", "text": "", "tool_uses": tool_uses, "raw_message": output_msg}

        return {"stop_reason": stop_reason, "text": "", "tool_uses": [], "raw_message": output_msg}

    def format_tool_result(self, tool_use_id: str, content: str) -> dict:
        return {"toolResult": {"toolUseId": tool_use_id, "content": [{"text": content}]}}
