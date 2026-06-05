"""
Memory Configuration for DEMASCAS
==================================

This module defines memory limits, cache sizes, and resource budgets
for all DEMASCAS components. Modify these values to match your system.

Default values are tuned for 8GB RAM (M1/M2 Mac). 
Adjust for your hardware:
- 4GB RAM: Reduce all by 50%
- 16GB+ RAM: Can increase episode/memory caches by 2x
"""

import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# CHROMADB MEMORY LIMITS
# ─────────────────────────────────────────────────────────────────────

# Max episodic memories to keep in active storage
# Older episodes are automatically compressed into JSON archives
MAX_ACTIVE_EPISODES = 300  # Default: 500, Optimized: 300

# When episode count exceeds this, trigger compression
COMPRESS_EPISODES_THRESHOLD = 350

# Max user memories (facts about user, preferences, etc)
MAX_USER_MEMORIES = 50

# ChromaDB batch size (fewer embeddings = lower memory spike)
CHROMA_BATCH_SIZE = 10  # Default: 50, Optimized: 10
CHROMA_QUERY_DISTANCE_THRESHOLD = 0.7  # Only return highly similar memories

# ─────────────────────────────────────────────────────────────────────
# LLM CONTEXT LIMITS
# ─────────────────────────────────────────────────────────────────────

# Max tokens in LLM context window per call
# Lower = less memory, but may miss context for complex tasks
MAX_CONTEXT_TOKENS = 3500  # Default: 4096, Optimized: 3500

# History buffer: keep this many recent steps in agent context
AGENT_HISTORY_WINDOW = 4  # Default: 6, Optimized: 4

# Token budget for injected memories/learned context
LEARNED_CONTEXT_BUDGET = 600  # Default: 1200, Optimized: 600

# ─────────────────────────────────────────────────────────────────────
# AGENT EXECUTION LIMITS
# ─────────────────────────────────────────────────────────────────────

# Max steps per agent task (prevents runaway execution)
MAX_AGENT_STEPS = 5  # Default: 20, Optimized: 5

# Max parallel agents (only run one at a time in v2)
MAX_PARALLEL_AGENTS = 1

# Tool schemas cache TTL (seconds) - refresh less frequently
TOOL_SCHEMA_CACHE_TTL = 600  # Default: 300, Optimized: 600

# ─────────────────────────────────────────────────────────────────────
# MEMORY ARCHIVAL POLICY
# ─────────────────────────────────────────────────────────────────────

# Days before episodic memory is archived (moved to .gz file)
ARCHIVE_AGE_DAYS = 30

# Days before archived files are permanently deleted
DELETE_ARCHIVE_AGE_DAYS = 365

# ─────────────────────────────────────────────────────────────────────
# TOOL FILTERING & ROUTING
# ─────────────────────────────────────────────────────────────────────

# Max tools to load into memory at once
# (v2 router filters 88 tools → 6-8 relevant, not all)
MAX_ACTIVE_TOOLS = 10  # Default: 88, Optimized: 10

# Tools loaded per agent domain
TOOLS_PER_DOMAIN = 3  # Default: 8, Optimized: 3

# ─────────────────────────────────────────────────────────────────────
# LEARNING SYSTEM CONFIG
# ─────────────────────────────────────────────────────────────────────

# User profile file size limit (KB) before cleanup
PROFILE_SIZE_LIMIT_KB = 200

# Batch size for learning context injections
LEARNING_BATCH_SIZE = 3  # Default: 5, Optimized: 3

# ─────────────────────────────────────────────────────────────────────
# FILE CACHE & CLEANUP
# ─────────────────────────────────────────────────────────────────────

# Temp files older than this (days) are auto-deleted
TEMP_FILE_AGE_DAYS = 7

# Log files older than this (days) are rotated
LOG_FILE_AGE_DAYS = 14

# Max log file size (MB) before rotation
LOG_FILE_SIZE_MB = 50

# ─────────────────────────────────────────────────────────────────────
# PERSISTENCE & DISK I/O
# ─────────────────────────────────────────────────────────────────────

# Flush profile to disk every N commands (not every command)
PROFILE_FLUSH_INTERVAL = 5

# Batch memory writes (write multiple at once)
MEMORY_WRITE_BATCH_SIZE = 10

# ─────────────────────────────────────────────────────────────────────
# SYSTEM CHECKS
# ─────────────────────────────────────────────────────────────────────

def validate_config():
    """Check that configured values are reasonable."""
    errors = []
    
    if MAX_CONTEXT_TOKENS > 4096:
        errors.append(f"MAX_CONTEXT_TOKENS ({MAX_CONTEXT_TOKENS}) exceeds model limit")
    
    if MAX_AGENT_STEPS > 20:
        errors.append(f"MAX_AGENT_STEPS ({MAX_AGENT_STEPS}) too high, reduces efficiency")
    
    if MAX_ACTIVE_EPISODES > 1000:
        errors.append(f"MAX_ACTIVE_EPISODES ({MAX_ACTIVE_EPISODES}) will stress memory")
    
    if COMPRESS_EPISODES_THRESHOLD <= MAX_ACTIVE_EPISODES:
        errors.append(f"COMPRESS_EPISODES_THRESHOLD must be > MAX_ACTIVE_EPISODES")
    
    if errors:
        import sys
        print("[!] Config validation failed:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    
    return True


def get_memory_info() -> dict:
    """Get current memory configuration as dict."""
    return {
        "episodes": MAX_ACTIVE_EPISODES,
        "context_tokens": MAX_CONTEXT_TOKENS,
        "agent_steps": MAX_AGENT_STEPS,
        "active_tools": MAX_ACTIVE_TOOLS,
        "history_window": AGENT_HISTORY_WINDOW,
    }


# Auto-validate on import
validate_config()
