"""
DEMASCAS — actuators/file_system.py
The Librarian: Local RAG capabilities — PDF reading, file searching,
and the watchdog-based file observer for automated document processing.
"""

import glob
import os
import subprocess
import time

import ollama
import PyPDF2

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WATCH_DIRECTORY = os.path.expanduser("~/Desktop/DEMASCAS_Inbox")
OLLAMA_MODEL = "qwen2.5:3b"


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

def send_mac_notification(title: str, text: str) -> None:
    """Triggers a native macOS notification with a sound."""
    script = f'display notification "{text}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script])


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
