#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import time
from pathlib import Path

import anthropic
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
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
INBOX_ID = os.environ["NOTION_INBOX_ID"]


# ── Claude: raw content → split sections ──────────────────────────────────────

def split_content(raw: str) -> list[dict]:
    """Ask Claude to split raw notes into logically separate sections."""
    with claude.messages.stream(
        model="claude-haiku-4-5",
        max_tokens=8000,
        system=(
            "당신은 노트 정리 전문가입니다. "
            "사용자가 막 적어둔 원본 노트를 받아서 주제별로 분리해 정리해주세요. "
            "반드시 아래 JSON 배열 형식으로만 답하세요 (다른 설명 없이):\n"
            '[{"title": "제목", "content": "정리된 내용"}, ...]'
        ),
        messages=[{"role": "user", "content": raw}],
    ) as stream:
        result = stream.get_final_message()

    text = next(b.text for b in result.content if b.type == "text")
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


# ── Notion helpers ─────────────────────────────────────────────────────────────

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


def upsert_to_notion(title: str, content: str, project: str | None) -> str:
    blocks = markdown_to_notion_blocks(content)

    if project:
        page_id = find_page_in_db(title)
        if page_id:
            print(f"    Updating Projects page: {title}")
            clear_page_blocks(page_id)
            append_blocks(page_id, blocks)
        else:
            print(f"    Creating Projects page: {title}")
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
            print(f"    Updating Inbox page: {title}")
            clear_page_blocks(page_id)
            append_blocks(page_id, blocks)
        else:
            print(f"    Creating Inbox page: {title}")
            page = notion.pages.create(
                parent={"page_id": INBOX_ID},
                properties={"title": {"title": [{"type": "text", "text": {"content": title}}]}},
                children=blocks[:100],
            )
            append_blocks(page["id"], blocks[100:])
            page_id = page["id"]

    url = f"https://notion.so/{page_id.replace('-', '')}"
    print(f"    Done → {url}")
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

    project = post.metadata.get("project")
    print(f"Processing: {filepath.name} {'→ Projects' if project else '→ Inbox'}")

    # 1. Claude splits raw content into sections
    print("  Splitting content with Claude...")
    try:
        sections = split_content(post.content)
    except Exception as e:
        print(f"  Split error: {e}")
        return

    print(f"  → {len(sections)} section(s) found")

    # 2. Save each section to output/ and upload to Notion
    OUTPUT_DIR.mkdir(exist_ok=True)
    MAPPINGS_DIR.mkdir(exist_ok=True)

    mapping = {"source": filepath.name, "sections": []}

    for section in sections:
        title = section["title"]
        content = section["content"]
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()
        out_path = OUTPUT_DIR / f"{safe_name}.md"

        # Write output file with frontmatter
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"---\ntitle: {title}\nsource: {filepath.name}\n---\n\n{content}\n")
        print(f"  Saved: output/{out_path.name}")

        # Upload to Notion
        page_id = upsert_to_notion(title, content, project)
        mapping["sections"].append({"title": title, "file": out_path.name, "notion_id": page_id})

    # 3. Save mapping record
    mapping_path = MAPPINGS_DIR / f"{filepath.stem}.json"
    with open(mapping_path, "w", encoding="utf-8") as f:
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


# ── Entrypoint ─────────────────────────────────────────────────────────────────

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
