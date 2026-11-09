"""
agents/__init__.py
"""

import logging
import threading
import time

from .base_agent import AgentBase, AgentResult, StepRecord
from .dispatcher import AgentDispatcher, route_command, should_use_agent, get_dispatcher
from .browser_agent import BrowserAgent
from .file_agent import FileAgent
from .system_agent import SystemAgent
from .research_agent import ResearchAgent
from .comms_agent import CommsAgent
from .app_learner_agent import AppLearnerAgent
from .workflow_agent import WorkflowAgent
from .app_builder_agent import AppBuilderAgent

_REGISTRY = {
    "browser": BrowserAgent,
    "file": FileAgent,
    "system": SystemAgent,
    "research": ResearchAgent,
    "comms": CommsAgent,
    "app_learner": AppLearnerAgent,
    "workflow": WorkflowAgent,
    "app_builder": AppBuilderAgent,
}

_AGENT_INSTANCES: dict[str, AgentBase] = {}
logger = logging.getLogger(__name__)


def _get_agent(domain: str) -> AgentBase:
    if domain not in _REGISTRY:
        domain = "system"
    if domain not in _AGENT_INSTANCES:
        _AGENT_INSTANCES[domain] = _REGISTRY[domain]()
    return _AGENT_INSTANCES[domain]


# Agents drive the single shared desktop (mouse, keyboard, browser) and keep
# per-run state on long-lived instances, so only one may run at a time.
_RUN_LOCK = threading.Lock()


def run_agent(command: str, context: dict) -> AgentResult:
    if not _RUN_LOCK.acquire(timeout=2):
        return AgentResult(
            success=False,
            summary="I'm still working on the previous task. Give me a moment.",
            steps_taken=0,
            tool_calls=[],
            error="agent_busy",
        )
    try:
        domain = route_command(command)
        agent = _get_agent(domain)
        agent._start_time = time.time()  # reset the wall-clock budget
        try:
            return agent.run(command, context or {})
        except Exception as e:
            logger.exception("[run_agent] domain=%s command=%r error=%s", domain, command, e)
            return AgentResult(
                success=False,
                summary="Something went wrong while running that agent task.",
                steps_taken=0,
                tool_calls=[],
                error=str(e),
            )
    finally:
        _RUN_LOCK.release()


__all__ = [
    "AgentBase",
    "AgentResult",
    "StepRecord",
    "AgentDispatcher",
    "route_command",
    "should_use_agent",
    "get_dispatcher",
    "BrowserAgent",
    "FileAgent",
    "SystemAgent",
    "ResearchAgent",
    "CommsAgent",
    "AppLearnerAgent",
    "WorkflowAgent",
    "AppBuilderAgent",
    "run_agent",
]
