from .base import LLMProvider
from .bedrock import BedrockProvider
from .openai_compat import OpenAICompatProvider


def get_provider(config) -> LLMProvider:
    """
    Return the appropriate LLMProvider based on config.llm_provider.

    LLM_PROVIDER=bedrock  (default) — AWS Bedrock Converse API
                                       Set BEDROCK_MODEL_ID and AWS_* credentials.
    LLM_PROVIDER=ollama             — Local Ollama instance.
                                       Set OLLAMA_MODEL and optionally OLLAMA_BASE_URL.
    LLM_PROVIDER=openai             — Any OpenAI-compatible endpoint (Azure, Vertex, custom).
                                       Set OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL.
    """
    provider = config.llm_provider.lower()

    if provider == "ollama":
        return OpenAICompatProvider(
            base_url=config.ollama_base_url,
            api_key="ollama",
            model=config.ollama_model,
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


__all__ = ["LLMProvider", "BedrockProvider", "OpenAICompatProvider", "get_provider"]
