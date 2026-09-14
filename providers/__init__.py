import logging

from .base import LLMProvider
from .bedrock import BedrockProvider
from .openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

# Fixed, deterministic specialist -> instance-slot assignment for splitting
# work across config.ollama_base_urls (round-robin by position). Explicit
# dict rather than hash() so assignment is stable across processes/restarts
# (Python's hash() is randomized per-process via PYTHONHASHSEED).
_SPECIALIST_SLOT = {
    "health": 0, "instrumentation": 1, "governance": 0, "detector": 1,
    "logs": 0, "rum": 1, "rca": 0, "synthetics": 1, "db": 0, "performance": 1,
}


def _pick_ollama_base_url(config, specialist: str | None) -> str:
    urls = getattr(config, "ollama_base_urls", None) or [config.ollama_base_url]
    if len(urls) <= 1 or not specialist:
        return urls[0]
    slot = _SPECIALIST_SLOT.get(specialist, 0) % len(urls)
    return urls[slot]


def _pick_ollama_model(config, specialist: str | None) -> str:
    overrides = getattr(config, "specialist_model_overrides", None) or {}
    if specialist and specialist in overrides:
        return overrides[specialist]
    return config.ollama_model


def get_provider(config, specialist: str | None = None) -> LLMProvider:
    """
    Return the appropriate LLMProvider based on config.llm_provider.

    LLM_PROVIDER=bedrock  (default) — AWS Bedrock Converse API
                                       Set BEDROCK_MODEL_ID and AWS_* credentials.
    LLM_PROVIDER=ollama             — Local Ollama instance.
                                       Set OLLAMA_MODEL and optionally OLLAMA_BASE_URL
                                       (or OLLAMA_BASE_URLS for a multi-instance split —
                                       see _SPECIALIST_SLOT above).
    LLM_PROVIDER=openai             — Any OpenAI-compatible endpoint (Azure, Vertex, custom).
                                       Set OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL.

    `specialist` (e.g. "health", "rca") lets callers route to a dedicated
    Ollama instance when OLLAMA_BASE_URLS lists more than one URL. Ignored
    for bedrock/openai and when only one Ollama URL is configured.
    """
    provider = config.llm_provider.lower()

    if provider == "ollama":
        model = _pick_ollama_model(config, specialist)
        if specialist:
            logger.info("Provider routing: specialist=%s model=%s", specialist, model)
        return OpenAICompatProvider(
            base_url=_pick_ollama_base_url(config, specialist),
            api_key="ollama",
            model=model,
            timeout=float(config.specialist_timeout),
            max_tokens=config.specialist_max_output_tokens,
        )

    if provider == "openai":
        if not config.openai_base_url:
            raise ValueError(
                "LLM_PROVIDER=openai requires OPENAI_BASE_URL. "
                "Example: OPENAI_BASE_URL=http://localhost:8080/v1"
            )
        return OpenAICompatProvider(
            base_url=config.openai_base_url,
            api_key=config.openai_api_key or "none",
            model=config.openai_model,
            timeout=float(config.specialist_timeout),
            max_tokens=config.specialist_max_output_tokens,
        )

    # Default: bedrock
    if not config.bedrock_model_id:
        raise ValueError(
            "LLM_PROVIDER=bedrock requires BEDROCK_MODEL_ID to be set in your .env. "
            "Example: BEDROCK_MODEL_ID=arn:aws:bedrock:us-west-2:ACCOUNT:application-inference-profile/ID"
        )
    return BedrockProvider(
        model_id=config.bedrock_model_id,
        region=config.aws_region,
    )


def check_provider_health(config) -> tuple[bool, str]:
    """
    Returns (True, "") if the provider is ready, or (False, reason) if not.
    For Bedrock, performs a cheap STS credential check.
    """
    if config.llm_provider.lower() == "bedrock":
        provider = get_provider(config)
        if not provider.is_token_valid():
            return False, "AWS credentials expired. Run deploy/refresh-aws-creds.sh to update."
    return True, ""


__all__ = ["LLMProvider", "BedrockProvider", "OpenAICompatProvider", "get_provider", "check_provider_health"]
