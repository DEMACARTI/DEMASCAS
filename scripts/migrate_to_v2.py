#!/usr/bin/env python3
"""
DEMASCAS v2 Migration Guide
============================

This script helps migrate from v1 agents to v2 optimized agents.

Run this ONCE to:
1. Backup old agent files
2. Apply memory config changes
3. Setup optimization scheduler
4. Validate system is ready for v2

Usage:
    python3 scripts/migrate_to_v2.py
    python3 scripts/migrate_to_v2.py --dry-run  # Preview changes
"""

import argparse
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

DEMASCAS_ROOT = Path(__file__).parent.parent
BACKUP_DIR = DEMASCAS_ROOT / ".backups" / datetime.now().strftime("%Y%m%d_%H%M%S")


def print_header(text: str):
    """Print section header."""
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def print_step(text: str):
    """Print a step."""
    print(f"\n[•] {text}")


def print_ok(text: str):
    """Print success message."""
    print(f"  ✓ {text}")


def print_warn(text: str):
    """Print warning message."""
    print(f"  ⚠ {text}")


def print_error(text: str):
    """Print error message."""
    print(f"  ✗ {text}")


def backup_files(dry_run: bool = False) -> bool:
    """Backup v1 agent files before migration."""
    print_step("Backing up v1 agent files...")
    
    files_to_backup = [
        "server/agents/dispatcher.py",
        "server/agents/base_agent.py",
        "core/orchestrator.py",
    ]
    
    if not dry_run:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    
    for file_path in files_to_backup:
        full_path = DEMASCAS_ROOT / file_path
        if full_path.exists():
            rel_path = full_path.relative_to(DEMASCAS_ROOT)
            if dry_run:
                print_ok(f"Would backup {rel_path}")
            else:
                dest = BACKUP_DIR / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(full_path, dest)
                print_ok(f"Backed up {rel_path}")
        else:
            print_warn(f"File not found: {rel_path}")
    
    if not dry_run:
        print_ok(f"Backup directory: {BACKUP_DIR}/")
    
    return True


def check_v2_files(dry_run: bool = False) -> bool:
    """Check that all v2 files exist."""
    print_step("Checking v2 files...")
    
    required_files = [
        "core/agent_orchestrator_v2.py",
        "core/memory_config.py",
        "server/agents/agents_v2.py",
        "scripts/optimize_memory.py",
        "docs/OPTIMIZATION_GUIDE_V2.md",
    ]
    
    all_exist = True
    for file_path in required_files:
        full_path = DEMASCAS_ROOT / file_path
        if full_path.exists():
            print_ok(f"Found {file_path}")
        else:
            print_error(f"Missing {file_path}")
            all_exist = False
    
    return all_exist


def validate_python_env() -> bool:
    """Check Python environment is compatible."""
    print_step("Validating Python environment...")
    
    try:
        import sys
        version = sys.version_info
        
        if version.major < 3 or version.minor < 9:
            print_error(f"Python 3.9+ required (found {version.major}.{version.minor})")
            return False
        
        print_ok(f"Python {version.major}.{version.minor} ✓")
        
        # Check required packages
        packages = ["requests", "chromadb", "flask"]
        for pkg in packages:
            try:
                __import__(pkg)
                print_ok(f"Package '{pkg}' installed")
            except ImportError:
                print_warn(f"Package '{pkg}' not found (may need pip install -r requirements.txt)")
        
        return True
    except Exception as e:
        print_error(f"Environment check failed: {e}")
        return False


def test_migration(dry_run: bool = False) -> bool:
    """Test that v2 components load correctly."""
    print_step("Testing v2 components...")
    
    try:
        # Try importing v2 modules
        print("  Importing agent_orchestrator_v2...", end=" ", flush=True)
        from core.agent_orchestrator_v2 import AgentOrchestrator, ToolRouterV2
        print("✓")
        
        print("  Importing memory_config...", end=" ", flush=True)
        from core.memory_config import get_memory_info, validate_config
        print("✓")
        
        print("  Importing agents_v2...", end=" ", flush=True)
        from server.agents.agents_v2 import create_agents_v2
        print("✓")
        
        # Test components
        print("  Creating ToolRouterV2...", end=" ", flush=True)
        router = ToolRouterV2()
        print("✓")
        
        print("  Creating orchestrator...", end=" ", flush=True)
        orchestrator = AgentOrchestrator()
        print("✓")
        
        print("  Getting memory config...", end=" ", flush=True)
        info = get_memory_info()
        print(f"✓ ({info['episodes']} episodes)")
        
        return True
    except Exception as e:
        print_error(f"Component test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def create_integration_checker(dry_run: bool = False) -> bool:
    """Create a checker script to test integration."""
    print_step("Creating integration checker...")
    
    checker_script = DEMASCAS_ROOT / "scripts" / "check_v2_integration.py"
    
    content = '''#!/usr/bin/env python3
"""Quick v2 integration checker."""

import sys
from pathlib import Path

root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

print("[*] DEMASCAS v2 Integration Checker")
print("=" * 50)

# Test imports
tests = [
    ("Agent Orchestrator", "from core.agent_orchestrator_v2 import AgentOrchestrator"),
    ("Memory Config", "from core.memory_config import validate_config"),
    ("Agents v2", "from server.agents.agents_v2 import create_agents_v2"),
    ("Browser Agent", "from server.agents.agents_v2 import BrowserAgentV2"),
    ("Tool Router", "from core.agent_orchestrator_v2 import ToolRouterV2"),
]

passed = 0
for name, import_stmt in tests:
    try:
        exec(import_stmt)
        print(f"  ✓ {name}")
        passed += 1
    except Exception as e:
        print(f"  ✗ {name}: {e}")

print()
print(f"Result: {passed}/{len(tests)} components loaded")

if passed == len(tests):
    print("[✓] v2 Integration OK")
    sys.exit(0)
else:
    print("[!] Integration check failed")
    sys.exit(1)
'''
    
    if not dry_run:
        with open(checker_script, 'w') as f:
            f.write(content)
        checker_script.chmod(0o755)
        print_ok(f"Created {checker_script.relative_to(DEMASCAS_ROOT)}")
    else:
        print_ok("Would create integration checker script")
    
    return True


def show_next_steps():
    """Show what user needs to do next."""
    print_header("NEXT STEPS")
    
    print("""
1. TEST V2 COMPONENTS
   python3 scripts/check_v2_integration.py

2. RUN MEMORY OPTIMIZATION
   python3 scripts/optimize_memory.py --dry-run      # Preview
   python3 scripts/optimize_memory.py --clean-all    # Full cleanup

3. UPDATE TOOL_SERVER
   In server/tool_server.py, change:
   
   FROM:
     from server.agents.dispatcher import dispatch_to_agent
   
   TO:
     from core.agent_orchestrator_v2 import AgentOrchestrator
     from server.agents.agents_v2 import create_agents_v2
     
     orchestrator = AgentOrchestrator()
     orchestrator.agents = create_agents_v2(orchestrator.router)

4. VERIFY MEMORY CONFIG
   Edit core/memory_config.py for your hardware:
   - 4GB RAM: Reduce all values by 50%
   - 8GB RAM: Current defaults are good
   - 16GB+ RAM: Can increase episode cache by 2x

5. TEST WITH SAMPLE QUERY
   Say: "Damascus, what is the weather today?"
   Monitor memory: python3 scripts/optimize.sh status

6. SCHEDULE OPTIMIZATION
   For daily cleanup, add to crontab:
   0 3 * * * cd /path/to/DEMASCAS && python3 scripts/optimize_memory.py

7. MONITOR PERFORMANCE
   Watch system stability for 24 hours
   Adjust memory_config.py values if needed

TROUBLESHOOTING
  - Still using lots of RAM?
    → Run: python3 scripts/optimize_memory.py --clean-all
    → Check: ~/.demascas/memory/ directory size
  
  - Agents not routing correctly?
    → Check: python3 scripts/check_v2_integration.py
    → View: core/agent_orchestrator_v2.py KEYWORD_ROUTES
  
  - Need to rollback?
    → Restore from: {backup_dir}
    → Revert tool_server.py changes
    """)


def main():
    """Main migration workflow."""
    parser = argparse.ArgumentParser(
        description="Migrate DEMASCAS to v2 optimized agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/migrate_to_v2.py              # Full migration
  python3 scripts/migrate_to_v2.py --dry-run    # Preview changes
        """,
    )
    parser.add_argument("--dry-run", action="store_true",
                       help="Preview changes without modifying files")
    args = parser.parse_args()

    print_header("DEMASCAS v2 MIGRATION ASSISTANT")
    
    if args.dry_run:
        print("\n[*] Running in DRY-RUN mode (no actual changes)")
    
    # Run migration steps
    steps = [
        ("Backup v1 files", lambda dr=args.dry_run: backup_files(dr)),
        ("Check v2 files", lambda dr=args.dry_run: check_v2_files(dr)),
        ("Validate environment", validate_python_env),
        ("Test v2 components", test_migration),
        ("Create checker script", lambda dr=args.dry_run: create_integration_checker(dr)),
    ]
    
    success_count = 0
    for step_name, step_func in steps:
        try:
            if step_func():
                success_count += 1
            else:
                print_error(f"{step_name} check failed")
        except Exception as e:
            print_error(f"{step_name} error: {e}")

    # Summary
    print_header("MIGRATION SUMMARY")
    print(f"\nCompleted: {success_count}/{len(steps)} checks")
    
    if success_count == len(steps):
        print("[✓] All checks passed! Ready for v2")
    else:
        print("[!] Some checks failed. Review above for details.")
        return 1
    
    # Show next steps
    show_next_steps()
    
    return 0


if __name__ == "__main__":
    exit(main())
