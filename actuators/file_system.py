"""
DEMASCAS — actuators/file_system.py
The Librarian: Local RAG capabilities — PDF reading, file searching,
and the watchdog-based file observer for automated document processing.
"""

import glob
import os
import subprocess
import sys
import time

import ollama
import PyPDF2

from core.utils import resolve_safe_path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WATCH_DIRECTORY = os.path.expanduser("~/Desktop/DEMASCAS_Inbox")
OLLAMA_MODEL = "gemma4:31b-cloud"


# ---------------------------------------------------------------------------
# PDF summarization
# ---------------------------------------------------------------------------

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

    pages: list[str] = []
    try:
        with open(latest_pdf, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            for i in range(min(3, len(reader.pages))):
                page_text = reader.pages[i].extract_text()
                if page_text:
                    pages.append(page_text)
                    # Stop early if we already have enough text for the LLM
                    if sum(len(p) for p in pages) >= 4000:
                        break
    except Exception as e:
        return f"Failed to read PDF: {e}"

    text_content = "\n".join(pages)
    if not text_content.strip():
        return f"Failed: '{file_name}' appears to be empty or contains only images."

    print("[*] Text extracted. Routing to LLM for summarization...")

    summary_prompt = (
        f"Summarize the following document in 3 concise bullet points:\n\n"
        f"{text_content[:4000]}"
    )

    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[{'role': 'user', 'content': summary_prompt}],
        options={"num_ctx": 4096, "num_predict": 300, "temperature": 0},
    )

    return f"Summary of {file_name}:\n" + response['message']['content']


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
# File creation & deletion (with confirmation enforced by the LLM prompt)
# ---------------------------------------------------------------------------

def create_file(file_path: str, content: str = "") -> str:
    """
    Creates a new file at the given path with optional content.
    Parent directories are created automatically if they don't exist.

    SAFETY: The system prompt instructs the LLM to always ask the user
    for verbal confirmation before calling this tool.

    Args:
        file_path: Absolute or ~-relative path (e.g. '~/Desktop/notes.txt').
        content:   Text to write into the file (default: empty).

    Returns:
        A success/failure message string.
    """
    expanded = os.path.expanduser(file_path)
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
    Deletes a file (or empty directory) at the given path.
    Moves to macOS Trash via Finder AppleScript when possible so the
    user can recover it.  Falls back to os.remove for non-Finder paths.

    SAFETY: The system prompt instructs the LLM to always ask the user
    for verbal confirmation before calling this tool.

    Args:
        file_path: Absolute or ~-relative path (e.g. '~/Desktop/notes.txt').

    Returns:
        A success/failure message string.
    """
    expanded = os.path.expanduser(file_path)
    print(f"[*] DEMASCAS is deleting: {expanded}")

    if not os.path.exists(expanded):
        return f"Failed: '{file_path}' does not exist."

    if sys.platform.startswith('darwin'):
        # macOS: Prefer Trash via Finder (recoverable) over hard delete
        result = subprocess.run(
            ['osascript', '-e',
             f'tell application "Finder" to delete '
             f'(POSIX file "{expanded}" as alias)'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return f"Success: Moved '{file_path}' to Trash."

    # Fallback: hard delete (used on Linux or if Finder fails)
    try:
        if os.path.isfile(expanded):
            os.remove(expanded)
        elif os.path.isdir(expanded) and not os.listdir(expanded):
            os.rmdir(expanded)
        else:
            return (f"Refused: '{file_path}' is a non-empty directory. "
                    "DEMASCAS won't delete directories with contents.")

        return f"Success: Deleted '{file_path}'."
    except PermissionError:
        return f"Failed: Permission denied for '{file_path}'."
    except Exception as e:
        return f"Failed to delete: {e}"


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
    """Triggers a native notification with a sound (macOS only)."""
    if sys.platform.startswith('darwin'):
        script = f'display notification "{text}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script])
    # Linux: notifications not implemented yet (would require notify-send)


# ---------------------------------------------------------------------------
# File-watcher daemon (Observer pattern)
# ---------------------------------------------------------------------------

def summarize_with_ollama(file_path: str, content: str) -> None:
    """Sends file content to the local Ollama LLM and shows a notification."""
    print(f"[*] DEMASCAS is reading: {file_path}...")

    prompt = (
        "You are DEMASCAS, a highly efficient system AI. "
        "Summarize the following text in exactly 2 short, concise bullet points. "
        f"Text to summarize:\n\n{content}"
    )

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{'role': 'user', 'content': prompt}],
        )
        summary = response['message']['content'].strip()

        print("\n[+] Summary Generated:\n" + summary + "\n")

        short_summary = summary.replace('"', "'")[:200] + "..."
        send_mac_notification("DEMASCAS: File Summarized", short_summary)

    except Exception as e:
        print(f"[-] Error connecting to DEMASCAS Engine (Ollama): {e}")


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
