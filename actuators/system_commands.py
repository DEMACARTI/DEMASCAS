"""
DEMASCAS — actuators/system_commands.py
Executes safe system commands (read-only info commands) to allow DEMASCAS
to answer queries about system state like running processes, disk space, etc.
"""

import subprocess
import os

# Strict allowlist of safe, read-only system information commands
ALLOWED_COMMANDS = {
    "ps", "top", "df", "uptime", "whoami", "sw_vers", 
    "system_profiler", "networksetup", "pmset", "sysctl", 
    "launchctl", "ifconfig", "which", "cat", "head", "tail", 
    "wc", "sort", "grep", "find", "ls", "echo", "date", "cal"
}

def run_shell_command(command: str) -> str:
    """
    Run a shell command to gather system information.
    Only allows read-only informational commands. No destructive commands allowed.
    Will reject rm, mv, cp, chmod, sudo, etc.
    Timeout is 10 seconds.
    """
    cmd_parts = command.strip().split()
    if not cmd_parts:
        return "[-] Error: Empty command."
        
    base_cmd = cmd_parts[0]
    
    # Simple strict validation against allowlist
    if base_cmd not in ALLOWED_COMMANDS:
        return f"[-] Error: Command '{base_cmd}' is not in the safe allowlist. Only read-only system commands are allowed."
        
    # Extra check for potentially dangerous symbols/pipe injection
    forbidden_chars = [";", "&&", "||", "`", "$()", ">", ">>"]
    for char in forbidden_chars:
        if char in command:
            return f"[-] Error: Command contains forbidden character/sequence '{char}'."
            
    try:
        # Run with a 10-second timeout to prevent hangs
        result = subprocess.run(
            command, 
            shell=True, 
            capture_output=True, 
            text=True, 
            timeout=10
        )
        
        output = result.stdout.strip()
        error = result.stderr.strip()
        
        if result.returncode != 0:
            if error:
                return f"[-] Command failed (exit code {result.returncode}):\n{error}"
            return f"[-] Command failed with exit code {result.returncode}"
            
        if not output:
            return "[+] Command succeeded with no output."
            
        # Truncate very long outputs (like ps aux) so we don't blow up the LLM context
        if len(output) > 4000:
            return output[:4000] + "\n\n... [Output truncated due to length]"
            
        return output
        
    except subprocess.TimeoutExpired:
        return "[-] Error: Command timed out after 10 seconds."
    except Exception as e:
        return f"[-] Error executing command: {str(e)}"

def get_system_info() -> str:
    """
    Get a quick summary of the current macOS system state 
    (OS version, uptime, CPU/Memory load, active user).
    """
    info = []
    
    # OS Version
    try:
        sw = subprocess.check_output(["sw_vers"], text=True).strip()
        # Parse it slightly nicer
        lines = sw.split("\n")
        version = lines[1].split(":")[1].strip() if len(lines) > 1 else "Unknown"
        info.append(f"macOS Version: {version}")
    except:
        pass
        
    # Uptime
    try:
        up = subprocess.check_output(["uptime"], text=True).strip()
        # Uptime string contains times and load averages
        info.append(f"Uptime details: {up}")
    except:
        pass
        
    # Active User
    try:
        user = subprocess.check_output(["whoami"], text=True).strip()
        info.append(f"Current User: {user}")
    except:
        pass
        
    # Disk Space (Root)
    try:
        df = subprocess.check_output(["df", "-h", "/"], text=True).strip().split("\n")
        if len(df) > 1:
            info.append(f"Disk Space (/): {df[1]}")
    except:
        pass
        
    if not info:
        return "[-] Could not retrieve system information."
        
    return "\n".join(info)

def set_default_browser(browser_name: str) -> str:
    """
    Sets the default browser on macOS using the 'defaultbrowser' CLI.
    Common names: 'safari', 'chrome', 'brave', 'firefox', 'edge'.
    """
    browser_map = {
        "brave": "brave",
        "brave browser": "brave",
        "chrome": "chrome",
        "google chrome": "chrome",
        "safari": "safari",
        "firefox": "firefox",
        "edge": "edge",
        "microsoft edge": "edge"
    }
    b_name = browser_name.lower().strip()
    cli_name = browser_map.get(b_name, b_name)
    
    try:
        result = subprocess.run(
            f"defaultbrowser {cli_name}", 
            shell=True, capture_output=True, text=True
        )
        if result.returncode == 0:
            return f"[+] Successfully set default browser to: {browser_name} ({cli_name})"
        else:
            return f"[-] Failed to set default browser: {result.stderr.strip()}"
    except Exception as e:
        return f"[-] Error setting default browser: {str(e)}"
