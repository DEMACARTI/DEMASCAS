"""
dispatcher.py - Routes commands to domain agents by keyword scoring.

DOMAIN_KEYWORDS maps domain names to lists of trigger phrases.
Scoring: each matching phrase scores len(phrase.split()) points (word count).
Longer phrases win over shorter (more specific beats more general).
Fallback domain when score == 0: "system".

Routing priority order for tie-breaking: comms > app_learner > workflow > app_builder > browser > file > research > system
"""

import re
from typing import Dict

DOMAIN_KEYWORDS = {
    "browser": [
        "open gmail", "check email", "compose email", "reply to email",
        "send email", "read email", "gmail", "mail",
        "browse", "navigate to", "go to website", "open website",
        "search the web", "look up online", "google it",
        "youtube", "watch video", "play video",
        "download from", "web page", "webpage",
        "brave", "browser", "tab", "website",
        "shop for", "buy online", "amazon", "order",
        "log in", "sign in", "fill form",
        "click on the", "click the button",
    ],
    "file": [
        "open my", "open the file", "open this file",
        "open downloads folder", "open download folder",
        "open documents folder", "open desktop folder",
        "find the", "find my", "find file",
        "pdf", "document", "docx", "spreadsheet", "excel",
        "pptx", "presentation", "slides",
        "download folder", "documents folder", "desktop",
        "show me the file", "list files", "what files",
        "create a file", "make a new file", "save as",
        "delete the file", "move the file", "rename",
        "read the pdf", "summarize the pdf",
        "latest pdf", "most recent file",
    ],
    "research": [
        "research", "look up", "find information about",
        "tell me about", "what is", "who is", "summarize",
        "explain", "define", "learn about",
        "news about", "latest on", "what happened",
        "weather in", "weather today",
        "compare", "difference between",
        "find out", "dig into", "investigate",
    ],
    "app_learner": [
        "learn how to use", "learn the app", "what can you do in",
        "how do i use", "explore the app", "map out",
        "figure out how to use", "teach yourself",
        "what did you learn about",
    ],
    "workflow": [
        "record a workflow", "save this workflow", "run the workflow",
        "replay workflow", "what workflows", "add a step to",
        "create a workflow", "automate this sequence",
        "run the", "play the workflow",
        "study routine", "open my study routine", "start study routine",
        "student productivity pack", "student workflow pack",
        "start class notes", "start revision timer",
        "assignment planner", "capture citation",
        "daily student startup", "start my study day",
        "list student workflows", "show built in workflows",
    ],
    "app_builder": [
        "build a script", "create a script", "write a script",
        "build an app", "create a tool", "make a shortcut",
        "build me a", "create me a", "write me a",
        "schedule", "set up to run every", "automate",
        "explain what the script", "what does the script",
    ],
    "comms": [
        "compose mail", "draft mail", "new mail", "new email",
        "compose a message", "draft a message", "write an email",
        "reply to", "respond to",
        "send a message to", "message",
        "smart reply", "reply with",
        "extract contact", "find contact info",
        "who emailed", "what did they say",
        "schedule a meeting", "set up a call",
    ],
    "system": [
        "open app", "launch", "quit", "close app",
        "switch to", "activate",
        "take a screenshot", "screenshot",
        "volume up", "volume down", "mute",
        "brightness", "sleep", "lock",
        "copy", "paste", "undo", "redo",
        "new window", "new tab", "close window",
        "type this", "click on", "scroll down", "scroll up",
        "press", "keyboard shortcut",
        "what's on screen", "what do you see",
        "list running apps", "running apps",
    ],
}

_PRIORITY = [
    "comms",
    "app_learner",
    "workflow",
    "app_builder",
    "browser",
    "file",
    "research",
    "system",
]

_FORCE_PATTERNS = [
    # Local document summaries must reach the File agent (it has the PDF
    # tools) — checked before the generic "summarize" → research rule.
    ("file", r"\b(summari[sz]e|read)\b.{0,40}\b(pdfs?|documents?|docx|files?|pptx|presentation|slides)\b"),
    # New domains
    ("app_learner", r"\b(learn how to use|learn the app|explore the app)\b"),
    ("workflow", r"\b(record a workflow|run the workflow|replay workflow)\b"),
    ("app_builder", r"\b(build a script|create a script|write a script|build me a)\b"),
    ("app_learner", r"\b(learn how to use|learn the app|what can you do in|figure out how to use)\b"),
    ("workflow", r"\b(record a workflow|run the workflow|replay workflow|what workflows|add a step to)\b"),
    ("workflow", r"\b(student productivity pack|student workflow pack|install student pack|set up student workflows)\b"),
    ("workflow", r"\b(list student workflows|show student workflows|what student workflows|show built in workflows|list built in workflows|show builtin workflows)\b"),
    ("workflow", r"\b(start class notes|start notes session|lecture notes)\b"),
    ("workflow", r"\b(start revision timer|study timer|focus timer|pomodoro)\b"),
    ("workflow", r"\b(assignment planner|plan my assignments|homework planner)\b"),
    ("workflow", r"\b(capture citation|research citation|save source link|citation notes)\b"),
    ("workflow", r"\b(daily student startup|start my study day|student startup|start school workflow|start daily startup)\b"),
    ("workflow", r"\b(study routine|open my study routine|start study routine)\b"),
    ("app_builder", r"\b(build a script|create a script|write a script|build an app|create a tool|build me a|build me an|create me a|create me an)\b"),
    # Comms
    # All email composing goes to the Comms agent (subject/body drafting,
    # people-graph lookup) — including "Gmail draft" phrasing.
    ("comms", r"\b(compose|draft|write|send)\b.{0,25}\b(e?mails?|gmail)\b"),
    ("comms", r"\b(new|create)\b.{0,25}\b(e?mail|gmail)\b"),
    ("comms", r"\b(email|mail)\b.{0,20}\bto\b"),
    ("comms", r"\b(reply to|respond to|smart reply)\b"),

    # System (macOS app productivity)
    ("system", r"\b(create|creating|make|making|new|blank|start|build|prepare)\b.{0,40}\b(powerpoint|power point|presentation|slides|deck)\b"),
    ("system", r"\b(close|quit|exit)\b.{0,40}\b(powerpoint|power point|presentation|slides|keynote|word|excel|app)\b"),
    # Research
    ("research", r"\b(research|summarize|look up|news about|weather in)\b"),
    # File operations
    ("file", r"\b(find my|find the|open my|my pdf|my document|my resume)\b"),
    ("file", r"\bopen\b.{0,30}\b(downloads?|documents?|desktop)\b.{0,15}\bfolder\b"),
    ("file", r"\b(show|list)\b.{0,25}\b(downloads?|documents?|desktop)\b"),
    # Browser intents
    ("browser", r"\b(email|gmail)\b"),
    ("browser", r"\b(youtube|watch video|play video|play music)\b"),
    ("browser", r"\bopen\b.{0,20}\byoutube\b.{0,20}\b(brave|browser)\b"),
    ("browser", r"\bopen\b.{0,40}\b(website|page|site|url|link|youtube|wikipedia|google|reddit|github)\b"),
    ("browser", r"\bopen\b.{0,30}\b(website|page|site|url|link)\b"),
    ("browser", r"\bopen\b.{0,30}(wikipedia|google|reddit|github|twitter|youtube|instagram|amazon)"),
    ("browser", r"\bopen\b.{0,30}(\.com|\.org|\.io|\.net|\.edu)"),
    ("browser", r"\b(go to|navigate to|browse to)\b"),
    ("browser", r"\bwikipedia\b"),
    ("browser", r"\bsearch (for|the web)\b"),
]


class AgentDispatcher:
    """
    Domain router using keyword scoring with phrase-length weighting.
    """

    def __init__(self):
        self._compiled: dict[str, list[tuple[str, re.Pattern[str], int]]] = {}
        for domain, keywords in DOMAIN_KEYWORDS.items():
            ordered = sorted(set(keywords), key=lambda k: len(k.split()), reverse=True)
            rows: list[tuple[str, re.Pattern[str], int]] = []
            for kw in ordered:
                pattern = re.compile(r"\b" + re.escape(kw.lower()) + r"\b", re.IGNORECASE)
                rows.append((kw, pattern, len(kw.split())))
            self._compiled[domain] = rows

        self._force_patterns = [(domain, re.compile(pat, re.IGNORECASE)) for domain, pat in _FORCE_PATTERNS]

        self._single_step_patterns = [
            re.compile(r"^(open|launch|start)\s+\w+$", re.IGNORECASE),
            re.compile(r"^(quit|close|exit)\s+\w+$", re.IGNORECASE),
            re.compile(r"^(copy|paste|undo|redo|save)$", re.IGNORECASE),
            re.compile(r"^(mute|unmute|volume up|volume down)$", re.IGNORECASE),
        ]

        self._agent_positive_patterns = [
            re.compile(r"\b(then|and then|after that|next|also|finally)\b", re.IGNORECASE),
            re.compile(r"\bopen\b.{0,30}\b(downloads?|documents?|desktop)\b.{0,15}\bfolder\b", re.IGNORECASE),
            re.compile(r"\bopen\b.{0,40}\b(youtube|wikipedia|google|reddit|github|website|site|url|link)\b", re.IGNORECASE),
            re.compile(r"\b(find|search|look up|research|summarize)\b", re.IGNORECASE),
            re.compile(r"\b(compose|draft|write|reply|respond)\b", re.IGNORECASE),
            re.compile(r"\b(create|new|blank)\b.{0,40}\b(powerpoint|power point|presentation|slides)\b", re.IGNORECASE),
            re.compile(r"\b(open my|open the file|my pdf|my document)\b", re.IGNORECASE),
            re.compile(r"\b(browse|navigate|go to|open website)\b", re.IGNORECASE),
            re.compile(r"\b(youtube|watch|play video)\b", re.IGNORECASE),
            re.compile(r"\b(email|gmail|mail)\b", re.IGNORECASE),
            re.compile(r"\b(learn how to use|learn the app|what can you do in|how do i use)\b", re.IGNORECASE),
            re.compile(r"\b(record a workflow|run the workflow|replay workflow|what workflows|add a step to)\b", re.IGNORECASE),
            re.compile(r"\b(student productivity pack|student workflow pack|install student pack|set up student workflows)\b", re.IGNORECASE),
            re.compile(r"\b(start class notes|start revision timer|assignment planner|capture citation|study timer|pomodoro|daily student startup|start my study day|student startup|list student workflows|show built in workflows)\b", re.IGNORECASE),
            re.compile(r"\b(study routine|open my study routine|start study routine)\b", re.IGNORECASE),
            re.compile(r"\b(build a script|create a script|write a script|build an app|create a tool|build me a|create me a)\b", re.IGNORECASE),
        ]

    def route(self, command: str) -> str:
        text = (command or "").strip()

        # App-close intent should never go to file/research domains.
        if re.search(r"\b(close|quit|exit)\b", text, re.IGNORECASE) and re.search(
            r"\b(powerpoint|power point|presentation|slides|keynote|word|excel|app|application)\b",
            text,
            re.IGNORECASE,
        ):
            return "system"

        for domain, pattern in self._force_patterns:
            if pattern.search(text):
                return domain

        scores = self.route_with_scores(command)
        best = max(scores.values()) if scores else 0
        if best == 0:
            return "system"

        winners = [d for d, s in scores.items() if s == best]
        for domain in _PRIORITY:
            if domain in winners:
                return domain
        return "system"

    def route_with_scores(self, command: str) -> Dict[str, int]:
        text = (command or "").lower().strip()
        out: Dict[str, int] = {domain: 0 for domain in DOMAIN_KEYWORDS.keys()}
        if not text:
            return out

        for domain, rows in self._compiled.items():
            score = 0
            for _, pattern, points in rows:
                if pattern.search(text):
                    score += points
            out[domain] = score

        return out

    def scores(self, command: str) -> Dict[str, int]:
        return self.route_with_scores(command)

    def should_use_agent(self, command: str) -> bool:
        text = (command or "").strip()
        if not text:
            return False

        for pat in self._single_step_patterns:
            if pat.search(text):
                return False

        for pat in self._agent_positive_patterns:
            if pat.search(text):
                return True

        return False


_dispatcher_instance = None


def get_dispatcher() -> AgentDispatcher:
    global _dispatcher_instance
    if _dispatcher_instance is None:
        _dispatcher_instance = AgentDispatcher()
    return _dispatcher_instance


def route_command(command: str) -> str:
    return get_dispatcher().route(command)


def should_use_agent(command: str) -> bool:
    return get_dispatcher().should_use_agent(command)
