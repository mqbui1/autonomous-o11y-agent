"""
Autonomous O11y Agent — configures the tool runner and returns the coordinator.
"""

import logging
import tools._runner as _runner
from config import AgentConfig

logger = logging.getLogger(__name__)


def build_agent(config: AgentConfig):
    """Configure tools with the given config and return the coordinator's run_assessment."""
    _runner._config = config
    from agents.coordinator import run_assessment
    return run_assessment
