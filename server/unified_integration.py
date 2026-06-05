"""
DEMASCAS Tool Server Integration - v2.1
=======================================

Bridges the C++ daemon to the unified orchestrator.

Endpoints:
  POST /query → Process query with unified orchestrator
  GET  /preferences → Get all learned preferences  
  POST /clear_preferences → Clear all learned preferences
  POST /tool → Execute single tool (backward compat)
  GET  /tools → List all tools
"""

import json
from flask import Flask, request, jsonify
from typing import Iterator, Dict

from core.unified_orchestrator import process_command, get_orchestrator
from core.memory_config import get_memory_info


def setup_unified_endpoints(app: Flask) -> None:
    """
    Add v2.1 unified orchestrator endpoints to Flask app.
    Call this in tool_server.py after creating the Flask app.
    
    Usage:
        from server.unified_integration import setup_unified_endpoints
        setup_unified_endpoints(app)
    """
    
    @app.route("/query", methods=["POST"])
    def query():
        """
        Process a user query with full unified orchestrator.
        
        Request: {"query": str, "context": dict (optional)}
        Response: Stream of events [{type, data}, ...]
        """
        data = request.get_json() or {}
        query = data.get("query", "")
        context = data.get("context", {})
        
        if not query:
            return jsonify({"error": "Missing 'query' parameter"}), 400
        
        # Process query and stream results
        events = []
        try:
            for event in process_command(query, context):
                events.append(event.to_dict())
        except Exception as e:
            events.append({
                "type": "error",
                "data": {"error": str(e)},
            })
        
        return jsonify({"events": events})
    
    @app.route("/preferences", methods=["GET"])
    def get_preferences():
        """Get all learned preferences."""
        orchestrator = get_orchestrator()
        summary = orchestrator.get_preferences_summary()
        return jsonify({"preferences": summary})
    
    @app.route("/clear_preferences", methods=["POST"])
    def clear_preferences():
        """Clear all learned preferences."""
        orchestrator = get_orchestrator()
        result = orchestrator.clear_preferences()
        return jsonify({"result": result})
    
    @app.route("/memory_config", methods=["GET"])
    def memory_config():
        """Get current memory configuration."""
        return jsonify({"config": get_memory_info()})


def stream_query(app: Flask) -> None:
    """
    Alternative endpoint that streams results as Server-Sent Events.
    Use this for real-time UI updates.
    
    Call this instead of setup_unified_endpoints if you want SSE streaming.
    """
    from flask import Response
    
    @app.route("/query_stream", methods=["POST"])
    def query_stream():
        """
        Process query with streaming results (Server-Sent Events).
        
        Response: text/event-stream with events
          data: {"type": "...", "data": {...}}
        """
        data = request.get_json() or {}
        query = data.get("query", "")
        context = data.get("context", {})
        
        if not query:
            return jsonify({"error": "Missing 'query'"}), 400
        
        def generate():
            """Generator that yields SSE lines."""
            try:
                for event in process_command(query, context):
                    event_json = json.dumps(event.to_dict())
                    yield f"data: {event_json}\n\n"
            except Exception as e:
                error_event = json.dumps({
                    "type": "error",
                    "data": {"error": str(e)}
                })
                yield f"data: {error_event}\n\n"
        
        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
    
    setup_unified_endpoints(app)  # Also add non-streaming endpoints


# ─────────────────────────────────────────────────────────────────────
# Backward Compatibility
# ─────────────────────────────────────────────────────────────────────

def compatibility_wrapper(app: Flask) -> None:
    """
    Wrap old /tool endpoint to use new unified system if appropriate.
    
    This allows gradual migration from old to new system.
    """
    old_tool = app.view_functions.get("tool")
    
    @app.route("/tool", methods=["POST"])
    def tool_compat():
        """
        Handle both old and new tool call formats.
        
        Old format (from daemon): {"name": "tool_name", "arguments": {...}}
        New format: {"name": "tool_name", "arguments": {...}, "use_unified": true}
        """
        data = request.get_json() or {}
        
        # If old daemon calls this, use old system
        if "use_unified" not in data and old_tool:
            return old_tool()
        
        # If explicitly requested unified, use new system
        tool_name = data.get("name", "")
        args = data.get("arguments", {})
        
        if not tool_name:
            return jsonify({"error": "Missing tool name"}), 400
        
        try:
            # Try to find and execute the tool via the new system
            from server.tool_server import execute_native_tool
            result = execute_native_tool(tool_name, args)
            return jsonify({"result": result})
        except Exception as e:
            # Fall back to old system
            if old_tool:
                return old_tool()
            return jsonify({"error": str(e)}), 500
