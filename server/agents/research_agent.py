"""
research_agent.py - Web research, news, weather, fact lookup.
"""

from datetime import datetime
import json
import re

import requests

from .base_agent import (
    AgentBase,
    AgentResult,
    StepRecord,
    LLM_TIMEOUT,
    MLX_TEXT_MODEL,
    MLX_TEXT_URL,
)


class ResearchAgent(AgentBase):
    domain_name = "research"
    tool_names = [
        "search_web", "read_webpage", "smart_search",
        "web_search", "web_read_article", "web_research",
        "get_weather", "get_news",
        "retrieve_memories", "save_memory",
        "create_file", "open_file",
    ]

    @property
    def system_prompt(self) -> str:
        return (
            "You are the DEMASCAS Research Agent for concise factual research and report writing.\n"
            "Rules:\n"
            "1. Before searching the web, check retrieve_memories(query=goal).\n"
            "2. Weather requests must use get_weather(location=...).\n"
            "3. News requests must use get_news(topic=...).\n"
            "4. Deep research should use web_research for multi-source synthesis.\n"
            "5. Read at most 3 web pages per task.\n"
            "6. After researching, save findings with save_memory including date and sources.\n"
            "7. Final response must be 2-4 short sentences for TTS.\n"
            "8. Never read the same URL twice.\n"
            "9. Skip paywall/login sources and choose alternatives.\n"
            "10. Finish with {'done': true, 'say': '2-4 sentence summary'}."
        )

    def run(self, goal, context):
        g = (goal or "").lower()
        if re.search(r"\b(weather|temperature|forecast|rain|sunny|cloudy)\b", g):
            return self._weather_flow(goal, context)
        if re.search(r"\b(news|headlines|what's happening|whats happening|today in)\b", g):
            return self._news_flow(goal, context)
        if re.search(r"\b(write a report on|report on|and write a report)\b", g):
            return self._report_flow(goal, context)
        if re.search(r"\bwhat else should i know about\b", g):
            return self._followup_flow(goal, context)

        cached = self._memory_first_lookup(goal)
        if cached is not None:
            return cached

        if re.search(r"\b(research|investigate|deep dive|find out about|tell me everything)\b", g):
            return self._deep_research_flow(goal, context)
        return self._execute(goal, context)

    def _weather_flow(self, goal, context):
        loc = self._extract_location(goal)
        result = self._call_tool("get_weather", {"location": loc})
        if self._is_tool_error(result):
            return AgentResult(False, "I couldn't fetch weather right now.", 1, ["get_weather"], error=result)
        return AgentResult(True, result[:300], 1, ["get_weather"])

    def _news_flow(self, goal, context):
        topic = self._extract_news_topic(goal)
        result = self._call_tool("get_news", {"topic": topic})
        if self._is_tool_error(result):
            return AgentResult(False, "I couldn't fetch news right now.", 1, ["get_news"], error=result)
        return AgentResult(True, result[:400], 1, ["get_news"])

    def _deep_research_flow(self, goal, context):
        topic = re.sub(
            r"\b(research|investigate|deep dive|find out about|tell me everything)\b",
            "",
            goal,
            flags=re.IGNORECASE,
        ).strip(" .")
        if not topic:
            topic = goal.strip()

        result = self._call_tool("web_research", {"topic": topic, "num_sources": 3})
        if self._is_tool_error(result):
            return AgentResult(False, "I couldn't complete deep research right now.", 1, ["web_research"], error=result)

        research_text = self._extract_tool_text(result)
        summary = self._make_summary(research_text)
        sources = self._extract_urls(research_text)
        mem_text = self._build_research_memory(topic, summary, sources)

        save_res = self._call_tool("save_memory", {"text": mem_text})
        tools = ["web_research"]
        steps = 1
        if not self._is_tool_error(save_res):
            tools.append("save_memory")
            steps = 2

        return AgentResult(True, summary, steps, tools)

    def _report_flow(self, goal, context):
        topic = self._extract_report_topic(goal)
        now = datetime.now().strftime("%Y-%m-%d")

        memories = self._call_tool("retrieve_memories", {"query": topic, "top_k": 3})
        research = self._call_tool("web_research", {"topic": topic, "num_sources": 3})
        if self._is_tool_error(research):
            return AgentResult(False, "I couldn't complete report research right now.", 2, ["retrieve_memories", "web_research"], error=research)

        research_text = self._extract_tool_text(research)
        summary = self._make_summary(research_text)
        findings = self._extract_findings(research_text)
        sources = self._extract_urls(research_text)

        report = self._build_report(topic, now, summary, findings, sources, memories)
        safe_topic = re.sub(r"[^a-zA-Z0-9]+", "_", topic).strip("_") or "topic"
        safe_topic = safe_topic[:40]
        report_path = f"~/Desktop/Research_{safe_topic}_{now}.md"

        create_res = self._call_tool("create_file", {"file_path": report_path, "content": report})
        if self._is_tool_error(create_res):
            return AgentResult(False, "I researched it but couldn't save the report file.", 3, ["retrieve_memories", "web_research", "create_file"], error=create_res)

        tools = ["retrieve_memories", "web_research", "create_file"]
        open_res = self._call_tool("open_file", {"file_path": report_path})
        if not self._is_tool_error(open_res):
            tools.append("open_file")

        mem_text = self._build_research_memory(topic, summary, sources)
        save_res = self._call_tool("save_memory", {"text": mem_text})
        if not self._is_tool_error(save_res):
            tools.append("save_memory")

        return AgentResult(True, f"Report saved to your Desktop as Research_{safe_topic}_{now}.md", len(tools), tools)

    def _followup_flow(self, goal, context):
        topic = re.sub(r".*what else should i know about", "", goal, flags=re.IGNORECASE).strip(" .")
        if not topic:
            topic = goal.strip()

        questions = self._generate_followup_questions(topic)
        mem_text = f"Follow-up questions for {topic}: " + " | ".join(questions)
        save_res = self._call_tool("save_memory", {"text": mem_text})

        spoken = " ".join(f"{idx + 1}. {q}" for idx, q in enumerate(questions))
        tool_calls = ["save_memory"] if not self._is_tool_error(save_res) else []
        return AgentResult(True, spoken, max(1, len(tool_calls)), tool_calls)

    def _memory_first_lookup(self, goal: str):
        memories = self._call_tool("retrieve_memories", {"query": goal, "top_k": 3})
        if self._is_tool_error(memories):
            return None

        text = (memories or "").strip()
        if not text:
            return None
        low = text.lower()
        if low.startswith("no memories") or low.startswith("no relevant"):
            return None

        entries = self._extract_memory_entries(text)
        research_entries = [e for e in entries if "research:" in e.lower()]
        if not research_entries:
            return None

        score = self._estimate_relevance(goal, " ".join(research_entries))
        if score < 0.8:
            return None

        date_match = re.search(r"\[(\d{4}-\d{2}-\d{2})\]", text)
        date_text = date_match.group(1) if date_match else "an earlier date"
        cached = self._trim_for_tts(" ".join(research_entries))
        return AgentResult(
            True,
            f"{cached} I researched this on {date_text}. Want me to check for updates?",
            1,
            ["retrieve_memories"],
            data={"cached": True, "memory_similarity": round(score, 2)},
        )

    def _estimate_relevance(self, goal: str, memory_text: str) -> float:
        goal_terms = {w for w in re.findall(r"[a-z0-9]+", goal.lower()) if len(w) > 3}
        mem_terms = {w for w in re.findall(r"[a-z0-9]+", memory_text.lower()) if len(w) > 3}
        if not goal_terms:
            return 0.0
        overlap = len(goal_terms & mem_terms) / max(len(goal_terms), 1)
        return overlap

    def _extract_memory_entries(self, text: str) -> list[str]:
        out: list[str] = []
        for line in (text or "").splitlines():
            ln = line.strip()
            if not ln:
                continue
            if re.match(r"^\d+\.\s+\[\d{4}-\d{2}-\d{2}\]\s+", ln):
                out.append(ln)
        return out

    def _extract_report_topic(self, goal: str) -> str:
        topic = re.sub(r".*(?:write a report on|report on)", "", goal, flags=re.IGNORECASE).strip(" .")
        if " and write a report" in topic.lower():
            topic = re.sub(r"\band write a report\b.*$", "", topic, flags=re.IGNORECASE).strip(" .")
        return topic or goal.strip()

    def _extract_tool_text(self, tool_result: str) -> str:
        text = (tool_result or "").strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "message" in parsed:
                return str(parsed.get("message", ""))
        except Exception:
            pass
        match = re.search(r"'message':\s*'(.+)'", text)
        if match:
            return match.group(1)
        return text

    def _make_summary(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", (text or "")).strip()
        if not cleaned:
            return "I couldn't extract enough details from the sources."
        sentences = re.split(r"(?<=[.!?])\s+", cleaned)
        picked = [s for s in sentences if len(s) > 20][:3]
        out = " ".join(picked).strip()
        if not out:
            out = cleaned[:420]
        return out[:520]

    def _extract_urls(self, text: str) -> list[str]:
        urls = re.findall(r"https?://[^\s)\]]+", text or "")
        deduped: list[str] = []
        for u in urls:
            if u not in deduped:
                deduped.append(u)
        return deduped[:6]

    def _extract_findings(self, text: str) -> list[str]:
        lines = [ln.strip(" -\t") for ln in (text or "").splitlines()]
        lines = [ln for ln in lines if len(ln) > 25 and not ln.lower().startswith("source") and "http" not in ln]
        if not lines:
            summary = self._make_summary(text)
            return [summary] if summary else ["No clear findings extracted."]
        return lines[:5]

    def _build_report(self, topic: str, date_text: str, summary: str, findings: list[str], sources: list[str], memories: str) -> str:
        source_lines = "\n".join(f"- {u}" for u in sources) if sources else "- No explicit URLs extracted"
        finding_lines = "\n".join(f"- {f}" for f in findings[:5])
        memory_section = ""
        mem_text = (memories or "").strip()
        if mem_text and not mem_text.lower().startswith("no memories") and not mem_text.lower().startswith("no relevant"):
            memory_section = f"\n## Prior Knowledge\n{self._trim_for_tts(mem_text)}\n"

        return (
            f"# Research Report: {topic}\n\n"
            f"Date: {date_text}\n\n"
            f"## Summary\n{summary}\n\n"
            f"## Key Findings\n{finding_lines}\n\n"
            f"## Sources\n{source_lines}\n"
            f"{memory_section}"
        )

    def _build_research_memory(self, topic: str, summary: str, sources: list[str]) -> str:
        date_text = datetime.now().strftime("%Y-%m-%d")
        source_text = ", ".join(sources) if sources else "none"
        return f"Research: {topic}\nDate: {date_text}\nFindings: {summary}\nSources: {source_text}"

    def _trim_for_tts(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if len(cleaned) > 320:
            cleaned = cleaned[:320] + "..."
        return cleaned

    def _generate_followup_questions(self, topic: str) -> list[str]:
        fallback = [
            f"What are the latest developments in {topic}?",
            f"What risks or limitations should I know about for {topic}?",
            f"Which practical next steps should I take to learn more about {topic}?",
        ]
        prompt = (
            "Generate exactly 3 concise follow-up research questions. "
            "Return only the questions, one per line, no numbering."
        )
        payload = {
            "model": MLX_TEXT_MODEL,
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 180,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Topic: {topic}"},
            ],
        }
        try:
            resp = requests.post(MLX_TEXT_URL, json=payload, timeout=LLM_TIMEOUT)
            if resp.status_code != 200:
                return fallback
            body = resp.json()
            raw = ""
            choices = body.get("choices", [])
            if choices:
                raw = choices[0].get("message", {}).get("content", "")
            if not raw:
                return fallback
            lines = [ln.strip(" -\t0123456789.") for ln in raw.splitlines() if ln.strip()]
            cleaned = [ln if ln.endswith("?") else f"{ln}?" for ln in lines if len(ln) > 8]
            if len(cleaned) >= 3:
                return cleaned[:3]
            return fallback
        except Exception:
            return fallback

    def _extract_location(self, goal):
        m = re.search(r"weather\s+(?:in|for|at|near)\s+(.+)$", goal, re.IGNORECASE)
        if m:
            return m.group(1).strip(" .")
        return "your location"

    def _extract_news_topic(self, goal):
        m = re.search(r"news\s+(?:about|on)\s+(.+)$", goal, re.IGNORECASE)
        if m:
            return m.group(1).strip(" .")
        m = re.search(r"latest\s+on\s+(.+)$", goal, re.IGNORECASE)
        if m:
            return m.group(1).strip(" .")
        return "general"

    def _on_step_complete(self, record):
        if not record.success:
            return
        if record.tool in {"smart_search", "web_search"}:
            self._active_context["last_result"] = (record.result or "")[:200]
