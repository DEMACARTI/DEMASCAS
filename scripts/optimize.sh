#!/bin/bash
# DEMASCAS Optimization Service
# Automatically manages memory usage
# 
# Usage:
#   ./optimize.sh start   — Start background optimization
#   ./optimize.sh stop    — Stop background optimization  
#   ./optimize.sh status  — Show memory stats
#   ./optimize.sh full    — Run full cleanup

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DEMASCAS_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
PYTHON_SCRIPT="$SCRIPT_DIR/optimize_memory.py"
PID_FILE="$DEMASCAS_DIR/.optimize.pid"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo_status() {
    echo -e "${GREEN}[✓]${NC} $1"
}

echo_error() {
    echo -e "${RED}[!]${NC} $1"
}

echo_info() {
    echo -e "${YELLOW}[*]${NC} $1"
}

# Show memory statistics
show_stats() {
    echo_info "Checking DEMASCAS memory footprint..."
    python3 << 'PYTHON'
from pathlib import Path
import os

demascas_dir = Path.home() / ".demascas"
if not demascas_dir.exists():
    print("  [*] No DEMASCAS data found yet")
    exit(0)

# Calculate sizes
memory_dir = demascas_dir / "memory"
archive_dir = demascas_dir / "memory_archive"

def get_size(path):
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file())

memory_size = get_size(memory_dir)
archive_size = get_size(archive_dir)
profile_size = (demascas_dir / "user_profile.json").stat().st_size if (demascas_dir / "user_profile.json").exists() else 0
total = memory_size + archive_size + profile_size

def fmt(b):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

print(f"  Memory DB:  {fmt(memory_size)}")
print(f"  Archives:   {fmt(archive_size)}")
print(f"  Profile:    {fmt(profile_size)}")
print(f"  Total:      {fmt(total)}")

if total > 500 * 1024 * 1024:
    print()
    print("  [!] WARNING: Footprint > 500 MB")
    print("      Run: $0 full")
PYTHON
}

# Start background optimization service
start_service() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo_error "Service already running (PID: $PID)"
            exit 1
        fi
    fi

    echo_info "Starting background optimization service..."
    
    nohup python3 << 'PYTHON' > /dev/null 2>&1 &
import subprocess
import time
from pathlib import Path
import os
import signal

PID_FILE = Path.home() / ".demascas" / ".optimize.pid"
SCRIPT = "$(resolve_path "$PYTHON_SCRIPT")"

def signal_handler(sig, frame):
    PID_FILE.unlink(missing_ok=True)
    exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Write PID
PID_FILE.parent.mkdir(parents=True, exist_ok=True)
PID_FILE.write_text(str(os.getpid()))

# Run optimization every 6 hours
try:
    while True:
        time.sleep(21600)  # 6 hours
        subprocess.run([
            "python3", "$PYTHON_SCRIPT"
        ], capture_output=True, timeout=300)
except Exception as e:
    pass
finally:
    PID_FILE.unlink(missing_ok=True)
PYTHON
    
    # Store the PID
    sleep 0.1
    BG_PID=$!
    if [ $BG_PID -ne 0 ]; then
        echo $BG_PID > "$PID_FILE"
        echo_status "Service started (PID: $BG_PID)"
        echo_info "Optimization will run every 6 hours"
    fi
}

# Stop background service
stop_service() {
    if [ ! -f "$PID_FILE" ]; then
        echo_error "Service not running"
        exit 1
    fi

    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo_info "Stopping service (PID: $PID)..."
        kill $PID 2>/dev/null || true
        sleep 1
        rm -f "$PID_FILE"
        echo_status "Service stopped"
    else
        rm -f "$PID_FILE"
        echo_error "Service not running (stale PID file cleaned up)"
    fi
}

# Run full cleanup
run_full_cleanup() {
    echo_info "Running full memory cleanup..."
    python3 "$PYTHON_SCRIPT" --clean-all
    echo_status "Cleanup complete"
}

# Main
case "${1:-status}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    status)
        show_stats
        ;;
    full)
        run_full_cleanup
        ;;
    *)
        echo "DEMASCAS Memory Optimizer"
        echo ""
        echo "Usage: $0 {start|stop|status|full}"
        echo ""
        echo "  start    Start background optimization (every 6 hours)"
        echo "  stop     Stop background service"
        echo "  status   Show current memory usage"
        echo "  full     Run complete memory cleanup now"
        echo ""
        show_stats
        ;;
esac
