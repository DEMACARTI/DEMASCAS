"""
DEMASCAS Unified Command Orchestrator v2.1
===========================================

Combines:
1. Preference detection (learns "when X, do Y" rules)
2. Smart tool routing (88 tools → 6-8 relevant)
3. Agent execution (5 specialized agents)
4. Learning system (episodic memory + user profile)

Intent Detection Flow:
  User query
    ↓
  Is it a preference statement?
    → Yes: Capture preference, confirm to user
    → No: Continue
  ↓
  Apply learned preferences to query
  ↓
  Route to best agent (Browser, File, System, Research, Vision)
  ↓
  Execute with minimal context, streaming results
  ↓
  Save episode to learning system
"""

import json
import logging
from typing import Iterator, Dict, Optional, Tuple
from dataclasses import dataclass

from core.preferences_v2 import (
    PreferenceDetector, PreferenceManager, PreferenceApplier,
    process_preference_statement, apply_learned_preferences
)
from core.agent_orchestrator_v2 import ToolRouterV2, AgentOrchestrator
from core.learning import save_episode, load_learned_context

logger = logging.getLogger(__name__)


@dataclass
class CommandEvent:
    """Single event in command processing."""
    type: str  # "preference", "routing", "executing", "result", "complete", "error"
    data: dict
    
    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {"type": self.type, "data": self.data}


class UnifiedOrchestrator:
    """
    Top-level command processor combining all systems.
    
    Handles:
    - Preference capture (learns "when you say X, do Y")
    - Query enhancement with learned preferences
    - Routing to appropriate agent
    - Streaming result execution
    - Episode logging for learning
    """
    
    def __init__(self):
        self.preference_manager = PreferenceManager()
        self.agent_orchestrator = AgentOrchestrator()
        self.query_history = []
        self.current_goal = None
    
    def process(self, query: str, context: Dict = None) -> Iterator[CommandEvent]:
        """
        Process a user query end-to-end.
        
        Yields events as they occur (preference capture, routing, execution, result).
        """
        context = context or {}
        self.current_goal = query
        self.query_history.append(query)
        
        # ─────────────────────────────────────────────────────────
        # STEP 1: PREFERENCE DETECTION
        # ─────────────────────────────────────────────────────────
        
        pref_result = process_preference_statement(query)
        if pref_result:
            # This was a preference statement — capture and confirm
            yield CommandEvent(
                type="preference",
                data={
                    "action": "captured",
                    "message": pref_result,
                    "query": query,
                }
            )
            return  # We're done for this turn
        
        # ─────────────────────────────────────────────────────────
        # STEP 2: ENHANCE QUERY WITH LEARNED PREFERENCES
        # ─────────────────────────────────────────────────────────
        
        enhanced_query, applied_prefs = apply_learned_preferences(query)
        
        if applied_prefs:
            yield CommandEvent(
                type="preference",
                data={
                    "action": "applied",
                    "applied": applied_prefs,
                    "original": query,
                    "enhanced": enhanced_query,
                }
            )
        
        # ─────────────────────────────────────────────────────────
        # STEP 3: ROUTE TO AGENT
        # ─────────────────────────────────────────────────────────
        
        tools = self.agent_orchestrator.router.route(enhanced_query, limit=8)
        
        yield CommandEvent(
            type="routing",
            data={
                "tools": tools,
                "query": enhanced_query,
                "count": len(tools),
            }
        )
        
        # ─────────────────────────────────────────────────────────
        # STEP 4: EXECUTE WITH AGENT
        # ─────────────────────────────────────────────────────────
        
        agent_domain = self._pick_agent(tools)
        agent = self.agent_orchestrator.agents.get(agent_domain)
        
        if not agent:
            yield CommandEvent(
                type="error",
                data={"error": f"No agent for domain {agent_domain}"},
            )
            return
        
        steps_executed = 0
        for event in agent.execute(enhanced_query, context):
            yield CommandEvent(
                type="executing" if event.get("status") == "step_1" else "result",
                data=event,
            )
            if event.get("status") == "complete":
                steps_executed = event.get("steps", 0)
        
        # ─────────────────────────────────────────────────────────
        # STEP 5: SAVE EPISODE FOR LEARNING
        # ─────────────────────────────────────────────────────────
        
        try:
            save_episode(
                query=query,
                enhanced_query=enhanced_query,
                agent_domain=agent_domain,
                tools_used=tools,
                success=True,
                steps=steps_executed,
            )
        except Exception as e:
            logger.warning(f"Failed to save episode: {e}")
        
        yield CommandEvent(
            type="complete",
            data={
                "original_query": query,
                "agent": agent_domain,
                "steps": steps_executed,
            }
        )
    
    def _pick_agent(self, tools: list) -> str:
        """Pick best agent based on routed tools (simple heuristic)."""
        domain_scores = {}
        for tool in tools:
            for domain in ["browser", "file", "system", "research", "vision"]:
                if domain in tool.lower():
                    domain_scores[domain] = domain_scores.get(domain, 0) + 1
        
        if not domain_scores:
            return "system"
        
        return max(domain_scores, key=domain_scores.get)
    
    def get_preferences_summary(self) -> str:
        """Get all learned preferences."""
        from core.preferences_v2 import get_preferences_summary
        return get_preferences_summary()
    
    def clear_preferences(self) -> str:
        """Clear all learned preferences."""
        import os
        from pathlib import Path
        prefs_file = Path.home() / ".demascas" / "preferences.json"
        if prefs_file.exists():
            prefs_file.unlink()
        return "[✓] All preferences cleared"


# ─────────────────────────────────────────────────────────────────────
# Singleton instance
# ─────────────────────────────────────────────────────────────────────

_orchestrator: Optional[UnifiedOrchestrator] = None


def get_orchestrator() -> UnifiedOrchestrator:
    """Get or create the unified orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = UnifiedOrchestrator()
    return _orchestrator


def process_command(query: str, context: Dict = None) -> Iterator[CommandEvent]:
    """
    Top-level entry point for processing a user command.
    
    Yields CommandEvent objects as they occur.
    """
    orchestrator = get_orchestrator()
    yield from orchestrator.process(query, context)
