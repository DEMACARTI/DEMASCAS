# DEMASCAS Preference Learning System - Architecture & Integration

## The Problem (From Your Logs)

When you said:
> "Remember that whenever I say open a browser, you open brave browser"
> "When I say start my study routine, open LMS"

**Nothing was captured.** The system ignored your preferences and just routed to agents.

### Root Causes

1. **No Preference Detection Layer**: The old system had no way to recognize "when X, do Y" patterns
2. **Old Router Only Filtered Tools**: Didn't understand intent beyond keyword matching
3. **No Learning Between Turns**: Even if preferences were saved, they weren't applied to future queries
4. **Agents Can't Learn**: base_agent executes tools but doesn't capture user intents
5. **No Unified Flow**: System jumped directly from NLP → agent dispatch, skipping preference detection

### Example from Your Logs

```
[🗣️  YOU SAID]: "when I say that my open my study routine, then you start LMS, alright?"
[ROUTER] Query: 'file' → 7 tools  ❌ WRONG! Matched on word "file"
INFO:agents.base_agent:[🤖 AGENT:FILE]  ❌ Routed to wrong agent
[STEP] file #1 find_file(...)     ❌ Tried to find a file instead of learning
```

---

## The Solution: Unified Orchestrator v2.1

Three new systems working together:

### 1. **PreferenceDetector** (core/preferences_v2.py)

Recognizes preference statements in natural language:

```python
"Remember that whenever I say open a browser, use Brave browser"
  ↓
{
  "type": "conditional",
  "trigger": "open a browser",
  "action": "use Brave browser"
}
```

Supported patterns:
- "whenever/when I [say/ask] X, [then] Y"
- "remember that (when) I X, Y"
- "always use X [for/when] Y"
- "never [use/open] X"
- "prefer X over Y"

### 2. **PreferenceManager** (core/preferences_v2.py)

Stores learned rules persistently:

```
~/.demascas/preferences.json
{
  "conditionals": {
    "open a browser": "use Brave browser",
    "start my study routine": "open LMS on Brave"
  },
  "always": {
    "Safari": "Chrome"
  },
  "never": ["PowerPoint"],
  "preferences": {
    "default_browser": "Brave"
  }
}
```

### 3. **PreferenceApplier** (core/preferences_v2.py)

Enhances queries with learned preferences:

```python
Input:  "open a browser"
Learned rules: {"open a browser": "use Brave browser"}
Output: "open a browser [learned: use Brave browser]"
```

---

## Processing Flow (v2.1)

```
User Query
  ↓
┌─────────────────────────────────────────┐
│ STEP 1: Preference Detection            │
│ Is this "when X, do Y"?                │
│ ✓ If YES → Capture & confirm, done      │
│ ✗ If NO → Continue                      │
└─────────────────────────────────────────┘
  ↓
┌─────────────────────────────────────────┐
│ STEP 2: Apply Learned Preferences       │
│ Enhance query with saved rules           │
│ "open browser" → "open browser          │
│                   [learned: use Brave]" │
└─────────────────────────────────────────┘
  ↓
┌─────────────────────────────────────────┐
│ STEP 3: Smart Routing (v2)              │
│ 88 tools → 6-8 relevant tools           │
└─────────────────────────────────────────┘
  ↓
┌─────────────────────────────────────────┐
│ STEP 4: Agent Execution                 │
│ Run best agent (Browser, File, System)  │
│ With learned context applied            │
└─────────────────────────────────────────┘
  ↓
┌─────────────────────────────────────────┐
│ STEP 5: Save Episode                    │
│ Log for future learning                 │
└─────────────────────────────────────────┘
```

---

## How It Fixes Your Issues

### Before (v1)

```
"when I say open a browser, use Brave"
↓
Router matches "browser" keyword
↓
Routes to browser agent with all 88 tools
↓
Agent tries to open something (wrong interpretation)
↓
Preference lost forever
```

### After (v2.1)

```
"when I say open a browser, use Brave"
↓
PreferenceDetector matches pattern
↓
Saves to ~/.demascas/preferences.json:
  "conditionals": {"open a browser": "use Brave"}
↓
Confirms to user: "[✓] Learned: when you say 'open a browser', I'll use Brave browser"
✓ Done — preference captured

Next time user says "open a browser"
↓
PreferenceApplier finds matching rule
↓
Enhances query: "open a browser [learned: use Brave]"
↓
Router + Agent + all downstream systems see the learned intent
✓ Correctly uses Brave
```

---

## Integration into Tool Server

### File: server/tool_server.py

**Before** (current):
```python
from server.agents.dispatcher import dispatch_to_agent
from server.agents.base_agent import AgentBase

# Processing:
@app.route("/chat", methods=["POST"])
def chat():
    query = request.json.get("query")
    result = dispatch_to_agent(query)  # Old system
    return jsonify({"result": result})
```

**After** (v2.1):
```python
from server.unified_integration import setup_unified_endpoints, compatibility_wrapper

# Add v2.1 endpoints
setup_unified_endpoints(app)

# Keep old /tool endpoint working (backward compat)
compatibility_wrapper(app)

# Now you have:
# - POST /query          (new unified system)
# - GET  /preferences    (show learned rules)
# - POST /clear_preferences (reset learning)
# - POST /tool           (old system, still works)
```

### Single Change Required

In `server/tool_server.py`, after `app = Flask(...)`:

```python
# Add this line:
from server.unified_integration import setup_unified_endpoints
setup_unified_endpoints(app)
```

That's it. The daemon can continue using `/tool` (backward compat), or the UI can call `/query` for the new unified system.

---

## Testing the New System

### Test 1: Capture a Preference

```bash
curl -X POST http://localhost:5001/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Remember whenever I say open a browser, use Brave browser"}'

Response:
{
  "events": [
    {
      "type": "preference",
      "data": {
        "action": "captured",
        "message": "[✓] Learned: when you say 'open a browser', I'll use Brave browser"
      }
    }
  ]
}
```

### Test 2: Use Learned Preference

```bash
curl -X POST http://localhost:5001/query \
  -H "Content-Type: application/json" \
  -d '{"query": "open a browser"}'

Response:
{
  "events": [
    {
      "type": "preference",
      "data": {
        "action": "applied",
        "applied": {
          "conditional": [
            "open a browser",
            "use Brave browser"
          ]
        }
      }
    },
    {
      "type": "routing",
      "data": {
        "tools": [...],
        "query": "open a browser [learned: use Brave browser]"
      }
    },
    ...execution continues...
  ]
}
```

### Test 3: View All Learned Preferences

```bash
curl http://localhost:5001/preferences

Response:
{
  "preferences": "[📋 Learned Preferences]\n\n  When...\n    • 'open a browser' → use Brave browser\n    • 'start my study routine' → open LMS on Brave\n\n  Always...\n    • Use Safari instead of Chrome\n\n  Never...\n    • PowerPoint\n\n..."
}
```

---

## Example Preferences Now Work

After integration, these will all work:

### Conditional Rules
```
"Whenever I say open a browser, use Brave browser"
"When I say start studying, open my LMS"
"Remember that when I ask for weather, also check the news"
```

### Always Rules
```
"Always open files in Preview instead of Adobe"
"Always use Safari for research"
"Prefer Google Sheets to Excel"
```

### Never Rules
```
"Never open PowerPoint for presentations"
"Never use system default browser, always use Brave"
```

### Named Preferences
```
"Set Brave as my default browser"
"Use Kokoro for text-to-speech"
```

---

## File Changes Summary

### Created (New)
- `core/preferences_v2.py` (470 lines) — Preference detection, storage, application
- `core/unified_orchestrator.py` (220 lines) — Unified command processor
- `server/unified_integration.py` (190 lines) — Flask endpoint integration

### Modified
- `server/tool_server.py` — Add one line: `setup_unified_endpoints(app)`

### Unchanged
- `core/agent_orchestrator_v2.py` — V2 routing still works
- `server/agents/agents_v2.py` — V2 agents still work
- `daemon/src/` — C++ daemon continues working (backward compat)

---

## Architecture Diagram

```
                    ┌─────────────────────┐
                    │   User Query        │
                    │  via C++ daemon     │
                    │  or UI              │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ PreferenceDetector  │
                    │                     │
                    │ "when X, do Y?" ────┐
                    │                     │
                    └──────────┬──────────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
            ✓ Preference        ✗ Regular Query
                    │                     │
                    ▼                     ▼
        ┌──────────────────┐   ┌──────────────────┐
        │PreferenceManager │   │PreferenceApplier │
        │                  │   │                  │
        │Save to disk      │   │Enhance query with│
        │Confirm to user   │   │learned rules     │
        │Done              │   └────────┬─────────┘
        └──────────────────┘            │
                                        ▼
                              ┌──────────────────┐
                              │ ToolRouterV2     │
                              │                  │
                              │88 → 6-8 tools   │
                              └────────┬─────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │ Agent Selection  │
                              │                  │
                              │Pick best agent  │
                              └────────┬─────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │ Agent Execution  │
                              │                  │
                              │Browser/File/    │
                              │System/Research  │
                              └────────┬─────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │ Learning System  │
                              │                  │
                              │Save episode      │
                              │for future use    │
                              └──────────────────┘
```

---

## Backward Compatibility

The new system works alongside the old one:
- Daemon can keep calling `/tool` endpoint (unchanged)
- Old agents still work if needed
- Preferences are captured but optional
- Works with existing tool schemas and agents

To keep using old system:
```python
# Just don't call setup_unified_endpoints()
# Everything works as before
```

To fully migrate:
```python
# Call setup_unified_endpoints(app)
# Daemon automatically starts using unified system
```

---

## Troubleshooting

### Q: Preferences aren't being saved
**A**: Check `~/.demascas/preferences.json` exists and is writable
```bash
cat ~/.demascas/preferences.json
```

### Q: Learned rules not being applied
**A**: Ensure PreferenceApplier is running:
```bash
curl http://localhost:5001/preferences
```

Should show your saved preferences.

### Q: Agent routing still wrong
**A**: The learned preference is being applied, but agent doesn't understand it.
Add the learned intent to agent's training data (agents_v2.py)

### Q: How do I clear learned preferences?
**A**: Call the clear endpoint:
```bash
curl -X POST http://localhost:5001/clear_preferences
```

Or manually:
```bash
rm ~/.demascas/preferences.json
```

---

## Integration Checklist

- [ ] Add three new files:
  - `core/preferences_v2.py`
  - `core/unified_orchestrator.py`
  - `server/unified_integration.py`

- [ ] Modify `server/tool_server.py`:
  - Import: `from server.unified_integration import setup_unified_endpoints`
  - Add: `setup_unified_endpoints(app)` after Flask app creation

- [ ] Test endpoint:
  ```bash
  curl http://localhost:5001/preferences
  ```

- [ ] Test preference capture:
  ```bash
  curl -X POST http://localhost:5001/query \
    -H "Content-Type: application/json" \
    -d '{"query": "Remember when I say hello, say hi back"}'
  ```

- [ ] Restart daemon:
  ```bash
  ./scripts/run.sh
  ```

- [ ] Test learning:
  ```bash
  Say: "Remember when I say hi, say hello back"
  Say: "hi"  (should get "hello back" response)
  ```

---

## Next: UI Integration

The new endpoints return structured events that can be streamed to a UI:

```json
{
  "events": [
    {
      "type": "preference",
      "data": {"action": "captured", "message": "..."}
    },
    {
      "type": "routing",
      "data": {"tools": [...], "query": "..."}
    },
    {
      "type": "executing",
      "data": {"tool": "...", ...}
    },
    {
      "type": "result",
      "data": {"step": {...}, "body": "..."}
    },
    {
      "type": "complete",
      "data": {"agent": "...", "steps": 3}
    }
  ]
}
```

UI can update progressively as events arrive, providing real-time feedback about what DEMASCAS is doing.
