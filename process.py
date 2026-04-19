#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import frontmatter
from dotenv import load_dotenv
from notion_client import Client
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

load_dotenv()

INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
MAPPINGS_DIR = OUTPUT_DIR / ".mappings"

notion = Client(auth=os.environ["NOTION_TOKEN"])
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
INBOX_ID = os.environ["NOTION_INBOX_ID"]


# ── Notion helpers ─────────────────────────────────────────────────────────────

_INLINE_RE = re.compile(
    r'`([^`\n]+)`'
    r'|\*\*([^*\n]+)\*\*'
    r'|\*([^*\n]+)\*'
    r'|_([^_\n]+)_'
    r'|\[([^\]]+)\]\(([^)]+)\)'
)

def parse_inline(text: str) -> list:
    result = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            result.append({"type": "text", "text": {"content": text[last:m.start()]}})
        g = m.groups()
        if g[0] is not None:
            result.append({"type": "text", "text": {"content": g[0]}, "annotations": {"code": True}})
        elif g[1] is not None:
            result.append({"type": "text", "text": {"content": g[1]}, "annotations": {"bold": True}})
        elif g[2] is not None:
            result.append({"type": "text", "text": {"content": g[2]}, "annotations": {"italic": True}})
        elif g[3] is not None:
            result.append({"type": "text", "text": {"content": g[3]}, "annotations": {"italic": True}})
        elif g[4] is not None:
            result.append({"type": "text", "text": {"content": g[4], "link": {"url": g[5]}}})
        last = m.end()
    if last < len(text):
        result.append({"type": "text", "text": {"content": text[last:]}})
    return result or [{"type": "text", "text": {"content": text}}]


def _table_block(lines: list) -> dict | None:
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if not cells or all(re.match(r'^[-: ]+$', c) for c in cells):
            continue
        rows.append(cells)
    if not rows:
        return None
    width = max(len(r) for r in rows)
    return {
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": [
                {
                    "type": "table_row",
                    "table_row": {
                        "cells": [[{"type": "text", "text": {"content": c}}]
                                  for c in (r + [""] * (width - len(r)))]
                    },
                }
                for r in rows
            ],
        },
    }


def markdown_to_notion_blocks(content: str) -> list:
    blocks = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines, i = [], i + 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({
                "type": "code",
                "code": {
                    "language": lang or "plain text",
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines)}}],
                },
            })

        elif stripped.startswith("### "):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": parse_inline(stripped[4:])}})
        elif stripped.startswith("## "):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": parse_inline(stripped[3:])}})
        elif stripped.startswith("# "):
            blocks.append({"type": "heading_1", "heading_1": {"rich_text": parse_inline(stripped[2:])}})

        elif "|" in stripped and stripped.startswith("|"):
            tbl_lines = []
            while i < len(lines) and "|" in lines[i]:
                tbl_lines.append(lines[i])
                i += 1
            block = _table_block(tbl_lines)
            if block:
                blocks.append(block)
            continue

        elif stripped.startswith("- ") or stripped.startswith("* "):
            text = stripped[2:]
            i += 1
            children = []
            while i < len(lines):
                nl = lines[i]
                ns = nl.lstrip()
                ni = len(nl) - len(ns)
                if ns and ni > indent and (ns.startswith("- ") or ns.startswith("* ")):
                    children.append({
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": parse_inline(ns[2:])},
                    })
                    i += 1
                elif ns and ni > indent:
                    i += 1
                else:
                    break
            blk = {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": parse_inline(text)}}
            if children:
                blk["bulleted_list_item"]["children"] = children
            blocks.append(blk)
            continue

        elif re.match(r'^\d+\.\s', stripped):
            text = re.sub(r'^\d+\.\s+', '', stripped)
            blocks.append({"type": "numbered_list_item", "numbered_list_item": {"rich_text": parse_inline(text)}})

        elif stripped in ("---", "***", "___"):
            blocks.append({"type": "divider", "divider": {}})

        elif not stripped:
            pass

        else:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": parse_inline(stripped)}})

        i += 1

    return blocks


def find_page_in_db(title: str) -> str | None:
    results = notion.databases.query(
        database_id=DATABASE_ID,
        filter={"property": "title", "title": {"equals": title}},
    )
    if results["results"]:
        return results["results"][0]["id"]
    return None


def find_page_in_inbox(title: str) -> str | None:
    children = notion.blocks.children.list(block_id=INBOX_ID)
    for block in children["results"]:
        if block["type"] == "child_page" and block["child_page"]["title"] == title:
            return block["id"]
    return None


def clear_page_blocks(page_id: str):
    response = notion.blocks.children.list(block_id=page_id)
    for block in response["results"]:
        notion.blocks.delete(block_id=block["id"])


def append_blocks(page_id: str, blocks: list):
    for i in range(0, len(blocks), 100):
        notion.blocks.children.append(block_id=page_id, children=blocks[i : i + 100])


def load_mapping(stem: str) -> dict | None:
    path = MAPPINGS_DIR / f"{stem}.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _get_notion_title(page_id: str) -> str | None:
    try:
        page = notion.pages.retrieve(page_id=page_id)
        for prop in page["properties"].values():
            if prop["type"] == "title" and prop["title"]:
                return prop["title"][0]["plain_text"]
    except Exception:
        pass
    return None


def pull_titles():
    """Notion에서 제목 변경사항을 로컬 파일로 가져옴."""
    if not MAPPINGS_DIR.exists():
        print("No mappings found.")
        return
    changed = 0
    for mapping_path in sorted(MAPPINGS_DIR.glob("*.json")):
        with open(mapping_path, encoding="utf-8") as f:
            mapping = json.load(f)
        local_file = INPUT_DIR / mapping["source"]
        if not local_file.exists():
            continue
        notion_title = _get_notion_title(mapping["notion_id"])
        if not notion_title or notion_title == mapping["title"]:
            continue
        print(f"  '{mapping['title']}' → '{notion_title}' ({mapping['source']})")
        post = frontmatter.load(local_file)
        post.metadata["title"] = notion_title
        with open(local_file, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))
        mapping["title"] = notion_title
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        changed += 1
    print(f"Pull done. {changed} file(s) updated.")


def upsert_to_notion(title: str, content: str, project: str | None, stem: str) -> str:
    blocks = markdown_to_notion_blocks(content)

    # Use stored page ID if available (survives Notion-side renames)
    mapping = load_mapping(stem)
    if mapping:
        page_id = mapping["notion_id"]
        print(f"  Updating by ID: {title}")
        clear_page_blocks(page_id)
        append_blocks(page_id, blocks)
        # Sync title back in case frontmatter title changed
        notion.pages.update(
            page_id=page_id,
            properties={"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        )
    elif project:
        page_id = find_page_in_db(title)
        if page_id:
            print(f"  Updating Projects page: {title}")
            clear_page_blocks(page_id)
            append_blocks(page_id, blocks)
        else:
            print(f"  Creating Projects page: {title}")
            page = notion.pages.create(
                parent={"database_id": DATABASE_ID},
                properties={"title": {"title": [{"type": "text", "text": {"content": title}}]}},
                children=blocks[:100],
            )
            append_blocks(page["id"], blocks[100:])
            page_id = page["id"]
    else:
        page_id = find_page_in_inbox(title)
        if page_id:
            print(f"  Updating Inbox page: {title}")
            clear_page_blocks(page_id)
            append_blocks(page_id, blocks)
        else:
            print(f"  Creating Inbox page: {title}")
            page = notion.pages.create(
                parent={"page_id": INBOX_ID},
                properties={"title": {"title": [{"type": "text", "text": {"content": title}}]}},
                children=blocks[:100],
            )
            append_blocks(page["id"], blocks[100:])
            page_id = page["id"]

    url = f"https://notion.so/{page_id.replace('-', '')}"
    print(f"  Done → {url}")
    return page_id


# ── Core pipeline ──────────────────────────────────────────────────────────────

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

    # 1. Save to output/
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / filepath.name
    shutil.copy2(filepath, out_path)
    print(f"  Saved: output/{filepath.name}")

    # 2. Upload to Notion
    page_id = upsert_to_notion(title, post.content, project, filepath.stem)

    # 3. Save mapping record
    MAPPINGS_DIR.mkdir(exist_ok=True)
    mapping = {"source": filepath.name, "title": title, "notion_id": page_id}
    mapping_path = MAPPINGS_DIR / f"{filepath.stem}.json"
    with open(mapping_path, "w", encoding="utf-8") as f:
        import json
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"  Mapping saved: {mapping_path}")


# ── File watcher ───────────────────────────────────────────────────────────────

class InputWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            process_file(Path(event.src_path))

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            process_file(Path(event.src_path))


PULL_INTERVAL = 300  # 5분마다 Notion 제목 변경 확인


def _pull_loop(stop_event: threading.Event):
    while not stop_event.wait(PULL_INTERVAL):
        print("\n[Auto-pull] Checking Notion title changes...")
        pull_titles()


def watch():
    INPUT_DIR.mkdir(exist_ok=True)
    observer = Observer()
    observer.schedule(InputWatcher(), str(INPUT_DIR), recursive=False)
    observer.start()

    stop_event = threading.Event()
    puller = threading.Thread(target=_pull_loop, args=(stop_event,), daemon=True)
    puller.start()

    print(f"Watching {INPUT_DIR}/ ... (auto-pull every {PULL_INTERVAL//60}min, Ctrl+C to stop)")
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
        pull_titles()
    elif args.watch:
        watch()
    elif args.file:
        process_file(Path(args.file))
    else:
        for f in sorted(INPUT_DIR.glob("*.md")):
            process_file(f)
