#!/usr/bin/env python3
import argparse
import json
import shutil
import threading
import time
from pathlib import Path

import frontmatter
from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from notion_api import pull_titles, upsert_page

load_dotenv()

INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
MAPPINGS_DIR = OUTPUT_DIR / ".mappings"
PULL_INTERVAL = 300  # seconds


# ── Mapping ────────────────────────────────────────────────────────────────────

def load_mapping(stem: str) -> dict | None:
    path = MAPPINGS_DIR / f"{stem}.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_mapping(stem: str, source: str, title: str, page_id: str):
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    mapping = {"source": source, "title": title, "notion_id": page_id}
    with open(MAPPINGS_DIR / f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


# ── Pipeline ───────────────────────────────────────────────────────────────────

def process_file(filepath: Path):
    try:
        post = frontmatter.load(filepath)
    except Exception as e:
        print(f"  Parse error ({filepath.name}): {e}")
        return

    if post.metadata.get("status") != "done":
        return

    title = post.metadata.get("title") or filepath.stem
    project = post.metadata.get("project")
    print(f"Processing: {filepath.name} [{title}] {'→ Projects' if project else '→ Inbox'}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    shutil.copy2(filepath, OUTPUT_DIR / filepath.name)
    print(f"  Saved: output/{filepath.name}")

    mapping = load_mapping(filepath.stem)
    page_id = upsert_page(title, post.content, project, mapping)
    save_mapping(filepath.stem, filepath.name, title, page_id)
    print(f"  Mapping saved.")


# ── Watcher ────────────────────────────────────────────────────────────────────

class InputWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            process_file(Path(event.src_path))

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            process_file(Path(event.src_path))


def _pull_loop(stop_event: threading.Event):
    while not stop_event.wait(PULL_INTERVAL):
        print("\n[Auto-pull] Checking Notion title changes...")
        pull_titles(INPUT_DIR, MAPPINGS_DIR)


def watch():
    INPUT_DIR.mkdir(exist_ok=True)
    observer = Observer()
    observer.schedule(InputWatcher(), str(INPUT_DIR), recursive=False)
    observer.start()

    stop_event = threading.Event()
    threading.Thread(target=_pull_loop, args=(stop_event,), daemon=True).start()

    print(f"Watching {INPUT_DIR}/ ... (auto-pull every {PULL_INTERVAL // 60}min, Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()
        observer.stop()
    observer.join()


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Input → Output → Notion sync")
    parser.add_argument("file", nargs="?", help="Process a specific file once")
    parser.add_argument("--watch", action="store_true", help="Watch input/ for changes")
    parser.add_argument("--pull", action="store_true", help="Pull Notion title changes to local files")
    args = parser.parse_args()

    if args.pull:
        pull_titles(INPUT_DIR, MAPPINGS_DIR)
    elif args.watch:
        watch()
    elif args.file:
        process_file(Path(args.file))
    else:
        for f in sorted(INPUT_DIR.glob("*.md")):
            process_file(f)
