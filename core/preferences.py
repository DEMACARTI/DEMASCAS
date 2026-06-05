"""
DEMASCAS — core/preferences.py
Persistent user preferences and rules (e.g. "always use Brave browser", "increase humor").
Stored in ~/.demascas/preferences.json.
"""

import json
import os

_DEMASCAS_DIR = os.path.expanduser("~/.demascas")
_PREFS_PATH = os.path.join(_DEMASCAS_DIR, "preferences.json")

# Default settings
_DEFAULT_PREFS = {
    "humor_level": 50,  # 0 = professional, 100 = full comedian
    "default_browser": "", # If empty, uses system default
    "custom_instructions": [] # List of strings "always do X", "never do Y"
}

def _load_prefs() -> dict:
    if not os.path.exists(_PREFS_PATH):
        return _DEFAULT_PREFS.copy()
    try:
        with open(_PREFS_PATH, "r") as f:
            data = json.load(f)
            
        # Merge with defaults to ensure all keys exist
        merged = _DEFAULT_PREFS.copy()
        merged.update(data)
        return merged
    except:
        return _DEFAULT_PREFS.copy()

def _save_prefs(prefs: dict) -> None:
    os.makedirs(_DEMASCAS_DIR, exist_ok=True)
    temp_path = _PREFS_PATH + ".tmp"
    with open(temp_path, "w") as f:
        json.dump(prefs, f, indent=4)
    os.replace(temp_path, _PREFS_PATH)

def set_preference(key: str, value: str) -> str:
    """
    Save a user preference or rule.
    Supported keys:
    - 'humor_level': 0-100 number as string
    - 'default_browser': name of browser (e.g. "Brave Browser", "Safari")
    - 'custom_instruction': a new rule to add to the rule list (pass the rule text as value)
    """
    prefs = _load_prefs()
    
    # Handle specifically typed properties
    if key == "humor_level":
        try:
            val = int(value)
            prefs["humor_level"] = max(0, min(100, val))
        except ValueError:
            return f"[-] Error: humor_level must be a number between 0 and 100."
    elif key == "custom_instruction":
        if "custom_instructions" not in prefs:
            prefs["custom_instructions"] = []
        if value not in prefs["custom_instructions"]:
            prefs["custom_instructions"].append(value)
    else:
        # Dynamic property creation
        prefs[key] = value
        
    _save_prefs(prefs)
    return f"[+] Saved preference: {key} = {value}"

def set_humor_level(level: int) -> str:
    """Set the humor level of DEMASCAS (0-100)."""
    return set_preference("humor_level", str(level))

def remove_preference(key: str) -> str:
    """
    Remove an existing user preference or rule.
    If key is 'custom_instruction', specify the exact text to remove in 'key'.
    """
    prefs = _load_prefs()
    
    # Check if they are trying to remove a specific custom instruction
    if "custom_instructions" in prefs and key in prefs["custom_instructions"]:
        prefs["custom_instructions"].remove(key)
        _save_prefs(prefs)
        return f"[+] Removed custom instruction: {key}"
        
    if key in prefs:
        # Don't completely delete default keys, just revert to default
        if key in _DEFAULT_PREFS:
            prefs[key] = _DEFAULT_PREFS[key]
        else:
            del prefs[key]
        _save_prefs(prefs)
        return f"[+] Removed preference: {key}"
        
    return f"[-] Preference '{key}' not found."

def get_all_preferences() -> dict:
    """Return the raw preferences dictionary."""
    return _load_prefs()

def get_preferences_prompt() -> str:
    """
    Returns a formatted string block containing user rules and preferences 
    to be injected into the LLM system prompt.
    """
    prefs = _load_prefs()
    
    lines = ["[USER PREFERENCES & RULES]"]
    
    # Inject default browser rule if explicitly set
    browser = prefs.get("default_browser", "").strip()
    if browser:
        lines.append(f"- IMPORTANT RULE: When interacting with the web or opening URLs, ALWAYS use '{browser}' as the default browser unless the user specifies otherwise.")
        
    # Inject humor and personality
    humor = prefs.get("humor_level", 50)
    if humor <= 20:
        lines.append("- PERSONALITY: Professional, extremely concise, formal, and direct. No jokes.")
    elif humor <= 50:
        lines.append("- PERSONALITY: Warm, friendly, efficient, occasionally witty.")
    elif humor <= 80:
        lines.append("- PERSONALITY: Humorous, playful, creative, uses mild jokes and interesting phrases.")
    else:
        lines.append("- PERSONALITY: Full comedian mode. Crack jokes, use sarcasm, pop culture references, and have high attitude.")
        
    # Inject custom instructions
    instructions = prefs.get("custom_instructions", [])
    if instructions:
        for instr in instructions:
            lines.append(f"- CUSTOM RULE: {instr}")
            
    # Include other arbitrary string keys
    for k, v in prefs.items():
        if k not in ["humor_level", "default_browser", "custom_instructions"] and isinstance(v, str):
            lines.append(f"- {k}: {v}")
            
    if len(lines) == 1:
        return ""  # Only header, no active rules
        
    return "\n".join(lines) + "\n"
