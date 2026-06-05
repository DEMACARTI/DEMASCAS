# DEMASCAS Architecture Graph

This graph shows how audio, daemon routing, tools, agents, and model servers are connected.

```mermaid
flowchart TD
    U[User Voice] --> A[daemon/src/audio_engine.cpp]
    A --> W[daemon/src/whisper_engine.cpp]
    W --> M[daemon/src/main.cpp]

    M -->|PATH 1 Shortcut| S[daemon/src/shortcut_map.hpp]
    M -->|PATH 2 Fast Q&A| L[daemon/src/llm_client.cpp]
    M -->|PATH 3 Tool ReAct| L
    M -->|PATH 3.5 Agent| TB[daemon/src/tool_bridge.cpp]
    M -->|PATH 4 Structured Task| TB

    L -->|HTTP /v1/chat/completions| TXT[MLX Text Server :8081]
    TB -->|HTTP /tool,/tools,/tools_for_query| TS[server/tool_server.py]
    TB -->|HTTP /agent| TS
    TB -->|HTTP /plan_and_execute| TS
    TB -->|HTTP /context,/learn| TS

    TS --> R[server/tool_router.py]
    TS --> ACT[actuators/*]
    TS --> CORE[core/*]
    TS --> AGI[server/agents/dispatcher.py]
    TS --> AGENTS[server/agents/*]
    TS --> MCP[mcp/manager.py]

    AGI --> AGENTS
    AGENTS -->|tool calls| TS

    ACT -->|browser automation| BR[actuators/browser.py]
    ACT -->|web goals| WA[actuators/web_agent.py]
    ACT -->|email/research| COM[actuators/communication.py]
    ACT -->|filesystem| FS[actuators/file_system.py]
    ACT -->|system controls| SYS[actuators/system_control.py]

    WA --> BR
    COM -->|HTTP /v1/chat/completions| TXT
    CORE -->|planner/executor prompts| TXT

    TS -->|optional VLM calls via sensors/vision.py| VLM[MLX Vision Server :8082]
```

## Key Runtime Dependencies

- Text LLM endpoint: http://127.0.0.1:8081/v1/chat/completions
- Tool server endpoint: http://127.0.0.1:5001
- Agent orchestration: server/agents/* via /agent
- Structured task engine: core/task_executor.py via /plan_and_execute
