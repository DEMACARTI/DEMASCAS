"""
DEMASCAS — actuators/file_system.py
The Librarian: Local RAG capabilities — PDF reading, file searching,
and the watchdog-based file observer for automated document processing.
"""

import glob
import os
import subprocess
import time

import PyPDF2
import requests

from core.config import LLM_CONFIGURED, LLM_MODEL, LLM_CHAT_URL
from core.utils import resolve_safe_path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WATCH_DIRECTORY = os.path.expanduser("~/Desktop/DEMASCAS_Inbox")
OLLAMA_MODEL = "qwen2.5:3b"   # only used if the MLX server is unreachable


def _llm_complete(prompt: str, max_tokens: int = 300) -> str:
    """Run a one-shot completion on the user's plugged-in local LLM (the same
    model the daemon uses).  Falls back to Ollama only if it is installed
    and MLX is unreachable.  Raises RuntimeError if neither works."""
    if not LLM_CONFIGURED:
        raise RuntimeError("no language model is set up yet (run: python main.py setup)")
    try:
        resp = requests.post(
            LLM_CHAT_URL,
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0,
                "stream": False,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as mlx_err:
        try:
            import ollama  # optional legacy backend
        except ImportError:
            raise RuntimeError(f"MLX text server unavailable ({mlx_err})") from mlx_err
        try:
            response = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"num_ctx": 4096, "num_predict": max_tokens, "temperature": 0},
            )
            return response["message"]["content"].strip()
        except Exception as ollama_err:
            raise RuntimeError(
                f"No LLM available (MLX: {mlx_err}; Ollama: {ollama_err})"
            ) from ollama_err


# ---------------------------------------------------------------------------
# PDF summarization
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_path: str, max_pages: int = 3, max_chars: int = 4000) -> str:
    """Read up to max_pages from a PDF and return extracted text."""
    pages: list[str] = []
    with open(pdf_path, 'rb') as file:
        reader = PyPDF2.PdfReader(file)
        for i in range(min(max_pages, len(reader.pages))):
            page_text = reader.pages[i].extract_text()
            if page_text:
                pages.append(page_text)
                if sum(len(p) for p in pages) >= max_chars:
                    break

    text_content = "\n".join(pages)
    return text_content[:max_chars]


def _summarize_pdf_text(file_name: str, text_content: str) -> str:
    """Summarize already extracted PDF text with the local LLM."""
    if not text_content.strip():
        return f"Failed: '{file_name}' appears to be empty or contains only images."

    print("[*] Text extracted. Routing to LLM for summarization...")
    summary_prompt = (
        "Summarize the following document in 3 concise bullet points:\n\n"
        f"{text_content}"
    )

    try:
        summary = _llm_complete(summary_prompt, max_tokens=300)
    except RuntimeError as e:
        return f"Failed to summarize '{file_name}': {e}"
    return f"Summary of {file_name}:\n" + summary


def _get_selected_finder_file() -> str:
    """Return POSIX path of the first selected Finder item, or empty string."""
    script = (
        'tell application "Finder"\n'
        '  if not (exists Finder window 1) then return ""\n'
        '  set sel to selection\n'
        '  if (count sel) is 0 then return ""\n'
        '  return POSIX path of (item 1 of sel as alias)\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()
    except Exception:
        return ""

def summarize_latest_pdf() -> str:
    """
    Finds the newest PDF in ~/Downloads, reads the first 3 pages,
    and asks the local LLM for a concise summary.

    Returns:
        A bullet-point summary string, or a failure message.
    """
    print("[*] DEMASCAS is scanning ~/Downloads for the latest PDF...")

    downloads_path = os.path.expanduser("~/Downloads")
    pdf_files = glob.glob(os.path.join(downloads_path, "*.pdf"))

    if not pdf_files:
        return "Failed: No PDF files found in the Downloads folder."

    latest_pdf = max(pdf_files, key=os.path.getctime)
    file_name = os.path.basename(latest_pdf)
    print(f"[*] Found target: '{file_name}'. Extracting text...")

    try:
        text_content = _extract_pdf_text(latest_pdf, max_pages=3, max_chars=4000)
    except Exception as e:
        return f"Failed to read PDF: {e}"

    return _summarize_pdf_text(file_name, text_content)


def summarize_selected_pdf(file_path: str = "") -> str:
    """
    Summarize the selected PDF in Finder, or a provided PDF file path.

    Args:
        file_path: Optional explicit path. If omitted, reads Finder selection.

    Returns:
        A bullet-point summary string, or a clear failure message.
    """
    target = (file_path or "").strip()
    if target:
        target = str(resolve_safe_path(target))
    else:
        target = _get_selected_finder_file()

    if not target:
        return "Failed: No file is selected in Finder. Please select a PDF and try again."
    if not os.path.exists(target):
        return f"Failed: Selected file not found: {target}"
    if not target.lower().endswith(".pdf"):
        return "Failed: The selected file is not a PDF. Please select a .pdf file."

    file_name = os.path.basename(target)
    print(f"[*] Summarizing selected PDF: '{file_name}'")
    try:
        text_content = _extract_pdf_text(target, max_pages=3, max_chars=4000)
    except Exception as e:
        return f"Failed to read selected PDF: {e}"

    return _summarize_pdf_text(file_name, text_content)


def list_files_in_folder(folder_path: str = "~/Downloads") -> str:
    """
    Lists files in a given folder, sorted by modification time (newest first).
    Returns up to 20 file names with sizes.

    Args:
        folder_path: Path to the folder (default: ~/Downloads).
                     Supports ~ for home directory.

    Returns:
        A formatted list of file names, or a failure message.
    """
    expanded = os.path.expanduser(folder_path)
    print(f"[*] DEMASCAS is listing files in: {expanded}")

    if not os.path.isdir(expanded):
        return f"Failed: '{folder_path}' is not a valid folder."

    try:
        entries = []
        for name in os.listdir(expanded):
            full = os.path.join(expanded, name)
            if os.path.isfile(full):
                size_mb = os.path.getsize(full) / (1024 * 1024)
                mtime = os.path.getmtime(full)
                entries.append((name, size_mb, mtime))

        if not entries:
            return f"The folder '{folder_path}' is empty."

        # Sort newest first
        entries.sort(key=lambda x: x[2], reverse=True)
        lines = []
        for name, size_mb, _ in entries[:20]:
            if size_mb >= 1.0:
                lines.append(f"  {name} ({size_mb:.1f} MB)")
            else:
                lines.append(f"  {name} ({size_mb*1024:.0f} KB)")

        header = f"Files in {folder_path} ({len(entries)} total, showing newest 20):"
        return header + "\n" + "\n".join(lines)
    except Exception as e:
        return f"Failed to list files: {e}"
# ---------------------------------------------------------------------------
# File creation & deletion (deletion requires voice confirmation, enforced server-side)
# ---------------------------------------------------------------------------

def create_file(file_path: str, content: str = "") -> str:
    """
    Creates a new file at the given path with optional content.
    Parent directories are created automatically if they don't exist.
    Never overwrites an existing file.

    Args:
        file_path: Absolute or ~-relative path (e.g. '~/Desktop/notes.txt').
        content:   Text to write into the file (default: empty).

    Returns:
        A success/failure message string.
    """
    expanded = str(resolve_safe_path(file_path))
    print(f"[*] DEMASCAS is creating file: {expanded}")

    try:
        parent = os.path.dirname(expanded)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if os.path.exists(expanded):
            return (f"Refused: '{file_path}' already exists. "
                    "Tell the user and ask if they want to overwrite it.")

        with open(expanded, 'w', encoding='utf-8') as f:
            f.write(content)

        size = os.path.getsize(expanded)
        return f"Success: Created '{file_path}' ({size} bytes)."
    except PermissionError:
        return f"Failed: Permission denied for '{file_path}'."
    except Exception as e:
        return f"Failed to create file: {e}"


def delete_file(file_path: str) -> str:
    """
    Moves a file (or empty directory) at the given path to the macOS Trash
    so the user can recover it.  Never deletes permanently.

    SAFETY: The tool server never runs this directly from an LLM or agent
    call — it creates a pending confirmation that the user must approve
    by voice (see CONFIRM_REQUIRED_TOOLS in server/tool_server.py).

    Args:
        file_path: Absolute or ~-relative path (e.g. '~/Desktop/notes.txt').

    Returns:
        A success/failure message string.
    """
    expanded = str(resolve_safe_path(file_path))
    print(f"[*] DEMASCAS is deleting: {expanded}")

    if not os.path.lexists(expanded):
        return f"Failed: '{file_path}' does not exist."

    if os.path.isdir(expanded) and not os.path.islink(expanded) and os.listdir(expanded):
        return (f"Refused: '{file_path}' is a non-empty directory. "
                "DEMASCAS won't delete directories with contents.")

    # Always move to the Trash (recoverable).  The path is passed as an
    # argv item, never spliced into the script, so quotes in file names
    # cannot inject AppleScript.  There is deliberately NO permanent-delete
    # fallback: if Finder refuses (e.g. missing Automation permission), we
    # report the failure instead of destroying data.
    script = (
        'on run argv\n'
        '  tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\n'
        'end run'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script, expanded],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return f"Failed: Timed out moving '{file_path}' to the Trash."
    except Exception as e:
        return f"Failed to delete: {e}"

    if result.returncode == 0:
        return f"Success: Moved '{file_path}' to Trash."
    err = (result.stderr or "").strip()
    hint = ""
    if "-1743" in err or "not allowed" in err.lower():
        hint = (" Grant Automation permission for Finder in System Settings → "
                "Privacy & Security → Automation.")
    return f"Failed: Could not move '{file_path}' to the Trash: {err}{hint}"


# ---------------------------------------------------------------------------
# File search & open
# ---------------------------------------------------------------------------

def find_file(folder: str, partial_name: str, extension: str = None) -> dict:
    """
    Recursively searches `folder` for files whose name contains `partial_name`.
    If `extension` is provided (e.g. "pdf", "pptx"), filter to that type only.
    Returns: {"found": True/False, "matches": [...], "count": int, "first_match_path": str|None}

    Path resolution: Uses resolve_safe_path() to fix hallucinated usernames.
    Generic name handling: If partial_name is generic (empty, "file", "a", etc.),
    lists all files of the given extension instead of searching for a literal name.
    """
    # ── Generic/blocklist names — trigger "list all" mode ────────
    GENERIC_NAME_BLOCKLIST = {
        "file", "a", "the", "my", "this", "that", "it",
        "pdf", "ppt", "doc", "docx", "pptx", "xls", "xlsx",
        "mp4", "mp3", "zip", "txt", "jpg", "png", "csv",
        "document", "presentation", "spreadsheet", "image",
    }

    try:
        real_folder = resolve_safe_path(folder)
        expanded = str(real_folder)
        print(f"[*] find_file: resolved '{folder}' → '{expanded}'")

        if not os.path.isdir(expanded):
            return {
                "success": False, "found": False, "matches": [], "count": 0,
                "first_match_path": None,
                "error_type": "folder_not_found",
                "message": f"Folder not found: '{folder}'. Use folder='~/Downloads' not a full path with username.",
                "suggestion": "Retry with folder='~/Downloads'",
            }

        # ── Determine search mode ────────────────────────────────
        clean_name = (partial_name or "").strip()
        is_generic = not clean_name or clean_name.lower() in GENERIC_NAME_BLOCKLIST
        partial_lower = "" if is_generic else clean_name.lower()
        ext_lower = extension.lower().lstrip(".") if extension else None

        matches = []
        for root, _dirs, files in os.walk(expanded):
            # Skip hidden directories
            if any(part.startswith(".") for part in root.split(os.sep) if part):
                continue
            for fname in files:
                if fname.startswith("."):
                    continue
                name_lower = fname.lower()

                # In generic mode: only filter by extension
                if is_generic:
                    if ext_lower and not name_lower.endswith("." + ext_lower):
                        continue
                else:
                    # Normal mode: match partial name
                    if partial_lower not in name_lower:
                        continue
                    if ext_lower and not name_lower.endswith("." + ext_lower):
                        continue

                full_path = os.path.join(root, fname)
                try:
                    size_bytes = os.path.getsize(full_path)
                except OSError:
                    size_bytes = 0
                matches.append({
                    "name": fname,
                    "path": full_path,
                    "size_mb": round(size_bytes / (1024 * 1024), 2),
                })
                if len(matches) >= 50:
                    break
            if len(matches) >= 50:
                break

        # Sort by relevance: exact name match first, then by size descending
        if not is_generic and partial_lower:
            matches.sort(key=lambda m: (
                0 if partial_lower == m["name"].lower().rsplit(".", 1)[0] else 1,
                -m["size_mb"]
            ))
        else:
            # In generic mode, sort newest-first by modification time
            matches.sort(key=lambda m: os.path.getmtime(m["path"]) if os.path.exists(m["path"]) else 0, reverse=True)

        top = matches[:5]
        first_path = top[0]["path"] if top else None

        if is_generic:
            ext_label = f".{ext_lower}" if ext_lower else "all types"
            if top:
                names = [m["name"] for m in top]
                msg = (f"No specific filename given. Found {len(matches)} file(s) "
                       f"({ext_label}) in {folder}: {', '.join(names)}. Which one?")
            else:
                msg = f"No files of type {ext_label} found in {folder}."
        else:
            if top:
                msg = (f"Found {len(matches)} file(s) matching '{partial_name}'"
                       + (f" with extension .{ext_lower}" if ext_lower else "")
                       + f" in {folder}.")
            else:
                msg = f"No files matching '{partial_name}' found in {folder}."

        return {
            "success": True,
            "found": len(top) > 0,
            "matches": top,
            "count": len(matches),
            "first_match_path": first_path,
            "message": msg,
        }
    except Exception as e:
        return {
            "success": False, "found": False, "matches": [], "count": 0,
            "first_match_path": None,
            "error_type": "exception",
            "message": f"Error searching files: {e}",
        }


def open_file(file_path: str) -> dict:
    """
    Opens any file at the given absolute path using macOS `open` command
    (which uses the default application for that file type).
    Uses resolve_safe_path() to fix hallucinated usernames.
    Returns: {"success": True/False, "message": str}
    """
    try:
        resolved = resolve_safe_path(file_path)
        expanded = str(resolved)
        print(f"[*] open_file: resolved '{file_path}' → '{expanded}'")

        if not os.path.exists(expanded):
            return {"success": False,
                    "message": f"File not found: '{file_path}'. Resolved to '{expanded}'."}

        result = subprocess.run(
            ["open", expanded],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            fname = os.path.basename(expanded)
            return {"success": True,
                    "message": f"Opened '{fname}' with its default application."}
        else:
            return {"success": False,
                    "message": f"Failed to open '{file_path}': {result.stderr.strip()}"}
    except subprocess.TimeoutExpired:
        return {"success": False,
                "message": f"Timed out opening '{file_path}'."}
    except Exception as e:
        return {"success": False,
                "message": f"Error opening file: {e}"}


# ---------------------------------------------------------------------------

def send_mac_notification(title: str, text: str) -> None:
    """Triggers a native macOS notification with a sound."""
    # Text is passed via argv so quotes/backslashes can't break the script.
    script = (
        'on run argv\n'
        '  display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"\n'
        'end run'
    )
    subprocess.run(["osascript", "-e", script, str(title), str(text)],
                   capture_output=True, timeout=10)


# ---------------------------------------------------------------------------
# File-watcher daemon (Observer pattern)
# ---------------------------------------------------------------------------

def summarize_with_ollama(file_path: str, content: str) -> None:
    """Sends file content to the local LLM and shows a notification.
    (Name kept for backward compatibility; uses MLX first, Ollama as fallback.)"""
    print(f"[*] DEMASCAS is reading: {file_path}...")

    prompt = (
        "You are DEMASCAS, a highly efficient system AI. "
        "Summarize the following text in exactly 2 short, concise bullet points. "
        f"Text to summarize:\n\n{content}"
    )

    try:
        summary = _llm_complete(prompt, max_tokens=200)

        print("\n[+] Summary Generated:\n" + summary + "\n")

        short_summary = summary.replace('"', "'")[:200] + "..."
        send_mac_notification("DEMASCAS: File Summarized", short_summary)

    except Exception as e:
        print(f"[-] Error connecting to the local LLM: {e}")


def start_file_observer() -> None:
    """
    Starts a watchdog observer that monitors WATCH_DIRECTORY for new
    .txt/.md files and auto-summarizes them.
    """
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            file_path = event.src_path
            if file_path.endswith((".txt", ".md")):
                try:
                    time.sleep(1)  # Wait for the file to finish writing
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    if content.strip():
                        summarize_with_ollama(file_path, content)
                    else:
                        print(f"[*] Ignored empty file: {file_path}")
                except Exception as e:
                    print(f"[-] Error reading file: {e}")

    os.makedirs(WATCH_DIRECTORY, exist_ok=True)

    observer = Observer()
    observer.schedule(_Handler(), WATCH_DIRECTORY, recursive=False)

    print(f"[*] DEMASCAS Observer Active.")
    print(f"[*] Watching for new files in: {WATCH_DIRECTORY}")
    print("[*] Press Ctrl+C to stop.")

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\n[*] DEMASCAS Observer shutting down.")
    observer.join()
