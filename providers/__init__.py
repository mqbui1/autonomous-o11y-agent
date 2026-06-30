from .base import LLMProvider
from .bedrock import BedrockProvider
from .openai_compat import OpenAICompatProvider


def get_provider(config) -> LLMProvider:
    """
    Return the appropriate LLMProvider based on config.llm_provider.

    config.llm_provider = "bedrock"  → AWS Bedrock (default)
    config.llm_provider = "openai"   → any OpenAI-compatible endpoint
                                        (Luna, Azure OpenAI, Vertex, Ollama, etc.)
    """
    if config.llm_provider == "openai":
        if not config.openai_base_url:
            raise ValueError(
                "LLM_PROVIDER=openai requires OPENAI_BASE_URL to be set. "
                "Example: OPENAI_BASE_URL=http://localhost:8080/v1"
            )
        return OpenAICompatProvider(
            base_url=config.openai_base_url,
            api_key=config.openai_api_key or "none",
            model=config.openai_model,
        )
    return BedrockProvider(
        model_id=config.bedrock_model_id,
        region=config.aws_region,
    )


__all__ = ["LLMProvider", "BedrockProvider", "OpenAICompatProvider", "get_provider"]
