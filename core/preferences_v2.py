"""
DEMASCAS Preference & Workflow Engine v2
=========================================

Handles user preferences, learned behaviors, and custom rules.

Examples:
- "Remember that whenever I say open a browser, use Brave browser"
- "When I say start my study routine, open LMS on Brave"
- "Always use Safari for research"
- "Never open PowerPoint, use Google Slides instead"

These are learned at runtime and applied to future queries automatically.
Persistent storage in ~/.demascas/preferences.json
"""

import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

IST = timezone(timedelta(hours=5, minutes=30))

DEMASCAS_DIR = Path.home() / ".demascas"
PREFS_FILE = DEMASCAS_DIR / "preferences.json"


# ─────────────────────────────────────────────────────────────────────
# PREFERENCE PATTERNS AND DETECTION
# ─────────────────────────────────────────────────────────────────────

class PreferenceDetector:
    """Detect preference/rule statements in user queries."""
    
    # Pattern examples:
    # "whenever I (say|ask|tell|request) X, (you should|please|then you) Y"
    # "when I (say|ask) X, Y"
    # "remember that (whenever|when) I X, Y"
    # "always use/open/do X"
    # "never use/open X"
    
    PREFERENCE_PATTERNS = [
        # "whenever/when I say/ask X, then/you should Y"
        r"(?:whenever|when)\s+I\s+(?:say|ask|tell|request)\s+(.+?),\s+(?:then\s+)?(?:you\s+)?(?:should\s+)?(.+?)(?:\?|$)",
        
        # "remember/save/recall that (when/whenever) I X, Y"
        r"(?:remember|save|recall)\s+that\s+(?:whenever|when)\s+I\s+(\w+.*?),\s+(.+?)(?:\?|$)",
        
        # "always/never do/use X when/for Y"
        r"(?:always|never)\s+(?:use|open|do|launch|start|go to)\s+(.+?)\s+(?:when|for|on)\s+(.+?)(?:\?|$)",
        
        # "prefer X over Y", "prefer X to Y"
        r"(?:prefer|use)\s+(.+?)\s+(?:over|instead of|to)\s+(.+?)(?:\?|$)",
        
        # "Set/Use X as default"
        r"(?:set|use)\s+(.+?)\s+as\s+(?:default|my\s+preferred|my\s+favorite)(?:\?|$)",
    ]
    
    @staticmethod
    def detect(query: str) -> Optional[Dict[str, str]]:
        """
        Try to detect a preference statement.
        
        Returns:
            {
                "type": "conditional" | "always" | "never" | "preference",
                "trigger": "what user says to trigger this",
                "action": "what DEMASCAS should do",
                "original": query
            }
            or None if not detected as preference
        """
        query_lower = query.lower()
        
        # Skip non-preference queries
        if not any(word in query_lower for word in 
                  ["remember", "whenever", "when i", "always", "never", 
                   "prefer", "use as default", "my preferred", "my favorite"]):
            return None
        
        for pattern in PreferenceDetector.PREFERENCE_PATTERNS:
            match = re.search(pattern, query_lower, re.IGNORECASE)
            if match:
                groups = match.groups()
                if len(groups) >= 2:
                    return {
                        "type": PreferenceDetector._infer_type(query_lower),
                        "trigger": groups[0].strip(),
                        "action": groups[1].strip(),
                        "original": query,
                    }
        
        return None
    
    @staticmethod
    def _infer_type(query_lower: str) -> str:
        """Infer preference type from query."""
        if "whenever" in query_lower or "when i" in query_lower:
            return "conditional"
        elif "always" in query_lower:
            return "always"
        elif "never" in query_lower:
            return "never"
        elif "prefer" in query_lower:
            return "preference"
        else:
            return "rule"


# ─────────────────────────────────────────────────────────────────────
# PREFERENCE STORAGE AND MANAGEMENT
# ─────────────────────────────────────────────────────────────────────

class PreferenceManager:
    """Load, save, and query user preferences."""
    
    _DEFAULTS = {
        "conditionals": {},  # "trigger" → "action"
        "always": {},        # "what to do" → "... instead"
        "never": [],         # things never to do
        "preferences": {},   # key → value (e.g. "default_browser" → "Brave")
        "created_at": None,
        "modified_at": None,
    }
    
    @staticmethod
    def _ensure_dir():
        """Create ~/.demascas if needed."""
        DEMASCAS_DIR.mkdir(parents=True, exist_ok=True)
    
    @staticmethod
    def load() -> Dict:
        """Load preferences from disk."""
        PreferenceManager._ensure_dir()
        if not PREFS_FILE.exists():
            return PreferenceManager._DEFAULTS.copy()
        
        try:
            with open(PREFS_FILE, 'r') as f:
                data = json.load(f)
                # Merge with defaults to handle new keys
                defaults = PreferenceManager._DEFAULTS.copy()
                defaults.update(data)
                return defaults
        except Exception as e:
            print(f"[!] Error loading preferences: {e}")
            return PreferenceManager._DEFAULTS.copy()
    
    @staticmethod
    def save(prefs: Dict) -> None:
        """Save preferences to disk."""
        PreferenceManager._ensure_dir()
        prefs["modified_at"] = datetime.now(IST).isoformat()
        
        temp_path = str(PREFS_FILE) + ".tmp"
        with open(temp_path, 'w') as f:
            json.dump(prefs, f, indent=2)
        
        PREFS_FILE.replace(Path(temp_path))
    
    @staticmethod
    def add_conditional(trigger: str, action: str) -> str:
        """
        Add a conditional preference: "when X, do Y".
        
        Returns: Confirmation message
        """
        prefs = PreferenceManager.load()
        prefs["conditionals"][trigger] = action
        PreferenceManager.save(prefs)
        return f"[✓] Learned: when you say '{trigger}', I'll {action}"
    
    @staticmethod
    def add_always(action: str, alternate: str) -> str:
        """
        Add an always rule: "always do X instead of Y".
        
        Returns: Confirmation message
        """
        prefs = PreferenceManager.load()
        prefs["always"][action] = alternate
        PreferenceManager.save(prefs)
        return f"[✓] Learned: I'll always use '{action}' instead of '{alternate}'"
    
    @staticmethod
    def add_never(action: str) -> str:
        """
        Add a never rule: "never do X".
        
        Returns: Confirmation message
        """
        prefs = PreferenceManager.load()
        if action not in prefs["never"]:
            prefs["never"].append(action)
        PreferenceManager.save(prefs)
        return f"[✓] Learned: I'll never {action}"
    
    @staticmethod
    def set_preference(key: str, value: str) -> str:
        """
        Set a named preference (e.g. "default_browser" → "Brave").
        """
        prefs = PreferenceManager.load()
        prefs["preferences"][key] = value
        PreferenceManager.save(prefs)
        return f"[✓] Set preference: {key} = {value}"
    
    @staticmethod
    def get_all() -> Dict:
        """Get all preferences."""
        return PreferenceManager.load()
    
    @staticmethod
    def get_conditionals() -> Dict[str, str]:
        """Get all conditional rules."""
        return PreferenceManager.load().get("conditionals", {})
    
    @staticmethod
    def get_always_rules() -> Dict[str, str]:
        """Get all 'always do X instead of Y' rules."""
        return PreferenceManager.load().get("always", {})
    
    @staticmethod
    def get_never_rules() -> List[str]:
        """Get list of things to never do."""
        return PreferenceManager.load().get("never", [])
    
    @staticmethod
    def get_preference(key: str, default: str = "") -> str:
        """Get a named preference."""
        return PreferenceManager.load().get("preferences", {}).get(key, default)


# ─────────────────────────────────────────────────────────────────────
# PREFERENCE APPLIER — Uses learned prefs in routing/execution
# ─────────────────────────────────────────────────────────────────────

class PreferenceApplier:
    """Apply learned preferences to user queries."""
    
    @staticmethod
    def apply_to_query(query: str) -> Tuple[str, Dict]:
        """
        Apply all known preferences to a query.
        
        Returns:
            (modified_query, applied_prefs)
        """
        prefs = PreferenceManager.get_all()
        modified = query
        applied = {}
        
        # Check conditionals: if query matches trigger, augment request
        for trigger, action in prefs.get("conditionals", {}).items():
            if PreferenceApplier._matches_trigger(query, trigger):
                modified += f" [learned: {action}]"
                applied["conditional"] = (trigger, action)
                break  # Only apply first matching conditional
        
        # Check always rules: replace mentions of forbidden with preferred
        for forbidden, preferred in prefs.get("always", {}).items():
            if forbidden.lower() in modified.lower():
                modified = re.sub(
                    rf"\b{re.escape(forbidden)}\b",
                    preferred,
                    modified,
                    flags=re.IGNORECASE
                )
                applied["always"] = (forbidden, preferred)
        
        # Check never rules: add context to avoid them
        never_rules = prefs.get("never", [])
        if never_rules:
            never_str = ", ".join(never_rules)
            modified += f" [never: {never_str}]"
            applied["never"] = never_rules
        
        # Check default preferences (e.g., default_browser)
        defaults = prefs.get("preferences", {})
        if defaults:
            applied["preferences"] = defaults
        
        return modified, applied
    
    @staticmethod
    def _matches_trigger(query: str, trigger: str) -> bool:
        """Check if query matches a conditional trigger."""
        # Exact substring match (case-insensitive)
        return trigger.lower() in query.lower()


# ─────────────────────────────────────────────────────────────────────
# PREFERENCE HANDLER — Ties it all together
# ─────────────────────────────────────────────────────────────────────

def process_preference_statement(query: str) -> Optional[str]:
    """
    If query is a preference statement, process it and return confirmation.
    Otherwise return None.
    """
    pref = PreferenceDetector.detect(query)
    if not pref:
        return None
    
    pref_type = pref["type"]
    trigger = pref["trigger"]
    action = pref["action"]
    
    if pref_type == "conditional":
        return PreferenceManager.add_conditional(trigger, action)
    
    elif pref_type == "always":
        # trigger is "what to do", action is "instead of X"
        return PreferenceManager.add_always(trigger, action)
    
    elif pref_type == "never":
        # action is what to never do
        return PreferenceManager.add_never(action)
    
    elif pref_type == "preference":
        # trigger is key, action is value
        return PreferenceManager.set_preference(trigger, action)
    
    return None


def apply_learned_preferences(query: str) -> Tuple[str, Dict]:
    """
    Enhance query with learned preferences.
    
    Returns: (enhanced_query, applied_rules)
    """
    return PreferenceApplier.apply_to_query(query)


def get_preferences_summary() -> str:
    """Get a human-readable summary of all learned preferences."""
    prefs = PreferenceManager.get_all()
    
    lines = ["[📋 Learned Preferences]"]
    
    if prefs.get("conditionals"):
        lines.append("\n  When...")
        for trigger, action in prefs["conditionals"].items():
            lines.append(f"    • '{trigger}' → {action}")
    
    if prefs.get("always"):
        lines.append("\n  Always...")
        for preferred, avoided in prefs["always"].items():
            lines.append(f"    • Use {preferred} instead of {avoided}")
    
    if prefs.get("never"):
        lines.append("\n  Never...")
        for thing in prefs["never"]:
            lines.append(f"    • {thing}")
    
    if prefs.get("preferences"):
        lines.append("\n  Settings...")
        for key, value in prefs["preferences"].items():
            lines.append(f"    • {key}: {value}")
    
    if len(lines) == 1:
        lines.append("    (No preferences learned yet)")
    
    return "\n".join(lines)
