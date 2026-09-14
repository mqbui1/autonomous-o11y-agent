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
    # Optional comma-separated list of additional Ollama instances (each with
    # its own OLLAMA_NUM_PARALLEL=1 slot) to split specialists across, e.g.
    # "http://ollama:11434/v1,http://ollama:11435/v1". A single-slot local
    # Ollama server otherwise serializes ALL 10 specialists regardless of
    # specialist_max_concurrency — this is the real fix for that ceiling.
    # Falls back to just [ollama_base_url] when unset (no behavior change).
    ollama_base_urls: list = field(
        default_factory=lambda: [
            u.strip() for u in os.environ.get("OLLAMA_BASE_URLS", "").split(",") if u.strip()
        ] or [os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")]
    )
    # Optional per-specialist model right-sizing on CPU-bound local Ollama
    # hosts, e.g. "health=qwen2.5:3b,logs=qwen2.5:3b,db=qwen2.5:7b". Specialists
    # not listed fall back to ollama_model (the "brain" model — keep the most
    # capable model there; a 3B model has previously hallucinated/misattributed
    # findings when asked to reason across domains, see coordinator.py's
    # _synthesize docstring, 2026-07-22). Ignored for bedrock/openai providers.
    specialist_model_overrides: dict = field(
        default_factory=lambda: dict(
            pair.split("=", 1)
            for pair in os.environ.get("OLLAMA_SPECIALIST_MODELS", "").split(",")
            if "=" in pair
        )
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
    # Safety-net cap on generated tokens per LLM call for local/self-hosted
    # models (ollama/openai providers only — Bedrock is fast enough and its
    # own API doesn't need this guardrail). CPU decode on a 14B model runs
    # at ~2-3 tok/s; without a cap, a rambling or non-terminating generation
    # (documented local-model failure mode, see training.md) can add many
    # extra minutes to a single specialist turn. Generous enough to not
    # truncate legitimate multi-issue submit_findings payloads.
    specialist_max_output_tokens: int = field(
        default_factory=lambda: int(os.environ.get("OLLAMA_MAX_TOKENS", "3000"))
    )

    subprocess_timeout: int = field(
        default_factory=lambda: int(os.environ.get("TOOL_TIMEOUT", "60"))
    )
    # provision_detectors/retune_detectors do real per-service baseline learning
    # (multiple SignalFlow queries per service) — genuinely slower than other
    # subprocess tools. Confirmed 2026-07-23: with the default TOOL_TIMEOUT (180s)
    # this routinely timed out on environments with more than a handful of
    # services, driving the detector specialist into a retry loop. Give it its
    # own, longer budget instead of raising the timeout for every tool.
    provision_timeout: int = field(
        default_factory=lambda: int(os.environ.get("PROVISION_TIMEOUT", "480"))
    )
    specialist_timeout: int = field(
        default_factory=lambda: int(os.environ.get("SPECIALIST_TIMEOUT", "900"))
    )
    # How many specialists may call the LLM concurrently. Bedrock has real
    # per-request cloud capacity, so all 10 specialists firing at once is fine.
    # A local Ollama instance is a single-process server serializing requests
    # (OLLAMA_NUM_PARALLEL=1 on memory-constrained hardware) — 10 concurrent
    # specialists just pile up in its request queue, and one of them can sit
    # queued long enough to blow past the OpenAI client's HTTP read timeout
    # before ever reaching config.specialist_timeout. Confirmed via live-test
    # regression 2026-09-06 (RCA specialist: raw httpx.ReadTimeout, not the
    # coordinator's TimeoutError). Default lower for non-bedrock providers.
    specialist_max_concurrency: int = field(
        default_factory=lambda: int(os.environ.get(
            "SPECIALIST_MAX_CONCURRENCY",
            "10" if os.environ.get("LLM_PROVIDER", "bedrock").lower() == "bedrock" else "3",
        ))
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
    # Max LLM turns per specialist (default 12 — RCA's 8-step investigation plus any
    # redundant tool calls from the local fine-tuned model needs headroom beyond 8;
    # confirmed via live-test regression 2026-07-22 that 8 was too tight)
    specialist_max_turns: int = field(
        default_factory=lambda: int(os.environ.get("SPECIALIST_MAX_TURNS", "12"))
    )
    # Max LLM turns for synthesis (default 5 — synthesis should drill, not loop)
    synthesis_max_turns: int = field(
        default_factory=lambda: int(os.environ.get("SYNTHESIS_MAX_TURNS", "5"))
    )

