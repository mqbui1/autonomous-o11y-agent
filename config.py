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
    bedrock_model_id: str = field(
        default_factory=lambda: os.environ.get(
            "BEDROCK_MODEL_ID",
            "arn:aws:bedrock:us-west-2:387769110234:application-inference-profile/fky19kpnw2m7",
        )
    )
    subprocess_timeout: int = field(
        default_factory=lambda: int(os.environ.get("TOOL_TIMEOUT", "300"))
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
