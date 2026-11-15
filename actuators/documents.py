"""
DEMASCAS — actuators/documents.py
Create and edit Office documents directly as files (cross-platform), then
open them in whatever app the laptop has.

Why files instead of clicking through PowerPoint:
  building the .pptx with python-pptx takes milliseconds and always works;
  driving PowerPoint's UI with a 3B model is slow and fragile.  The LLM's only
  job is the CONTENT (one JSON outline call).

Edits ("change slide 3's title", "add a slide about X") are structured
operations applied to the file; if the deck is open in PowerPoint it is
refreshed through PowerPoint's scripting interface (close without saving +
reopen), so you see the change.

Works on macOS, Windows and Linux (opening uses the OS default handler when
PowerPoint/Keynote aren't present).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

DOCS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "DEMASCAS")
_STATE_PATH = os.path.join(os.path.expanduser("~"), ".demascas", "documents.json")


# ── helpers ──────────────────────────────────────────────────────────

def _llm_json(prompt: str, max_tokens: int) -> dict | None:
    """One JSON-only call to the plugged-in LLM (see core/llm)."""
    from actuators.file_system import _llm_complete
    raw = _llm_complete(prompt + "\nReply with ONLY the JSON object — no explanation, no markdown.",
                        max_tokens=max_tokens)
    start, depth = -1, 0
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                cand = raw[start:i + 1]
                # Small models emit LaTeX-style \( \) and other escapes that
                # are illegal in JSON, and stray quotes before a closing
                # bracket ("x"']}) — repair both before giving up.
                no_esc = re.sub(r'\\(?!["\\/bfnrtu])', "", cand)
                for attempt in (cand, no_esc,
                                re.sub(r"(?<=\")\s*'+(?=\s*[\]}])", "", no_esc)):
                    try:
                        return json.loads(attempt)
                    except ValueError:
                        continue
                start = -1
    return None


def _grounding(topic: str, max_chars: int = 1500) -> str:
    """Fresh facts from a quick web search so a small model doesn't invent
    details (it made up school names in testing).  Best effort, bounded."""
    try:
        from core.web import store
        from core.web.crawler import research_web
        if store.coverage(topic, fresh_days=30) < 2:
            research_web(topic, answer=False, fresh_days=30)     # read + remember
        notes = store.context_block(topic, max_chars=max_chars, k=4)
        if notes:
            return notes
    except Exception:
        pass
    try:   # fallback: search-result snippets
        from actuators.network import search_web
        res = search_web(topic, max_results=5)
    except Exception:
        return ""
    if not res.startswith("Search results"):
        return ""
    lines = [ln.strip() for ln in res.splitlines()
             if ln.strip() and not ln.strip().startswith(("URL:", "Search results"))]
    return "\n".join(lines)[:max_chars]


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text or "").strip()
    s = re.sub(r"\s+", " ", s)[:60].strip()
    return s or "Untitled"


def _unique_path(title: str, ext: str) -> str:
    os.makedirs(DOCS_DIR, exist_ok=True)
    base = os.path.join(DOCS_DIR, _slug(title))
    path = f"{base}.{ext}"
    n = 2
    while os.path.exists(path):
        path = f"{base} ({n}).{ext}"
        n += 1
    return path


def _remember(kind: str, path: str) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        state = {}
        if os.path.exists(_STATE_PATH):
            with open(_STATE_PATH) as f:
                state = json.load(f)
        state[f"last_{kind}"] = path
        with open(_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def _last(kind: str) -> str:
    try:
        with open(_STATE_PATH) as f:
            return json.load(f).get(f"last_{kind}", "")
    except Exception:
        return ""


def _app_installed(name: str) -> bool:
    return sys.platform == "darwin" and os.path.isdir(f"/Applications/{name}.app")


def _open(path: str, preferred_apps: tuple[str, ...] = ()) -> str:
    """Open a file in the first installed preferred app, else the OS default."""
    try:
        if sys.platform == "darwin":
            for app in preferred_apps:
                if _app_installed(app):
                    subprocess.run(["open", "-a", app, path], check=True, timeout=15)
                    return app
            subprocess.run(["open", path], check=True, timeout=15)
            return "default app"
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
            return "default app"
        subprocess.Popen(["xdg-open", path])
        return "default app"
    except Exception as e:
        return f"[-] could not open: {e}"


# ── presentations ────────────────────────────────────────────────────

_OUTLINE_PROMPT = (
    "Write the content for a presentation.\n"
    "Topic: {topic}\n{extra}{facts}"
    "Return JSON: {{\"title\": \"...\", \"subtitle\": \"...\", \"slides\": "
    "[{{\"title\": \"...\", \"bullets\": [\"...\"], \"notes\": \"...\"}}]}}\n"
    "Rules: exactly {n} content slides; 3-4 short bullets each (max 12 words); "
    "notes = one sentence the presenter can say; factual and specific."
)


def _build_pptx(outline: dict, path: str) -> int:
    from pptx import Presentation
    from pptx.util import Pt

    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = str(outline.get("title") or "Presentation")
    if len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = str(outline.get("subtitle") or "")
    count = 0
    for s in outline.get("slides") or []:
        if not isinstance(s, dict):
            continue
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = str(s.get("title") or "")
        body = slide.placeholders[1].text_frame
        bullets = [str(b) for b in (s.get("bullets") or []) if str(b).strip()]
        for i, b in enumerate(bullets):
            para = body.paragraphs[0] if i == 0 else body.add_paragraph()
            para.text = b
            para.font.size = Pt(22)
        if s.get("notes"):
            slide.notes_slide.notes_text_frame.text = str(s["notes"])
        count += 1
    prs.save(path)
    return count


_SPLIT_PROMPT = """A user asked for a presentation with this request:
"{topic}"
Separate the SUBJECT (2-6 words) from any instructions about what it should cover.
Return JSON: {{"topic": "...", "focus": "what it should cover, or empty"}}"""


def _split_topic(topic: str) -> tuple[str, str]:
    """'about you, meaning DEMASCAS, and it should say what you are and what
    you do' → ('DEMASCAS', 'what it is and what it does')."""
    from core.self_profile import is_self_topic
    words = topic.split()
    if len(words) <= 6 and not re.search(r"\b(should|must|explain|cover|include|meaning)\b", topic, re.I):
        return topic, ""
    try:
        data = _llm_json(_SPLIT_PROMPT.format(topic=topic), max_tokens=80) or {}
    except RuntimeError:
        data = {}
    t = str(data.get("topic") or "").strip(" .")
    f = str(data.get("focus") or "").strip(" .")
    if "demascas" in topic.lower() and (not t or is_self_topic(t) or "you" in t.lower()):
        t = "DEMASCAS"
    if not t or len(t.split()) > 8:
        t = " ".join(re.split(r"[,;]| and | which | that ", topic)[0].split()[:6])
    return t, f


def create_presentation(topic: str, num_slides: int = 5, audience: str = "",
                        open_after: bool = True, grounded: bool = True, focus: str = "") -> dict:
    """
    Create a PowerPoint presentation about a topic: the LLM writes the slide
    content, DEMASCAS builds the .pptx in ~/Documents/DEMASCAS and opens it
    (PowerPoint, else Keynote, else the default app).
    """
    topic = (topic or "").strip()
    if not topic:
        return {"success": False, "message": "[-] What should the presentation be about?"}
    n = max(2, min(int(num_slides or 5), 12))
    t0 = time.time()
    topic, split_focus = _split_topic(topic)
    focus = focus or split_focus
    extra = (f"Audience: {audience}\n" if audience else "") + (f"Focus on: {focus}\n" if focus else "")
    from core.self_profile import is_self_topic, self_facts
    if is_self_topic(topic):
        # About DEMASCAS itself: its own documentation, not a web search.
        topic = "DEMASCAS"
        facts, source = self_facts(), "These facts describe the subject"
    else:
        facts, source = (_grounding(topic) if grounded else ""), "Use these facts from a web search"
    t_search = time.time() - t0
    facts_block = (f"{source} (don't invent names, dates or places):\n{facts}\n" if facts else "")
    outline = None
    for attempt, (slides_n, fb) in enumerate(((n, facts_block), (min(n, 4), facts_block[:800]))):
        try:
            outline = _llm_json(_OUTLINE_PROMPT.format(topic=topic, extra=extra, n=slides_n, facts=fb),
                                max_tokens=120 + 110 * slides_n)
        except RuntimeError as e:
            return {"success": False, "message": f"[-] {e}"}
        if outline and outline.get("slides"):
            break                          # else retry once: fewer slides, shorter context
    if not outline or not outline.get("slides"):
        return {"success": False, "message": "[-] The model didn't return a usable slide outline."}
    t_llm = time.time() - t0
    path = _unique_path(outline.get("title") or topic, "pptx")
    slides = _build_pptx(outline, path)
    _remember("presentation", path)
    opened = _open(path, ("Microsoft PowerPoint", "Keynote")) if open_after else "not opened"
    return {
        "success": True,
        "message": f"Created a {slides + 1}-slide presentation \"{outline.get('title', topic)}\".",
        "path": path, "slides": slides + 1, "opened_in": opened,
        "timing_s": {"search": round(t_search, 1), "content": round(t_llm - t_search, 1),
                      "total": round(time.time() - t0, 1)},
    }


_EDIT_PROMPT = (
    "A presentation has these slides:\n{slides}\n"
    "Instruction: {instruction}\n"
    "Return JSON: {{\"ops\": [ ... ]}} using only these operations:\n"
    "  {{\"op\": \"set_title\", \"slide\": N, \"title\": \"...\"}}\n"
    "  {{\"op\": \"set_bullets\", \"slide\": N, \"bullets\": [\"...\"]}}\n"
    "  {{\"op\": \"add_slide\", \"after\": N, \"title\": \"...\", \"bullets\": [\"...\"]}}\n"
    "  {{\"op\": \"delete_slide\", \"slide\": N}}\n"
    "Slides are numbered from 1 (slide 1 is the title slide)."
)


def _slide_summary(prs) -> str:
    lines = []
    for i, slide in enumerate(prs.slides, 1):
        title = slide.shapes.title.text if slide.shapes.title is not None else ""
        lines.append(f"{i}. {title}")
    return "\n".join(lines)


def _delete_slide(prs, index0: int) -> None:
    sldIdLst = prs.slides._sldIdLst  # python-pptx has no public delete API
    sld = list(sldIdLst)[index0]
    prs.part.drop_rel(sld.rId)
    sldIdLst.remove(sld)


def _move_slide(prs, old0: int, new0: int) -> None:
    sldIdLst = prs.slides._sldIdLst
    el = list(sldIdLst)[old0]
    sldIdLst.remove(el)
    sldIdLst.insert(new0, el)


def _apply_ops(prs, ops: list[dict]) -> list[str]:
    from pptx.util import Pt
    done = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        total = len(prs.slides)
        try:
            if kind == "set_title":
                n = int(op["slide"])
                if 1 <= n <= total and prs.slides[n - 1].shapes.title is not None:
                    prs.slides[n - 1].shapes.title.text = str(op.get("title", ""))
                    done.append(f"retitled slide {n}")
            elif kind == "set_bullets":
                n = int(op["slide"])
                if not op.get("bullets"):
                    continue          # never replace a slide's content with nothing
                if 1 <= n <= total and len(prs.slides[n - 1].placeholders) > 1:
                    tf = prs.slides[n - 1].placeholders[1].text_frame
                    tf.clear()
                    for i, b in enumerate(op.get("bullets") or []):
                        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                        p.text = str(b)
                        p.font.size = Pt(22)
                    done.append(f"rewrote slide {n}")
            elif kind == "add_slide":
                after = max(0, min(int(op.get("after", total)), total))
                slide = prs.slides.add_slide(prs.slide_layouts[1])
                slide.shapes.title.text = str(op.get("title", ""))
                tf = slide.placeholders[1].text_frame
                for i, b in enumerate(op.get("bullets") or []):
                    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    p.text = str(b)
                    p.font.size = Pt(22)
                _move_slide(prs, len(prs.slides) - 1, after)
                done.append(f"added slide {after + 1}")
            elif kind == "delete_slide":
                n = int(op["slide"])
                if 2 <= n <= total:     # never delete the title slide by accident
                    _delete_slide(prs, n - 1)
                    done.append(f"deleted slide {n}")
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    return done


def _refresh_in_powerpoint(path: str) -> bool:
    """If the deck is open in PowerPoint, close it (no save — the file on
    disk is newer) and reopen so the edit shows.  Path passed via argv."""
    if sys.platform != "darwin" or not _app_installed("Microsoft PowerPoint"):
        return False
    running = subprocess.run(["pgrep", "-x", "Microsoft PowerPoint"], capture_output=True)
    if running.returncode != 0:
        return False
    script = (
        'on run argv\n'
        '  tell application "Microsoft PowerPoint"\n'
        '    repeat with p in (presentations whose full name is (item 1 of argv))\n'
        '      close p saving no\n'
        '    end repeat\n'
        '  end tell\n'
        'end run'
    )
    subprocess.run(["osascript", "-e", script, path], capture_output=True, timeout=15)
    return _open(path, ("Microsoft PowerPoint",)) == "Microsoft PowerPoint"


_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
              "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
              "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_N = r"(\d+|" + "|".join(_NUM_WORDS) + r")"
_VERBS = r"(?:change|rename|retitle|set|make|delete|remove|add|insert|rewrite|update|redo)"


def _num(tok: str) -> int:
    tok = tok.lower()
    return int(tok) if tok.isdigit() else _NUM_WORDS.get(tok, 0)


def _bullets_for(title: str, deck_title: str, focus: str = "") -> list[str]:
    facts = _grounding(f"{deck_title} {focus or title}", max_chars=900)
    data = _llm_json(
        f"Write 3-4 short bullet points (max 12 words each) for a presentation slide "
        f"titled \"{title}\" in a presentation about \"{deck_title}\"."
        + (f" Focus: {focus}." if focus else "")
        + (f"\nUse these facts from a web search (don't invent names, dates or places):\n{facts}"
           if facts else "")
        + "\nReturn JSON: {\"bullets\": [\"...\"]}", max_tokens=220)
    bullets = (data or {}).get("bullets") or []
    return [str(b) for b in bullets if isinstance(b, (str, int, float)) and str(b).strip()][:5]


def _clause_ops(clause: str, prs) -> list[dict] | None:
    """Deterministic parse of one edit clause; None if not recognised."""
    c = clause.strip().rstrip(".")
    total = len(prs.slides)
    deck_title = prs.slides[0].shapes.title.text if total and prs.slides[0].shapes.title is not None else ""
    m = (re.search(rf"(?:change|rename|set|make)\s+(?:the\s+)?title\s+of\s+(?:the\s+)?(?:slide\s+{_N}|{_N}\s+slide)\s+(?:to|as|into)\s+(.+)", c, re.I)
         or re.search(rf"(?:rename|retitle)\s+(?:the\s+)?(?:slide\s+{_N}|{_N}\s+slide)\s+(?:to|as)\s+(.+)", c, re.I)
         or re.search(rf"slide\s+{_N}(?:'s)?\s+title\s+(?:to|should be|as)\s+(.+)", c, re.I))
    if m:
        groups = [g for g in m.groups() if g]
        return [{"op": "set_title", "slide": _num(groups[0]), "title": groups[-1].strip(" \"'")}]
    m = re.search(rf"(?:delete|remove)\s+(?:the\s+)?(?:slide\s+{_N}|{_N}\s+slide)", c, re.I)
    if m:
        return [{"op": "delete_slide", "slide": _num(next(g for g in m.groups() if g))}]
    m = re.search(r"(?:add|insert)\s+(?:a\s+|another\s+|one\s+more\s+|a\s+new\s+)?slide\s+(?:about|on|for|covering|regarding)\s+(.+)", c, re.I)
    if m:
        rest = m.group(1)
        after = total
        pos = re.search(rf"\s+(?:after\s+slide\s+{_N}|before\s+slide\s+{_N}|at\s+the\s+(end|beginning|start))\s*$", rest, re.I)
        if pos:
            if pos.group(1):
                after = _num(pos.group(1))
            elif pos.group(2):
                after = max(1, _num(pos.group(2)) - 1)
            elif pos.group(3) and pos.group(3).lower() in ("beginning", "start"):
                after = 1
            rest = rest[:pos.start()]
        topic = re.sub(r"^(his|her|their|its|the)\s+", "", rest.strip(" .\"'"), flags=re.I)
        title = topic[:1].upper() + topic[1:]
        return [{"op": "add_slide", "after": after, "title": title,
                 "bullets": _bullets_for(title, deck_title)}]
    m = re.search(rf"(?:rewrite|update|redo)\s+(?:the\s+)?(?:slide\s+{_N}|{_N}\s+slide)(?:\s+(?:about|to cover|on|with)\s+(.+))?", c, re.I)
    if m:
        groups = [g for g in m.groups()]
        n = _num(next(g for g in groups[:2] if g))
        if 1 <= n <= total:
            t = prs.slides[n - 1].shapes.title
            title = t.text if t is not None else ""
            return [{"op": "set_bullets", "slide": n,
                     "bullets": _bullets_for(title, deck_title, focus=groups[2] or "")}]
    return None


def edit_presentation(instruction: str, file_path: str = "") -> dict:
    """
    Edit a presentation (default: the one DEMASCAS created last), e.g.
    "change the title of slide 3 to Early Life and add a slide about his
    Nobel Prize at the end".  The deck is updated and refreshed in
    PowerPoint if it's open.

    Common edits are parsed deterministically; the LLM only writes slide
    CONTENT (bullets).  Small models produced malformed / wrong multi-op
    plans when asked to plan edits themselves.
    """
    from pptx import Presentation

    path = os.path.expanduser(file_path or _last("presentation"))
    if not path or not os.path.exists(path):
        return {"success": False, "message": "[-] Which presentation? I don't have one open."}
    prs = Presentation(path)
    clauses = re.split(rf"\s*(?:,\s*)?\b(?:and|then|also)\s+(?={_VERBS}\b)", instruction or "", flags=re.I)
    ops: list[dict] = []
    try:
        for clause in clauses:
            if not clause.strip():
                continue
            parsed = _clause_ops(clause, prs)
            if parsed is None:
                # Fallback: ask the model for ONE operation for this clause.
                plan = _llm_json(_EDIT_PROMPT.format(slides=_slide_summary(prs), instruction=clause)
                                 + "\nReturn exactly one operation.", max_tokens=250)
                parsed = ((plan or {}).get("ops") or [])[:1]
            ops.extend(parsed)
    except RuntimeError as e:
        return {"success": False, "message": f"[-] {e}"}
    done = _apply_ops(prs, ops)
    if not done:
        return {"success": False, "message": "[-] I couldn't work out how to make that change."}
    prs.save(path)
    _remember("presentation", path)
    refreshed = _refresh_in_powerpoint(path)
    return {"success": True, "message": "Done: " + ", ".join(done) + ".",
            "path": path, "refreshed_in_app": refreshed}


# ── documents & spreadsheets ─────────────────────────────────────────

def create_document(topic: str, kind: str = "document", open_after: bool = True) -> dict:
    """Write a Word document (letter, report, notes...) about a topic and open it."""
    from docx import Document

    topic = (topic or "").strip()
    if not topic:
        return {"success": False, "message": "[-] What should the document be about?"}
    try:
        data = _llm_json(
            f"Write a {kind} about: {topic}\n"
            "Return JSON: {\"title\": \"...\", \"sections\": [{\"heading\": \"...\", "
            "\"paragraphs\": [\"...\"]}]}\nRules: 3-5 sections, 1-2 short paragraphs each.",
            max_tokens=700)
    except RuntimeError as e:
        return {"success": False, "message": f"[-] {e}"}
    if not data or not data.get("sections"):
        return {"success": False, "message": "[-] The model didn't return usable content."}
    doc = Document()
    doc.add_heading(str(data.get("title") or topic), level=0)
    for sec in data["sections"]:
        if not isinstance(sec, dict):
            continue
        doc.add_heading(str(sec.get("heading", "")), level=1)
        for para in sec.get("paragraphs") or []:
            doc.add_paragraph(str(para))
    path = _unique_path(data.get("title") or topic, "docx")
    doc.save(path)
    _remember("document", path)
    opened = _open(path, ("Microsoft Word", "Pages")) if open_after else "not opened"
    return {"success": True, "message": f"Created the {kind} \"{data.get('title', topic)}\".",
            "path": path, "opened_in": opened}


def create_spreadsheet(description: str, open_after: bool = True) -> dict:
    """Create an Excel sheet from a description (e.g. "a weekly study timetable")."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    description = (description or "").strip()
    if not description:
        return {"success": False, "message": "[-] What should the spreadsheet contain?"}
    try:
        data = _llm_json(
            f"Design a spreadsheet: {description}\n"
            "Return JSON: {\"title\": \"...\", \"columns\": [\"...\"], \"rows\": [[...]]}\n"
            "Rules: 3-8 columns, 5-15 example rows, values short.",
            max_tokens=700)
    except RuntimeError as e:
        return {"success": False, "message": f"[-] {e}"}
    if not data or not data.get("columns"):
        return {"success": False, "message": "[-] The model didn't return a usable table."}
    wb = Workbook()
    ws = wb.active
    ws.title = _slug(data.get("title") or "Sheet")[:31]
    ws.append([str(c) for c in data["columns"]])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in data.get("rows") or []:
        if isinstance(row, list):
            ws.append(row)
    path = _unique_path(data.get("title") or description, "xlsx")
    wb.save(path)
    _remember("spreadsheet", path)
    opened = _open(path, ("Microsoft Excel", "Numbers")) if open_after else "not opened"
    return {"success": True, "message": f"Created the spreadsheet \"{data.get('title', description)}\".",
            "path": path, "opened_in": opened}
