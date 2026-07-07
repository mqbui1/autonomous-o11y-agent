import os
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).parent


@dataclass
class AgentConfig:
    realm: str
    token: str
    environment: str
    auto_apply: bool = False
    service: str = ""

    # Paths to sibling projects — override via env vars or constructor
    provisioner_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("PROVISIONER_PATH", str(_HERE.parent / "auto-detector-provisioner"))
        )
    )
    governance_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("GOVERNANCE_PATH", str(_HERE.parent / "o11y-usage-governance"))
        )
    )
    analyzer_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("ANALYZER_PATH", str(_HERE.parent / "o11y-instrumentation-analyzer"))
        )
    )
    health_check_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("HEALTH_CHECK_PATH", str(_HERE.parent / "splunk-o11y-health-check"))
        )
    )

    aws_region: str = field(
        default_factory=lambda: os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    )
    # ── LLM provider ──────────────────────────────────────────────────────────
    # Values: "bedrock" | "ollama" | "openai"
    llm_provider: str = field(
        default_factory=lambda: os.environ.get("LLM_PROVIDER", "bedrock")
    )

    # Bedrock
    bedrock_model_id: str = field(
        default_factory=lambda: os.environ.get("BEDROCK_MODEL_ID", "")
    )

    # Ollama (local)
    ollama_base_url: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")
    )
    ollama_model: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
    )

    # OpenAI-compatible (Azure, Vertex, custom endpoints)
    openai_base_url: str = field(
        default_factory=lambda: os.environ.get("OPENAI_BASE_URL", "")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENAI_API_KEY", "")
    )
    openai_model: str = field(
        default_factory=lambda: os.environ.get("OPENAI_MODEL", "")
    )

    subprocess_timeout: int = field(
        default_factory=lambda: int(os.environ.get("TOOL_TIMEOUT", "60"))
    )
    specialist_timeout: int = field(
        default_factory=lambda: int(os.environ.get("SPECIALIST_TIMEOUT", "900"))
    )

    # ── Streaming mode (gateway co-deployment) ────────────────────────────────
    streaming_port: int = field(
        default_factory=lambda: int(os.environ.get("OTLP_RECEIVER_PORT", "4318"))
    )
    streaming_host: str = field(
        default_factory=lambda: os.environ.get("OTLP_RECEIVER_HOST", "0.0.0.0")
    )
    alert_webhook_url: str = field(
        default_factory=lambda: os.environ.get("ALERT_WEBHOOK_URL", "")
    )
    alert_cooldown_seconds: int = field(
        default_factory=lambda: int(os.environ.get("ALERT_COOLDOWN_SECONDS", "300"))
    )
    # Comma-separated "detector:service" pairs to suppress permanently.
    # Example: ALERT_SUPPRESS=pii:test-service,attribute:load-generator
    alert_suppress_patterns: list = field(
        default_factory=lambda: [
            p.strip() for p in os.environ.get("ALERT_SUPPRESS", "").split(",") if p.strip()
        ]
    )

    # ── Source code access (for performance specialist) ───────────────────────
    # Set SOURCE_ROOT to a mounted directory containing service source repos.
    # Set GITHUB_TOKEN + GITHUB_REPO for GitHub API access.
    # Neither set → profiling-only mode (file:line recommendations, no diffs).
    source_root: str = field(
        default_factory=lambda: os.environ.get("SOURCE_ROOT", "")
    )
    github_token: str = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN", "")
    )
    github_repo: str = field(
        default_factory=lambda: os.environ.get("GITHUB_REPO", "")
    )
    github_branch: str = field(
        default_factory=lambda: os.environ.get("GITHUB_BRANCH", "main")
    )

    # ── Synthesis / assessment timeouts ───────────────────────────────────────
    synthesis_timeout: int = field(
        default_factory=lambda: int(os.environ.get("SYNTHESIS_TIMEOUT", "900"))
    )
    # Max LLM turns per specialist (default 8 — enough for any domain, prevents runaway loops)
    specialist_max_turns: int = field(
        default_factory=lambda: int(os.environ.get("SPECIALIST_MAX_TURNS", "8"))
    )
    # Max LLM turns for synthesis (default 5 — synthesis should drill, not loop)
    synthesis_max_turns: int = field(
        default_factory=lambda: int(os.environ.get("SYNTHESIS_MAX_TURNS", "5"))
    )
