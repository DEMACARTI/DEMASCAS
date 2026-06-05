"""
Optimized Agents for DEMASCAS v2.0
==================================

Each agent is lightweight, autonomous, and handles its domain efficiently.
- BrowserAgent: Email, web browsing, navigation
- FileAgent: Find, read, create files
- SystemAgent: App control, shortcuts, system actions
- ResearchAgent: Knowledge lookup, web search, Q&A

All agents:
- Max 5 execution steps (vs 20 in v1)
- Streaming results
- Early exit on success
- Minimal context overhead
"""

from core.agent_orchestrator_v2 import SmartAgentV2, ToolRouterV2
from typing import Iterator, Optional
import logging

logger = logging.getLogger(__name__)


class BrowserAgentV2(SmartAgentV2):
    """
    Handles email and web tasks.
    
    Sequence:
    1. Get current screen state
    2. Navigate/open URL if needed  
    3. Click/type to reach goal
    4. Verify success
    """

    def __init__(self, router: ToolRouterV2):
        super().__init__("browser", router)
        self.available_tools = [
            "open_url",
            "open_url_and_wait", 
            "browser_click_element",
            "browser_type_text",
            "browser_scroll",
            "open_application",
        ]

    def plan(self, goal: str) -> list[str]:
        """
        Plan browser task with 3-4 steps.
        
        Examples:
        - "Compose email" → [open_application(Gmail), click_compose, type_body]
        - "Search web" → [open_url(google), type_search, click_search]
        """
        goal_lower = goal.lower()
        
        # Detect task type
        if any(w in goal_lower for w in ["email", "mail", "compose", "reply"]):
            return [
                "open_application",  # Open Gmail/Mail app
                "browser_click_element",  # Click compose
                "browser_type_text",  # Type message
                "browser_click_element",  # Click send
            ]
        
        elif any(w in goal_lower for w in ["navigate", "go to", "website", "browse"]):
            return [
                "open_url_and_wait",  # Open URL
                "browser_scroll",  # Optional scroll
                "browser_click_element",  # Click if needed
            ]
        
        elif any(w in goal_lower for w in ["search", "look up", "find online"]):
            return [
                "open_url",  # Open search engine
                "browser_type_text",  # Type query
                "browser_click_element",  # Click search
            ]
        
        else:
            # Generic browser task
            return [
                "open_application",
                "browser_click_element",
                "browser_type_text",
            ]


class FileAgentV2(SmartAgentV2):
    """
    Handles file operations.
    
    Sequence:
    1. Find file (fast keyword search)
    2. Open/read if needed
    3. Return result or perform operation
    """

    def __init__(self, router: ToolRouterV2):
        super().__init__("file", router)
        self.available_tools = [
            "find_file",
            "open_file",
            "read_file_content",
            "list_files",
            "create_file",
            "delete_file",
        ]

    def plan(self, goal: str) -> list[str]:
        """
        Plan file task with 2-3 steps.
        
        Examples:
        - "Find readme.md" → [find_file, open_file]
        - "Create notes" → [create_file]
        - "List downloads" → [list_files]
        """
        goal_lower = goal.lower()
        
        if any(w in goal_lower for w in ["find", "search", "locate", "where"]):
            return [
                "find_file",
                "open_file",
            ]
        
        elif any(w in goal_lower for w in ["read", "read the", "show me", "content", "display"]):
            return [
                "find_file",
                "read_file_content",
            ]
        
        elif any(w in goal_lower for w in ["create", "make", "new file", "write"]):
            return [
                "create_file",
            ]
        
        elif any(w in goal_lower for w in ["delete", "remove", "trash"]):
            return [
                "find_file",
                "delete_file",
            ]
        
        elif any(w in goal_lower for w in ["list", "show files", "what files"]):
            return [
                "list_files",
            ]
        
        else:
            # Default: find + read
            return ["find_file", "read_file_content"]


class SystemAgentV2(SmartAgentV2):
    """
    Handles system operations.
    
    Sequence:
    1. Execute command/action directly
    2. Verify result
    """

    def __init__(self, router: ToolRouterV2):
        super().__init__("system", router)
        self.available_tools = [
            "open_application",
            "close_application",
            "keyboard_shortcut",
            "type_text",
            "volume_control",
            "brightness_control",
            "screenshot",
            "list_running_apps",
        ]

    def plan(self, goal: str) -> list[str]:
        """
        Plan system task with 1-2 steps.
        
        Examples:
        - "Open Finder" → [open_application]
        - "Take screenshot" → [screenshot]
        - "Toggle mute" → [volume_control]
        """
        goal_lower = goal.lower()
        
        if any(w in goal_lower for w in ["open", "launch", "start"]):
            return ["open_application"]
        
        elif any(w in goal_lower for w in ["close", "quit", "exit"]):
            return ["close_application"]
        
        elif any(w in goal_lower for w in ["screenshot", "capture screen"]):
            return ["screenshot"]
        
        elif any(w in goal_lower for w in ["volume", "mute", "sound"]):
            return ["volume_control"]
        
        elif any(w in goal_lower for w in ["brightness"]):
            return ["brightness_control"]
        
        elif any(w in goal_lower for w in ["press", "shortcut", "keyboard"]):
            return ["keyboard_shortcut"]
        
        elif any(w in goal_lower for w in ["running apps", "list apps"]):
            return ["list_running_apps"]
        
        else:
            return ["open_application"]


class ResearchAgentV2(SmartAgentV2):
    """
    Handles knowledge and research tasks.
    
    Sequence:
    1. Search knowledge base / web
    2. Synthesize answer
    """

    def __init__(self, router: ToolRouterV2):
        super().__init__("research", router)
        self.available_tools = [
            "search_knowledge",
            "web_search",
            "get_weather",
            "get_news",
            "summarize_text",
        ]

    def plan(self, goal: str) -> list[str]:
        """
        Plan research task with 1-2 steps.
        
        Examples:
        - "What is X?" → [search_knowledge, web_search]
        - "Weather today" → [get_weather]
        - "Latest news" → [get_news]
        """
        goal_lower = goal.lower()
        
        if any(w in goal_lower for w in ["weather"]):
            return ["get_weather"]
        
        elif any(w in goal_lower for w in ["news", "latest"]):
            return ["get_news"]
        
        elif any(w in goal_lower for w in ["summarize", "sum up", "summary"]):
            return ["summarize_text", "search_knowledge"]
        
        else:
            # Default research: knowledge + web
            return ["search_knowledge", "web_search"]


class VisionAgentV2(SmartAgentV2):
    """
    Handles vision and screenshot tasks.
    
    Sequence:
    1. Capture screen
    2. Analyze if requested
    """

    def __init__(self, router: ToolRouterV2):
        super().__init__("vision", router)
        self.available_tools = [
            "screenshot",
            "camera_capture",
            "analyze_image",
        ]

    def plan(self, goal: str) -> list[str]:
        """
        Plan vision task.
        
        Examples:
        - "Take a photo" → [camera_capture]
        - "Screenshot" → [screenshot]
        - "What's on screen?" → [screenshot, analyze_image]
        """
        goal_lower = goal.lower()
        
        if any(w in goal_lower for w in ["photo", "camera", "picture"]):
            return ["camera_capture"]
        
        elif any(w in goal_lower for w in ["screenshot", "capture screen"]):
            return ["screenshot"]
        
        elif any(w in goal_lower for w in ["analyze", "what's on", "describe"]):
            return ["screenshot", "analyze_image"]
        
        else:
            return ["screenshot"]


# ─────────────────────────────────────────────────────────────────────
# Factory function to create all agents
# ─────────────────────────────────────────────────────────────────────

def create_agents_v2(router: ToolRouterV2) -> dict[str, SmartAgentV2]:
    """Create optimized agent instances."""
    return {
        "browser": BrowserAgentV2(router),
        "file": FileAgentV2(router),
        "system": SystemAgentV2(router),
        "research": ResearchAgentV2(router),
        "vision": VisionAgentV2(router),
    }
