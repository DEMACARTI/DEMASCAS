"""
DEMASCAS — core/utils.py
Shared utility functions used across actuators and server modules.
"""

import pathlib
import re


def resolve_safe_path(path_str: str) -> pathlib.Path:
    """
    Safely resolves any path to an absolute path, replacing hallucinated usernames.

    Whisper often transcribes the user's name incorrectly, producing paths like
    /Users/Duk Shradh Hall/Downloads instead of the real home directory.
    This function replaces the entire /Users/<anything>/ prefix with the actual
    home directory, making file paths immune to username hallucination.

    Transformations:
        /Users/<anything>/X  → home/X
        ~/X                  → home/X
        relative path        → returned as-is (pathlib.Path)

    Always returns a real pathlib.Path using the actual current user's home directory.
    """
    if not path_str or not isinstance(path_str, str):
        return pathlib.Path.home()

    path_str = path_str.strip()

    home = pathlib.Path.home()

    # Handle tilde
    if path_str.startswith("~"):
        return pathlib.Path(path_str.replace("~", str(home), 1))

    # Handle /Users/<anything>/... — replace hallucinated username with real home
    if path_str.startswith("/Users/"):
        parts = pathlib.Path(path_str).parts  # ('/', 'Users', '<name>', 'Downloads', ...)
        if len(parts) >= 4:
            # Skip '/', 'Users', '<hallucinated_name>' and rejoin the rest
            return home / pathlib.Path(*parts[3:])
        elif len(parts) == 3:
            # /Users/<name> with nothing after → just return home
            return home

    return pathlib.Path(path_str)
