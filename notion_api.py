import json
import os
from pathlib import Path

import frontmatter
from dotenv import load_dotenv
from notion_client import Client

from converter import markdown_to_notion_blocks

load_dotenv()

notion = Client(auth=os.environ["NOTION_TOKEN"])
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
INBOX_ID = os.environ["NOTION_INBOX_ID"]


# ── Page lookup ────────────────────────────────────────────────────────────────

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


def get_notion_title(page_id: str) -> str | None:
    try:
        page = notion.pages.retrieve(page_id=page_id)
        for prop in page["properties"].values():
            if prop["type"] == "title" and prop["title"]:
                return prop["title"][0]["plain_text"]
    except Exception:
        pass
    return None


# ── Block operations ───────────────────────────────────────────────────────────

def clear_page_blocks(page_id: str):
    response = notion.blocks.children.list(block_id=page_id)
    for block in response["results"]:
        notion.blocks.delete(block_id=block["id"])


def append_blocks(page_id: str, blocks: list):
    for i in range(0, len(blocks), 100):
        notion.blocks.children.append(block_id=page_id, children=blocks[i:i + 100])


# ── Upsert ─────────────────────────────────────────────────────────────────────

def upsert_page(title: str, content: str, project: str | None, mapping: dict | None) -> str:
    blocks = markdown_to_notion_blocks(content)

    if mapping:
        page_id = mapping["notion_id"]
        print(f"  Updating by ID: {title}")
        clear_page_blocks(page_id)
        append_blocks(page_id, blocks)
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

    print(f"  Done → https://notion.so/{page_id.replace('-', '')}")
    return page_id


# ── Title pull ─────────────────────────────────────────────────────────────────

def pull_titles(mappings_dir: Path):
    if not mappings_dir.exists():
        print("No mappings found.")
        return
    changed = 0
    for mapping_path in sorted(mappings_dir.glob("*.json")):
        with open(mapping_path, encoding="utf-8") as f:
            mapping = json.load(f)
        notion_title = get_notion_title(mapping["notion_id"])
        if not notion_title or notion_title == mapping["title"]:
            continue
        print(f"  '{mapping['title']}' → '{notion_title}' ({mapping['source']})")
        mapping["title"] = notion_title
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        changed += 1
    print(f"Pull done. {changed} file(s) updated.")
