#!/usr/bin/env python3
import argparse
import os
import shutil
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

notion = Client(auth=os.environ["NOTION_TOKEN"])
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]


def markdown_to_notion_blocks(content: str) -> list:
    blocks = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            lang = line[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({
                "type": "code",
                "code": {
                    "language": lang or "plain text",
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines)}}],
                },
            })
        elif line.startswith("### "):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": line[4:]}}]}})
        elif line.startswith("## "):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": line[3:]}}]}})
        elif line.startswith("# "):
            blocks.append({"type": "heading_1", "heading_1": {"rich_text": [{"type": "text", "text": {"content": line[2:]}}]}})
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": line[2:]}}]}})
        elif len(line) > 2 and line[0].isdigit() and line[1] == ".":
            blocks.append({"type": "numbered_list_item", "numbered_list_item": {"rich_text": [{"type": "text", "text": {"content": line[3:]}}]}})
        elif line.strip() in ("---", "***", "___"):
            blocks.append({"type": "divider", "divider": {}})
        elif line.strip() == "":
            pass
        else:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]}})

        i += 1

    return blocks


def find_page_by_title(title: str) -> str | None:
    results = notion.databases.query(
        database_id=DATABASE_ID,
        filter={"property": "title", "title": {"equals": title}},
    )
    if results["results"]:
        return results["results"][0]["id"]
    return None


def clear_page_blocks(page_id: str):
    response = notion.blocks.children.list(block_id=page_id)
    for block in response["results"]:
        notion.blocks.delete(block_id=block["id"])


def upsert_to_notion(title: str, content: str):
    blocks = markdown_to_notion_blocks(content)
    chunk_size = 100
    page_id = find_page_by_title(title)

    if page_id:
        print(f"  Updating existing page: {title}")
        clear_page_blocks(page_id)
    else:
        print(f"  Creating new page: {title}")
        page = notion.pages.create(
            parent={"database_id": DATABASE_ID},
            properties={"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            children=blocks[:chunk_size],
        )
        page_id = page["id"]
        blocks = blocks[chunk_size:]

    for i in range(0, len(blocks), chunk_size):
        notion.blocks.children.append(block_id=page_id, children=blocks[i : i + chunk_size])

    print(f"  Done → https://notion.so/{page_id.replace('-', '')}")


def process_file(filepath: Path):
    try:
        post = frontmatter.load(filepath)
    except Exception as e:
        print(f"  Parse error ({filepath.name}): {e}")
        return

    if post.metadata.get("status") != "done":
        return

    title = post.metadata.get("title") or filepath.stem
    print(f"Processing: {filepath.name}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    shutil.copy2(filepath, OUTPUT_DIR / filepath.name)
    print(f"  Saved to output/{filepath.name}")

    upsert_to_notion(title, post.content)


class InputWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            process_file(Path(event.src_path))

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            process_file(Path(event.src_path))


def watch():
    INPUT_DIR.mkdir(exist_ok=True)
    observer = Observer()
    observer.schedule(InputWatcher(), str(INPUT_DIR), recursive=False)
    observer.start()
    print(f"Watching {INPUT_DIR}/ ... (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Input → Output → Notion sync")
    parser.add_argument("file", nargs="?", help="Process a specific file once")
    parser.add_argument("--watch", action="store_true", help="Watch input/ for changes")
    args = parser.parse_args()

    if args.watch:
        watch()
    elif args.file:
        process_file(Path(args.file))
    else:
        for f in sorted(INPUT_DIR.glob("*.md")):
            process_file(f)
