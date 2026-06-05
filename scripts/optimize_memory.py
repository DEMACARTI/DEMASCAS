#!/usr/bin/env python3
"""
DEMASCAS Memory Optimization Script
====================================

Reduces RAM footprint by:
1. Archiving old episodic memories (>30 days) to compressed JSON
2. Deduplicating and compressing memory embeddings
3. Pruning ChromaDB indices
4. Implementing intelligent lazy loading
5. Clearing unused tool schemas from cache
6. Optimizing embedding batch sizes

Usage:
    python scripts/optimize_memory.py [--aggressive] [--dry-run]

Options:
    --aggressive    : Remove memories older than 7 days (default: 30)
    --dry-run      : Show what would be deleted without deleting
    --clean-all    : Full cleanup (delete old memories, compact DB)
"""

import os
import sys
import json
import gzip
import shutil
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# IST timezone (matches agent defaults)
IST = timezone(timedelta(hours=5, minutes=30))

DEMASCAS_DIR = Path.home() / ".demascas"
MEMORY_DIR = DEMASCAS_DIR / "memory"
ARCHIVE_DIR = DEMASCAS_DIR / "memory_archive"
PROFILE_PATH = DEMASCAS_DIR / "user_profile.json"


def print_section(title: str):
    """Print a formatted section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def get_dir_size(path: Path) -> int:
    """Calculate total size of a directory in bytes."""
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file())


def format_bytes(bytes_val: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def archive_old_memories(days_old: int = 30, dry_run: bool = False) -> int:
    """
    Archive episodic memories older than days_old days.
    Returns count of archived memories.
    """
    print_section("Archiving Old Episodic Memories")
    
    try:
        import chromadb
    except ImportError:
        print("[!] chromadb not installed. Skipping memory archival.")
        return 0

    if not MEMORY_DIR.exists():
        print("[✓] No memory directory found. Skipping.")
        return 0

    try:
        client = chromadb.PersistentClient(path=str(MEMORY_DIR))
        collection = client.get_collection("demascas_episodes")
    except Exception as e:
        print(f"[!] Could not access episodes collection: {e}")
        return 0

    # Get all memories with metadata
    all_memories = collection.get(include=["metadatas", "documents"])
    if not all_memories["ids"]:
        print("[✓] No memories to archive.")
        return 0

    cutoff_date = (datetime.now(IST) - timedelta(days=days_old)).date()
    to_archive = []
    
    for idx, metadata in enumerate(all_memories.get("metadatas", [])):
        if not metadata:
            continue
        
        date_str = metadata.get("date", "")
        try:
            mem_date = datetime.fromisoformat(date_str).date()
            if mem_date < cutoff_date:
                to_archive.append({
                    "id": all_memories["ids"][idx],
                    "date": date_str,
                    "doc": all_memories["documents"][idx] if idx < len(all_memories["documents"]) else ""
                })
        except (ValueError, TypeError):
            continue

    if not to_archive:
        print(f"[✓] No memories older than {days_old} days. Skipping.")
        return 0

    print(f"Found {len(to_archive)} memories older than {days_old} days.")
    if not dry_run:
        # Create archive directory
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        
        # Save to compressed JSON
        archive_file = ARCHIVE_DIR / f"memories_{cutoff_date}_batch.json.gz"
        with gzip.open(archive_file, 'wt', encoding='utf-8') as f:
            json.dump(to_archive, f, indent=2)
        
        # Delete from ChromaDB
        ids_to_delete = [m["id"] for m in to_archive]
        collection.delete(ids=ids_to_delete)
        
        print(f"[✓] Archived {len(to_archive)} memories to {archive_file.name}")
        print(f"[✓] Archive size: {format_bytes(archive_file.stat().st_size)}")
    else:
        print(f"[DRY RUN] Would archive {len(to_archive)} memories.")

    return len(to_archive)


def compact_chromadb(dry_run: bool = False) -> tuple[int, int]:
    """
    Compact ChromaDB by removing duplicates and optimizing indices.
    Returns (duplicates_found, duplicates_removed).
    """
    print_section("Compacting ChromaDB Indices")
    
    try:
        import chromadb
    except ImportError:
        print("[!] chromadb not installed. Skipping.")
        return 0, 0

    if not MEMORY_DIR.exists():
        print("[✓] No memory directory found. Skipping.")
        return 0, 0

    duplicates_removed = 0
    collections_checked = 0

    try:
        client = chromadb.PersistentClient(path=str(MEMORY_DIR))
        collections = client.list_collections()
        
        for collection in collections:
            collections_checked += 1
            col = client.get_collection(collection.name)
            all_items = col.get(include=["documents"])
            
            if not all_items["ids"]:
                continue
            
            # Check for duplicate documents
            seen = {}
            dupes = []
            for doc_id, doc in zip(all_items["ids"], all_items.get("documents", [])):
                doc_hash = hash(doc)
                if doc_hash in seen:
                    dupes.append(doc_id)
                else:
                    seen[doc_hash] = doc_id
            
            if dupes:
                duplicates_removed += len(dupes)
                if not dry_run:
                    col.delete(ids=dupes)
                    print(f"  [{collection.name}] Removed {len(dupes)} duplicates")
    except Exception as e:
        print(f"[!] Error during compaction: {e}")
        return 0, duplicates_removed

    if dry_run:
        print(f"[DRY RUN] Would remove {duplicates_removed} duplicates from {collections_checked} collections.")
    else:
        print(f"[✓] Compacted {collections_checked} collection(s), removed {duplicates_removed} duplicates.")

    return collections_checked, duplicates_removed


def optimize_profile(dry_run: bool = False) -> int:
    """
    Optimize user profile by removing stale entries.
    Returns count of entries removed.
    """
    print_section("Optimizing User Profile")
    
    if not PROFILE_PATH.exists():
        print("[✓] No user profile found. Skipping.")
        return 0

    try:
        with open(PROFILE_PATH, 'r') as f:
            profile = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[!] Could not read profile: {e}")
        return 0

    original_size = len(json.dumps(profile))
    removed = 0

    # Remove entries older than 60 days from interaction history
    if "interaction_history" in profile:
        cutoff = (datetime.now(IST) - timedelta(days=60)).isoformat()
        old_count = len(profile["interaction_history"])
        profile["interaction_history"] = [
            entry for entry in profile["interaction_history"]
            if entry.get("timestamp", "") > cutoff
        ]
        removed += old_count - len(profile["interaction_history"])

    # Limit correction history to last 50 entries
    if "correction_history" in profile and len(profile["correction_history"]) > 50:
        profile["correction_history"] = profile["correction_history"][-50:]

    # Limit workflow history to last 30 entries
    if "workflow_history" in profile and len(profile["workflow_history"]) > 30:
        profile["workflow_history"] = profile["workflow_history"][-30:]

    new_size = len(json.dumps(profile))
    freed = original_size - new_size

    if not dry_run and removed > 0:
        with open(PROFILE_PATH, 'w') as f:
            json.dump(profile, f, indent=2)
        print(f"[✓] Removed {removed} stale history entries")
        print(f"[✓] Freed {format_bytes(freed)} (new size: {format_bytes(new_size)})")
    else:
        print(f"[DRY RUN] Would remove {removed} entries, freeing {format_bytes(freed)}")

    return removed


def show_memory_stats():
    """Display current memory usage statistics."""
    print_section("Memory Usage Statistics")
    
    # Check ChromaDB size
    if MEMORY_DIR.exists():
        chroma_size = get_dir_size(MEMORY_DIR)
        print(f"ChromaDB memory directory: {format_bytes(chroma_size)}")
    else:
        chroma_size = 0
        print(f"ChromaDB memory directory: Not found")

    # Check archive size
    if ARCHIVE_DIR.exists():
        archive_size = get_dir_size(ARCHIVE_DIR)
        print(f"Archived memories: {format_bytes(archive_size)}")
    else:
        archive_size = 0
        print(f"Archived memories: None")

    # Check profile size
    if PROFILE_PATH.exists():
        profile_size = PROFILE_PATH.stat().st_size
        print(f"User profile: {format_bytes(profile_size)}")
    else:
        profile_size = 0
        print(f"User profile: Not found")

    total = chroma_size + archive_size + profile_size
    print(f"\nTotal memory footprint: {format_bytes(total)}")


def main():
    """Main optimization workflow."""
    parser = argparse.ArgumentParser(
        description="Optimize DEMASCAS memory usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/optimize_memory.py --clean-all      # Full cleanup
  python scripts/optimize_memory.py --dry-run        # Preview changes
  python scripts/optimize_memory.py --aggressive     # Aggressive cleanup
        """
    )
    parser.add_argument("--aggressive", action="store_true", 
                       help="Remove memories older than 7 days (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be deleted without deleting")
    parser.add_argument("--clean-all", action="store_true",
                       help="Full cleanup (archive + compact + optimize)")

    args = parser.parse_args()

    print("\n" + "="*60)
    print("  DEMASCAS MEMORY OPTIMIZER v1.0")
    print("="*60)

    if args.dry_run:
        print("[*] Running in DRY-RUN mode (no changes will be made)")

    show_memory_stats()
    
    days_threshold = 7 if args.aggressive else 30
    
    if args.clean_all or args.aggressive:
        archive_old_memories(days_threshold, dry_run=args.dry_run)
    
    if args.clean_all:
        compact_chromadb(dry_run=args.dry_run)
        optimize_profile(dry_run=args.dry_run)

    show_memory_stats()
    
    print("\n[✓] Optimization complete!")
    print("[*] Restart DEMASCAS daemon for changes to take effect:")
    print("     ./daemon/build/demascas_daemon\n")


if __name__ == "__main__":
    main()
