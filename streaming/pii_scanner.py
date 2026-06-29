"""
PII/PCI scanner for the streaming pipeline.

Scans span attributes for sensitive data patterns before telemetry
reaches Splunk. Fires a CRITICAL alert on first match per service/span.

Patterns are intentionally conservative — false positives are preferable
to missed detections in a compliance context.
"""

import logging
import re
from dataclasses import dataclass

from .alerts import AlertDispatcher, StreamingAlert

logger = logging.getLogger(__name__)

# (name, compiled_regex, severity)
_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # PCI DSS — card numbers
    ("visa_card",
     re.compile(r'\b4[0-9]{12}(?:[0-9]{3})?\b'),
     "critical"),
    ("mastercard",
     re.compile(r'\b(?:5[1-5][0-9]{2}|222[1-9]|22[3-9][0-9]|2[3-6][0-9]{2}|27[01][0-9]|2720)[0-9]{12}\b'),
     "critical"),
    ("amex",
     re.compile(r'\b3[47][0-9]{13}\b'),
     "critical"),
    ("generic_card_16",
     re.compile(r'\b(?:\d[ -]?){13,16}\b'),
     "high"),
    # PII
    ("ssn",
     re.compile(r'\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b'),
     "critical"),
    ("email",
     re.compile(r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b'),
     "high"),
    ("phone_us",
     re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),
     "high"),
    ("ipv4_private",
     re.compile(r'\b(?:10\.|172\.(?:1[6-9]|2[0-9]|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\b'),
     "low"),
]

# Attribute keys that should never contain raw values
_SENSITIVE_KEYS = frozenset({
    "card.number", "card_number", "credit_card", "cvv", "cvc",
    "pan", "account_number", "routing_number",
    "password", "passwd", "secret", "token", "api_key", "private_key",
    "ssn", "social_security", "date_of_birth", "dob",
})


@dataclass
class PIIFinding:
    service: str
    span_name: str
    attribute_key: str
    pattern_name: str
    severity: str


class PIIScanner:
    def __init__(self, dispatcher: AlertDispatcher):
        self.dispatcher = dispatcher

    def scan_span(self, service: str, span_name: str, attributes: dict) -> list[PIIFinding]:
        findings: list[PIIFinding] = []

        for key, value in attributes.items():
            if not isinstance(value, str):
                value = str(value)

            # Check sensitive key names regardless of value
            key_lower = key.lower().replace("-", "_").replace(".", "_")
            if key_lower in _SENSITIVE_KEYS:
                finding = PIIFinding(
                    service=service,
                    span_name=span_name,
                    attribute_key=key,
                    pattern_name="sensitive_key_name",
                    severity="critical",
                )
                findings.append(finding)
                self._fire(finding)
                continue

            # Pattern match on value
            for pattern_name, pattern, severity in _PATTERNS:
                if pattern.search(value):
                    finding = PIIFinding(
                        service=service,
                        span_name=span_name,
                        attribute_key=key,
                        pattern_name=pattern_name,
                        severity=severity,
                    )
                    findings.append(finding)
                    self._fire(finding)
                    break   # one match per attribute is enough

        return findings

    def _fire(self, finding: PIIFinding):
        self.dispatcher.fire(StreamingAlert(
            severity=finding.severity,
            detector="pii",
            service=finding.service,
            title=f"Sensitive data in span attribute `{finding.attribute_key}`",
            detail=(
                f"span={finding.span_name}  "
                f"pattern={finding.pattern_name}  "
                f"key={finding.attribute_key}  "
                "— data is IN FLIGHT, not yet in Splunk. "
                "Deploy redactionprocessor immediately."
            ),
            environment=self.dispatcher.environment,
        ))
