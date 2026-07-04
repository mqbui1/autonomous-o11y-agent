"""AWS Bedrock Converse API provider."""

import logging
import time

import boto3
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError

from .base import LLMProvider

logger = logging.getLogger(__name__)

# Fresh client per call — no long-lived TCP connection that can go stale
_BEDROCK_CONFIG = Config(read_timeout=600, connect_timeout=30, retries={"max_attempts": 3})

_RETRY_DELAYS = [2, 5, 15]  # seconds between retries on connection errors


class BedrockProvider(LLMProvider):
    def __init__(self, model_id: str, region: str):
        self.model_id = model_id
        self.region = region

    def _new_client(self):
        return boto3.client(
            "bedrock-runtime", region_name=self.region, config=_BEDROCK_CONFIG
        )

    def convert_tools(self, tools: list[dict]) -> list:
        return tools

    def converse(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> dict:
        kwargs = dict(
            modelId=self.model_id,
            system=[{"text": system_prompt}],
            messages=messages,
        )
        if tools:
            kwargs["toolConfig"] = {"tools": tools}

        last_exc = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                logger.warning("Bedrock connection error (attempt %d), retrying in %ds…", attempt, delay)
                time.sleep(delay)
            try:
                # Create a fresh client each attempt — avoids stale TCP connections
                response = self._new_client().converse(**kwargs)
                break
            except EndpointConnectionError as exc:
                last_exc = exc
        else:
            raise last_exc

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
