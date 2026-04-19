import re

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
