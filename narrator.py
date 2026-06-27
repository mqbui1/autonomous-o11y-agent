"""
Narrative synthesis — takes the agent's full conversation and generates
a structured, plain-language summary of what was found and what was done.
"""

import json
import boto3
import logging

logger = logging.getLogger(__name__)

NARRATOR_PROMPT = """\
You are summarizing the output of an autonomous observability agent run for a
Splunk Observability Cloud environment.

Given the agent's full activity log below, produce a concise executive summary with
these sections:

## Environment
One line: realm, environment name, timestamp.

## What I Found
Bullet list of key findings — instrumentation gaps, cardinality issues, detector
coverage gaps. Include specific metrics, scores, or numbers wherever available.

## What I Did
Bullet list of actions taken (if auto_apply was enabled) or recommended (if dry-run).

## Requires Human Review
Bullet list of items that need a human decision — things the agent flagged but did
not act on automatically.

## Health Snapshot
A simple table with columns: Area | Status | Key Metric
Include rows for: APM, Detectors, Cardinality, Instrumentation, Collectors.
Status should be one of: ✅ Healthy | ⚠️ Attention | 🔴 Critical

Keep the summary under 400 words. Be specific — include metric names, service names,
MTS counts, and score values from the agent output where available.
"""


def generate_narrative(
    agent_output: str,
    config,
    model_id: str = None,
) -> str:
    """
    Call Bedrock Claude directly (not via Strands) to synthesize the agent output
    into a plain-language summary.

    Args:
        agent_output: The full text output from the agent run.
        config: AgentConfig instance.
        model_id: Override the Bedrock model ID.
    """
    model = model_id or config.bedrock_model_id
    client = boto3.client("bedrock-runtime", region_name=config.aws_region)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"{NARRATOR_PROMPT}\n\n"
                    f"---AGENT OUTPUT---\n{agent_output}\n---END---\n\n"
                    f"Environment: realm={config.realm}, environment={config.environment}"
                ),
            }
        ],
    }

    try:
        response = client.invoke_model(
            modelId=model,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]
    except Exception as e:
        logger.warning(f"Narrator failed: {e}")
        return f"[Narrative generation failed: {e}]\n\nRaw agent output:\n{agent_output}"
