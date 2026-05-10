"""
title: Claude Code
description: Run Claude Code's agent loop from inside OpenWebUI chats via the Claude Agent SDK.
author: Thomas Friedel, Denis Kutuzov (aka R8CEH)
author_url: https://github.com/tfriedel/openwebui-claude-code
funding_url: https://github.com/R8CEH/openwebui-claude-code
version: 0.2.0
license: MIT
requirements: claude-agent-sdk>=0.1.60, anthropic>=0.40.0
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
_DOWNLOAD_EXTENSIONS = {
    ".pdf",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".xml",
    ".xlsx",
    ".docx",
    ".pptx",
    ".zip",
    ".py",
    ".js",
    ".ts",
    ".sh",
    ".rs",
    ".go",
    ".cpp",
    ".c",
    ".h",
}

_EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".sh": "bash",
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
    ".sql": "sql",
    ".toml": "toml",
    ".xml": "xml",
}

_ARTIFACT_EXTENSIONS = _IMAGE_EXTENSIONS | _DOWNLOAD_EXTENSIONS
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024  # 50 MiB

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

log = logging.getLogger(__name__)

_chat_sessions: Dict[str, str] = {}
_chat_workdirs: Dict[str, str] = {}


async def _emit_project_name(prompt: str, event_emitter: Optional[Callable]) -> str:
    """Извлекает название проекта из промпта или генерирует из ключевых слов."""
    explicit = re.search(
        r'(?:назов[её]м|название|named?|call(?:\s+it)?|project)\s+["\']?([A-Za-z0-9][A-Za-z0-9_\-]{1,30})["\']?',
        prompt,
        re.IGNORECASE,
    )
    if explicit:
        name = explicit.group(1)
    else:
        words = re.findall(r"[A-Za-z]{3,}", prompt)
        meaningful = [
            w.capitalize()
            for w in words
            if w.lower()
            not in {
                "the",
                "and",
                "for",
                "with",
                "from",
                "that",
                "this",
                "let",
                "make",
                "create",
                "write",
                "simple",
                "just",
            }
        ][:3]
        name = "_".join(meaningful) or "Project"

    if event_emitter:
        try:
            await event_emitter(
                {"type": "chat:title", "data": {"title": name.replace("_", " ")}}
            )
        except Exception:
            pass
    return name


_TOOL_PREVIEW_FIELDS = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebSearch": "query",
    "WebFetch": "url",
    "Task": "description",
}


def _tool_preview(name: str, tool_input: Dict[str, Any]) -> str:
    key = _TOOL_PREVIEW_FIELDS.get(name)
    if key and key in tool_input:
        raw = str(tool_input[key])
    elif tool_input:
        raw = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(tool_input.items())[:2])
    else:
        return ""
    first = raw.split("\n", 1)[0]
    truncated = first if len(first) <= 120 else first[:117] + "…"
    return truncated + (" …" if "\n" in raw and not truncated.endswith("…") else "")


_FENCE_LANG_PER_TOOL = {
    "Bash": "bash",
    "Glob": "text",
    "Grep": "text",
    "WebSearch": "text",
    "WebFetch": "text",
}


def _tool_input_block(name: str, tool_input: Dict[str, Any]) -> str:
    if not tool_input:
        return "```\n(no input)\n```"
    primary = _TOOL_PREVIEW_FIELDS.get(name)
    if primary and primary in tool_input:
        lang = _FENCE_LANG_PER_TOOL.get(name, "text")

        if name in ("Write", "Edit") and "content" in tool_input:
            file_path = tool_input.get(primary, "")
            ext = Path(file_path).suffix.lower()
            code_lang = _EXT_TO_LANG.get(ext, "text")
            content = tool_input["content"]
            display_path = (
                "/".join(Path(file_path).parts[-2:])
                if len(Path(file_path).parts) >= 2
                else file_path
            )
            return f"`{display_path}`\n\n```{code_lang}\n{content}\n```"

        parts = [f"```{lang}\n{tool_input[primary]}\n```"]
        others = {k: v for k, v in tool_input.items() if k != primary}
        if others:
            parts.append(
                f"```json\n{json.dumps(others, indent=2, ensure_ascii=False)}\n```"
            )
        return "\n\n".join(parts)
    return f"```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)}\n```"


def _format_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                parts.append(text if isinstance(text, str) else repr(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _iter_artifact_files(scan_dirs: List[Path]) -> "list[Path]":
    seen: List[Path] = []
    for path in scan_dirs[0].iterdir():
        if path.is_file() and path.suffix.lower() in _ARTIFACT_EXTENSIONS:
            seen.append(path)
    return seen


def _snapshot_artifacts(scan_dirs: List[Path]) -> Dict[str, int]:
    snapshot: Dict[str, int] = {}
    for path in _iter_artifact_files(scan_dirs):
        try:
            snapshot[str(path)] = path.stat().st_mtime_ns
        except OSError:
            pass
    return snapshot


async def _inline_new_artifacts(
    scan_dirs: List[Path],
    before: Dict[str, int],
    user_id: Optional[str],
) -> List[str]:
    if not user_id:
        return ["\n\n_(Can't save artifacts: no user context.)_\n"]
    try:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage
    except Exception as exc:
        return [f"\n\n_(File store unavailable: {exc})_\n"]

    chunks: List[str] = []
    for path in sorted(_iter_artifact_files(scan_dirs)):
        try:
            mtime = path.stat().st_mtime_ns
            size = path.stat().st_size
        except OSError:
            continue
        if before.get(str(path)) == mtime:
            continue
        if size > _MAX_ARTIFACT_BYTES:
            chunks.append(
                f"\n\n_(Skipped {path.name}: {size // 1024 // 1024} MiB exceeds {_MAX_ARTIFACT_BYTES // 1024 // 1024} MiB limit.)_\n"
            )
            continue

        ext = path.suffix.lower()
        is_image = ext in _IMAGE_EXTENSIONS
        mime = mimetypes.guess_type(path.name)[0] or (
            "image/png" if is_image else "application/octet-stream"
        )

        file_id = str(uuid.uuid4())
        storage_filename = f"{file_id}_{path.name}"
        try:
            with path.open("rb") as handle:
                contents, storage_path = Storage.upload_file(
                    handle,
                    storage_filename,
                    {
                        "OpenWebUI-User-Id": user_id,
                        "OpenWebUI-File-Id": file_id,
                    },
                )
        except Exception as exc:
            log.exception("Artifact upload failed: %s", path)
            chunks.append(f"\n\n_(Failed to save {path.name}: {exc})_\n")
            continue

        try:
            await Files.insert_new_file(
                user_id,
                FileForm(
                    id=file_id,
                    filename=path.name,
                    path=storage_path,
                    data={},
                    meta={
                        "name": path.name,
                        "content_type": mime,
                        "size": len(contents),
                    },
                ),
            )
        except Exception as exc:
            log.warning("DB insert FAILED: %s -> %s", path.name, exc)
            chunks.append(f"\n\n_(Saved but not linkable: {path.name}: {exc})_\n")
            continue

        if is_image:
            chunks.append(f"\n\n![{path.name}](/api/v1/files/{file_id}/content)\n")
        else:
            kib = size // 1024
            chunks.append(
                f"\n\n📎 [{path.name}](/api/v1/files/{file_id}/content) · {kib} KiB\n"
            )
    return chunks


def _extract_system_prompt(body: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []
    for msg in body.get("messages") or []:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    parts.append(piece.get("text", ""))
    merged = "\n\n".join(p for p in parts if p and p.strip())
    return merged or None


def _knowledge_collections(
    metadata: Optional[Dict[str, Any]],
    files: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set = set()

    def _add(coll: Any, name: Any) -> None:
        if not coll:
            return
        cid = str(coll)
        if cid in seen:
            return
        seen.add(cid)
        out.append({"id": cid, "name": str(name or cid)})

    def _consume(item: Dict[str, Any]) -> None:
        name = item.get("name")
        if item.get("collection_name"):
            _add(item["collection_name"], name)
            return
        if item.get("collection_names"):
            for coll in item["collection_names"]:
                _add(coll, name)
            return
        item_type = item.get("type")
        if item_type == "collection" and item.get("id"):
            _add(item["id"], name)
        elif item_type == "file" and item.get("id"):
            coll = item["id"] if item.get("legacy") else f"file-{item['id']}"
            _add(coll, name)

    model_knowledge = ((metadata or {}).get("model") or {}).get("info", {}).get(
        "meta", {}
    ).get("knowledge") or []
    for item in model_knowledge:
        if isinstance(item, dict):
            _consume(item)

    for f in files or []:
        if isinstance(f, dict):
            _consume(f)

    return out


def _knowledge_row_ids(metadata: Optional[Dict[str, Any]]) -> List[str]:
    ids: List[str] = []
    model_knowledge = ((metadata or {}).get("model") or {}).get("info", {}).get(
        "meta", {}
    ).get("knowledge") or []
    for item in model_knowledge:
        if (
            isinstance(item, dict)
            and item.get("type") == "collection"
            and item.get("id")
        ):
            ids.append(str(item["id"]))
    return ids


def _build_kb_mcp_server(
    knowledge: List[Dict[str, str]],
    knowledge_row_ids: Optional[List[str]] = None,
    user_dict: Optional[Dict[str, Any]] = None,
    event_emitter: Optional[Callable] = None,
):
    if not knowledge:
        return None, [], {}

    collection_names = [k["id"] for k in knowledge]
    display = ", ".join(k["name"] for k in knowledge)

    @tool(
        "search_knowledge",
        (
            f"Search the attached knowledge base(s): {display}. "
            "Call whenever you need internal facts, prior guidance, or "
            "documented product/process details. Reformulate and search "
            "multiple times if the first query misses."
        ),
        {"query": str, "top_k": int},
    )
    async def _search(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from open_webui.main import app
            from open_webui.models.users import Users
            from open_webui.retrieval.utils import query_collection
        except Exception as exc:
            return {
                "content": [
                    {"type": "text", "text": f"Knowledge search unavailable: {exc}"}
                ]
            }

        query = str(args.get("query") or "").strip()
        if not query:
            return {
                "content": [
                    {"type": "text", "text": "Empty query — nothing to search."}
                ]
            }
        try:
            default_top_k = int(app.state.config.RAG_TOP_K.value)
        except Exception:
            default_top_k = 5
        top_k = int(args.get("top_k") or default_top_k)

        user_obj = None
        user_id = (user_dict or {}).get("id")
        if user_id:
            try:
                user_obj = Users.get_user_by_id(user_id)
            except Exception:
                pass

        async def _embed(queries, prefix=None):
            return await app.state.EMBEDDING_FUNCTION(
                queries, prefix=prefix, user=user_obj
            )

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": f"🔎 Searching KB: {query[:80]}",
                            "done": False,
                        },
                    }
                )
            except Exception:
                pass

        try:
            results = await query_collection(
                collection_names=collection_names,
                queries=[query],
                embedding_function=_embed,
                k=top_k,
            )
        except Exception as exc:
            log.exception("KB search failed")
            return {"content": [{"type": "text", "text": f"Search failed: {exc}"}]}

        docs = (results.get("documents") or [[]])[0] or []
        metas = (results.get("metadatas") or [[]])[0] or []
        dists = (results.get("distances") or [[]])[0] or []

        if not docs:
            return {
                "content": [
                    {"type": "text", "text": f"No passages found for: {query!r}"}
                ]
            }

        if event_emitter:
            dist_iter = dists or [None] * len(metas)
            for doc, meta, dist in zip(docs, metas, dist_iter):
                meta = meta or {}
                source_name = (
                    meta.get("name")
                    or meta.get("source")
                    or meta.get("title")
                    or "unknown"
                )
                try:
                    await event_emitter(
                        {
                            "type": "citation",
                            "data": {
                                "document": [doc],
                                "metadata": [
                                    {
                                        "source": source_name,
                                        "file_id": meta.get("file_id", ""),
                                        "relevance_score": (
                                            round(float(dist), 3)
                                            if dist is not None
                                            else None
                                        ),
                                    }
                                ],
                                "source": {"name": source_name},
                            },
                        }
                    )
                except Exception:
                    pass

        parts = [f"Found {len(docs)} passage(s) for {query!r}:\n"]
        for i, (doc, meta) in enumerate(zip(docs, metas), 1):
            meta = meta or {}
            name = (
                meta.get("source") or meta.get("name") or meta.get("title") or "unknown"
            )
            parts.append(f'<source id="{i}" name="{name}">{doc}</source>')
        parts.append(
            "\nCite these using [1], [2], … in your response. Do not include the XML tags themselves."
        )
        return {"content": [{"type": "text", "text": "\n\n".join(parts)}]}

    kb_ids: List[str] = list(knowledge_row_ids or [])

    async def _iter_scoped_files():
        from open_webui.models.knowledge import Knowledges

        for kid in kb_ids:
            try:
                files = Knowledges.get_files_by_id(kid) or []
            except Exception:
                continue
            for f in files:
                yield f

    async def _allowed_file_ids() -> Set[str]:
        return {f.id async for f in _iter_scoped_files()}

    @tool(
        "list_knowledge_documents",
        (
            f"List every document in the attached knowledge base(s): {display}. "
            "Returns file_id, filename, and size for each."
        ),
        {},
    )
    async def _list_docs(args: Dict[str, Any]) -> Dict[str, Any]:
        if not kb_ids:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "No enumerable knowledge collections attached.",
                    }
                ]
            }
        lines = [f"Documents in {display}:"]
        count = 0
        async for f in _iter_scoped_files():
            content = (f.data or {}).get("content", "") or ""
            lines.append(
                f"- file_id={f.id} · {f.filename} · {len(content) // 1024} KiB · {len(content)} chars"
            )
            count += 1
        if count == 0:
            lines.append("(no files found)")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "read_knowledge_document",
        (
            "Read the full content of a knowledge document (or a character range of it) by file_id. "
            "Omit start_char/end_char to read the whole file. Each call caps at 40 000 chars."
        ),
        {"file_id": str, "start_char": int, "end_char": int},
    )
    async def _read_doc(args: Dict[str, Any]) -> Dict[str, Any]:
        from open_webui.models.files import Files

        file_id = str(args.get("file_id") or "").strip()
        if not file_id:
            return {"content": [{"type": "text", "text": "file_id is required."}]}
        allowed = await _allowed_file_ids()
        if file_id not in allowed:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"file_id {file_id!r} is not in the attached knowledge base(s).",
                    }
                ]
            }
        try:
            file_obj = Files.get_file_by_id(file_id)
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Lookup failed: {exc}"}]}
        if file_obj is None:
            return {"content": [{"type": "text", "text": "File not found."}]}
        content = (file_obj.data or {}).get("content", "") or ""
        total = len(content)
        start = max(0, int(args.get("start_char") or 0))
        raw_end = args.get("end_char")
        end = total if raw_end in (None, 0) else min(total, max(start, int(raw_end)))
        MAX_CHARS = 40_000
        if end - start > MAX_CHARS:
            end = start + MAX_CHARS
        slice_ = content[start:end]
        header = (
            f"# {file_obj.filename}\n"
            f"_chars {start}..{end} of {total}"
            f"{' (truncated — call again with a higher start_char to continue)' if end < total else ''}_\n\n"
        )
        return {"content": [{"type": "text", "text": header + slice_}]}

    @tool(
        "grep_knowledge",
        (
            "Regex/substring search across knowledge documents. Fast (runs on pre-extracted text in the DB). "
            "Use for exact keywords, product codes, acronyms where vector search struggles."
        ),
        {"pattern": str, "file_id": str, "case_insensitive": bool, "max_matches": int},
    )
    async def _grep(args: Dict[str, Any]) -> Dict[str, Any]:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return {"content": [{"type": "text", "text": "pattern is required."}]}
        flags = re.IGNORECASE if args.get("case_insensitive", True) else 0
        max_matches = int(args.get("max_matches") or 30)
        file_id_filter = str(args.get("file_id") or "").strip() or None
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return {"content": [{"type": "text", "text": f"Invalid regex: {exc}"}]}
        if file_id_filter:
            allowed = await _allowed_file_ids()
            if file_id_filter not in allowed:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"file_id {file_id_filter!r} is not in the attached knowledge base(s).",
                        }
                    ]
                }
        hits: List[str] = []
        files_scanned = 0
        async for f in _iter_scoped_files():
            if file_id_filter and f.id != file_id_filter:
                continue
            files_scanned += 1
            content = (f.data or {}).get("content", "") or ""
            for m in compiled.finditer(content):
                ctx_start = max(0, m.start() - 80)
                ctx_end = min(len(content), m.end() + 80)
                ctx = content[ctx_start:ctx_end].replace("\n", " ")
                hits.append(
                    f"- **{f.filename}** (file_id={f.id}) @ char {m.start()}:\n  …{ctx}…"
                )
                if len(hits) >= max_matches:
                    break
            if len(hits) >= max_matches:
                break
        scope = (
            f"1 file ({file_id_filter})"
            if file_id_filter
            else f"{files_scanned} file(s)"
        )
        if not hits:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"No matches for /{pattern}/ across {scope}.",
                    }
                ]
            }
        header = f"Found {len(hits)} match(es) for /{pattern}/ across {scope}{' (capped at max_matches)' if len(hits) >= max_matches else ''}:\n"
        return {"content": [{"type": "text", "text": header + "\n".join(hits)}]}

    tools_list = [_search]
    tool_names = ["mcp__helm-kb__search_knowledge"]
    tools_by_name: Dict[str, Any] = {"search_knowledge": _search}
    if kb_ids:
        tools_list.extend([_list_docs, _read_doc, _grep])
        tool_names.extend(
            [
                "mcp__helm-kb__list_knowledge_documents",
                "mcp__helm-kb__read_knowledge_document",
                "mcp__helm-kb__grep_knowledge",
            ]
        )
        tools_by_name.update(
            {
                "list_knowledge_documents": _list_docs,
                "read_knowledge_document": _read_doc,
                "grep_knowledge": _grep,
            }
        )

    server = create_sdk_mcp_server("helm-kb", "0.1", tools=tools_list)
    return server, tool_names, tools_by_name


def _anthropic_kb_tool_defs(
    knowledge: List[Dict[str, str]], has_kb_ids: bool
) -> List[Dict[str, Any]]:
    if not knowledge:
        return []
    display = ", ".join(k["name"] for k in knowledge)
    defs: List[Dict[str, Any]] = [
        {
            "name": "search_knowledge",
            "description": (
                f"Search the attached knowledge base(s): {display}. "
                "Call whenever you need internal facts, prior guidance, or "
                "documented product/process details. Reformulate and search "
                "multiple times if the first query misses."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        }
    ]
    if has_kb_ids:
        defs.extend(
            [
                {
                    "name": "list_knowledge_documents",
                    "description": f"List every document in {display}. Returns file_id, filename, and size for each.",
                    "input_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": "read_knowledge_document",
                    "description": "Read full content (or a character range) of a knowledge document by file_id. Caps at 40 000 chars per call.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "file_id": {"type": "string"},
                            "start_char": {"type": "integer"},
                            "end_char": {"type": "integer"},
                        },
                        "required": ["file_id"],
                    },
                },
                {
                    "name": "grep_knowledge",
                    "description": "Regex/substring search across knowledge documents. Fast (runs on pre-extracted text in the DB).",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                            "file_id": {"type": "string"},
                            "case_insensitive": {"type": "boolean"},
                            "max_matches": {"type": "integer"},
                        },
                        "required": ["pattern"],
                    },
                },
            ]
        )
    return defs


async def _dispatch_kb_tool(
    name: str,
    args: Dict[str, Any],
    tools_by_name: Dict[str, Any],
) -> str:
    sdk_tool = tools_by_name.get(name)
    if sdk_tool is None:
        return f"Unknown tool: {name}"
    try:
        result = await sdk_tool.handler(args or {})
    except Exception as exc:
        log.exception("KB tool %s failed", name)
        return f"Tool {name} failed: {exc}"
    content = result.get("content") or []
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return first.get("text", "")
    return ""


def _extract_latest_user_prompt(body: Dict[str, Any]) -> str:
    messages = body.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


_MODE_PREFIXES = ("/agent", "/fast")


def _strip_mode_prefix(prompt: str) -> str:
    stripped = prompt.lstrip()
    for tag in _MODE_PREFIXES:
        if stripped.startswith(tag):
            return stripped[len(tag) :].lstrip()
    return prompt


class Pipe:
    class Valves(BaseModel):
        ANTHROPIC_API_KEY: str = Field(
            default="",
            description="Anthropic API key (pay-per-token). Leave empty to use the subscription OAuth token or inherit from the backend env.",
        )
        CLAUDE_CODE_OAUTH_TOKEN: str = Field(
            default="",
            description=(
                "Long-lived Claude Pro/Max/Team OAuth token generated by "
                "`claude setup-token` on a machine with a browser. When set, "
                "bills against your subscription (not the API). Takes priority "
                "over ANTHROPIC_API_KEY — the key gets unset so it can't "
                "override. Anthropic's terms: use your own subscription only, "
                "don't re-offer subscription auth to end users."
            ),
        )
        MODELS: str = Field(
            default="claude-haiku-4-5:Haiku,claude-sonnet-4-6:Sonnet",
            description="Comma-separated list of models in format model_id:DisplayName",
        )
        PERMISSION_MODE: str = Field(
            default="bypassPermissions",
            description='Permission mode: "default", "acceptEdits", "bypassPermissions", "plan", or "dontAsk".',
        )
        ALLOWED_TOOLS: str = Field(
            default="Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch",
            description="Comma-separated tools auto-approved without prompting.",
        )
        WORKDIR_ROOT: str = Field(
            default="/tmp/claude-agent-pipe",
            description="Root directory for per-chat workspaces. One subdir per chat_id.",
        )
        CLAUDE_MD_TEMPLATE: str = Field(
            default="",
            description="Path to a CLAUDE.md template file to copy into each new project directory. Example: /home/your_dir/claude_template/CLAUDE.md",
        )
        MAX_TURNS: int = Field(
            default=30,
            description="Maximum agent turns per user message. 0 disables the cap.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._current_model: str = ""

    @property
    def _model(self) -> str:
        """Текущая модель — берётся из _current_model или первой записи MODELS."""
        if self._current_model:
            return self._current_model
        first = self.valves.MODELS.split(",")[0].strip()
        model_id = first.split(":")[0].strip() if ":" in first else first
        return model_id

    def pipes(self) -> List[Dict[str, str]]:
        result = []
        for entry in self.valves.MODELS.split(","):
            entry = entry.strip()
            if ":" in entry:
                model_id, display = entry.split(":", 1)
            else:
                model_id, display = entry, entry
            result.append(
                {
                    "id": f"claude-code-{model_id.strip()}",
                    "name": f"Claude Code ({display.strip()})",
                }
            )
        return result

    async def _run_messages_api(
        self,
        body: Dict[str, Any],
        user_dict: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        files: Optional[List[Dict[str, Any]]],
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            yield "_Fast path needs the `anthropic` package — add it to the Function requirements._"
            return

        if self.valves.ANTHROPIC_API_KEY:
            client = AsyncAnthropic(api_key=self.valves.ANTHROPIC_API_KEY)
        else:
            client = AsyncAnthropic()

        system_parts: List[str] = []
        ws_system = _extract_system_prompt(body)
        if ws_system:
            system_parts.append(ws_system)
        system = "\n\n".join(p for p in system_parts if p.strip()) or None

        knowledge = _knowledge_collections(metadata, files)
        kb_row_ids = _knowledge_row_ids(metadata)
        _, _, kb_tools_by_name = _build_kb_mcp_server(
            knowledge,
            knowledge_row_ids=kb_row_ids,
            user_dict=user_dict,
            event_emitter=event_emitter,
        )
        tool_defs = _anthropic_kb_tool_defs(knowledge, bool(kb_row_ids))

        messages: List[Dict[str, Any]] = []
        for msg in body.get("messages") or []:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user":
                content = _strip_mode_prefix(content)
            if content:
                messages.append({"role": role, "content": content})

        if not messages:
            return

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {"description": "⚡ fast mode", "done": False},
                    }
                )
            except Exception:
                pass

        MAX_TOOL_ROUNDS = 10
        for _round in range(MAX_TOOL_ROUNDS + 1):
            kwargs: Dict[str, Any] = {
                "model": self._model,
                "max_tokens": 4096,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tool_defs:
                kwargs["tools"] = tool_defs

            try:
                async with client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        yield text
                    final = await stream.get_final_message()
            except Exception as exc:
                log.exception("Fast path failed")
                yield f"\n\n**Fast-path error:** `{type(exc).__name__}: {exc}`\n"
                return

            if final.stop_reason != "tool_use":
                return

            assistant_content: List[Dict[str, Any]] = []
            for block in final.content:
                bt = block.type
                if bt == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif bt == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results: List[Dict[str, Any]] = []
            for block in final.content:
                if block.type != "tool_use":
                    continue
                preview = _tool_preview(block.name, block.input or {})
                if event_emitter:
                    try:
                        await event_emitter(
                            {
                                "type": "status",
                                "data": {
                                    "description": f"🔧 {block.name}"
                                    + (f": {preview}" if preview else ""),
                                    "done": False,
                                },
                            }
                        )
                    except Exception:
                        pass
                summary = f"🔧 {block.name}" + (f" · {preview}" if preview else "")
                yield (
                    "\n\n<details>\n"
                    f"<summary>{summary}</summary>\n\n"
                    f"{_tool_input_block(block.name, block.input or {})}\n\n"
                    "</details>\n\n"
                )
                text = await _dispatch_kb_tool(
                    block.name, block.input or {}, kb_tools_by_name
                )
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": text}
                )
            messages.append({"role": "user", "content": tool_results})

        yield "\n\n_(Fast-path tool loop cap reached.)_\n"

    async def _run_lite_agent(
        self,
        body: Dict[str, Any],
        user_dict: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        files: Optional[List[Dict[str, Any]]],
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        prompt = _strip_mode_prefix(_extract_latest_user_prompt(body))
        if not prompt:
            return

        system_parts: List[str] = []
        ws_system = _extract_system_prompt(body)
        if ws_system:
            system_parts.append(ws_system)

        history_lines: List[str] = []
        messages = body.get("messages") or []
        user_turns_remaining = sum(1 for m in messages if m.get("role") == "user")
        consumed = 0
        for msg in messages:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user":
                content = _strip_mode_prefix(content)
                consumed += 1
                if consumed == user_turns_remaining:
                    continue
            if content.strip():
                history_lines.append(f"{role.capitalize()}: {content.strip()}")
        if history_lines:
            system_parts.append(
                "Prior conversation (for context):\n\n" + "\n\n".join(history_lines)
            )

        knowledge = _knowledge_collections(metadata, files)
        kb_row_ids = _knowledge_row_ids(metadata)
        kb_server, kb_tool_names, _kb_dict = _build_kb_mcp_server(
            knowledge,
            knowledge_row_ids=kb_row_ids,
            user_dict=user_dict,
            event_emitter=event_emitter,
        )

        if kb_tool_names:
            system_parts.append(
                "You have read-only knowledge-base tools ("
                + ", ".join(t.rsplit("__", 1)[-1] for t in kb_tool_names)
                + "). Use them when the user asks about facts that might be in the knowledge base."
            )
        else:
            system_parts.append("Respond concisely and directly.")
        system_text = "\n\n".join(p for p in system_parts if p.strip())

        options_kwargs: Dict[str, Any] = {
            "model": self._model,
            "permission_mode": self.valves.PERMISSION_MODE,
            "allowed_tools": kb_tool_names,
            "setting_sources": [],
            "system_prompt": system_text,
            "include_partial_messages": True,
        }
        if kb_server is not None:
            options_kwargs["mcp_servers"] = {"helm-kb": kb_server}
        options = ClaudeAgentOptions(**options_kwargs)

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {"description": "⚡ fast mode (OAuth)", "done": False},
                    }
                )
            except Exception:
                pass

        thinking_buffers: Dict[int, str] = {}
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, StreamEvent):
                        ev = message.event or {}
                        etype = ev.get("type")
                        if etype == "message_start":
                            thinking_buffers.clear()
                        elif etype == "content_block_start":
                            block = ev.get("content_block") or {}
                            if block.get("type") == "thinking":
                                thinking_buffers[ev.get("index", 0)] = ""
                                yield "<thinking>"
                        elif etype == "content_block_delta":
                            delta = ev.get("delta") or {}
                            dt = delta.get("type")
                            if dt == "text_delta":
                                yield delta.get("text", "")
                            elif dt == "thinking_delta":
                                idx = ev.get("index", 0)
                                if idx in thinking_buffers:
                                    thinking_buffers[idx] += delta.get("thinking", "")
                                    yield delta.get("thinking", "")
                        elif etype == "content_block_stop":
                            idx = ev.get("index", 0)
                            if idx in thinking_buffers:
                                thinking_buffers.pop(idx)
                                yield "</thinking>"
                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                preview = _tool_preview(block.name, block.input)
                                summary = f"🔧 {block.name}" + (
                                    f" · {preview}" if preview else ""
                                )
                                yield (
                                    "\n\n<details>\n"
                                    f"<summary>{summary}</summary>\n\n"
                                    f"{_tool_input_block(block.name, block.input)}\n\n"
                                    "</details>\n\n"
                                )
                    elif isinstance(message, UserMessage):
                        content = message.content
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, ToolResultBlock)
                                    and block.is_error
                                ):
                                    err_text = _format_tool_result(block.content)[:400]
                                    yield (
                                        "\n\n<details>\n<summary>"
                                        "⚙️ tool hiccup</summary>\n\n"
                                        f"```\n{err_text}\n```\n\n"
                                        "</details>\n\n"
                                    )
                    elif isinstance(message, ResultMessage):
                        return
        except Exception as exc:
            log.exception("Lite-agent fast path failed")
            yield f"\n\n**Fast-path error:** `{type(exc).__name__}: {exc}`\n"

    async def pipe(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable] = None,
        __files__: Optional[List[Dict[str, Any]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        if self.valves.CLAUDE_CODE_OAUTH_TOKEN:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = self.valves.CLAUDE_CODE_OAUTH_TOKEN
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        elif self.valves.ANTHROPIC_API_KEY:
            os.environ["ANTHROPIC_API_KEY"] = self.valves.ANTHROPIC_API_KEY
        os.environ.setdefault("IS_SANDBOX", "1")

        # Определяем модель по выбранному pipe
        selected = (body.get("model") or "").split(".")[-1]
        if selected.startswith("claude-code-"):
            self._current_model = selected[len("claude-code-") :]
        else:
            self._current_model = ""

        prompt = _extract_latest_user_prompt(body)
        if not prompt:
            yield "_No user message to send to Claude Code._"
            return

        prompt = _strip_mode_prefix(prompt)

        chat_id = __chat_id__ or "default"
        if chat_id not in _chat_workdirs:
            name = await _emit_project_name(prompt, __event_emitter__)
            if name == "Project":
                name = f"Project_{chat_id[:6]}"
            _chat_workdirs[chat_id] = name
        workdir = Path(self.valves.WORKDIR_ROOT) / _chat_workdirs[chat_id]
        workdir.mkdir(parents=True, exist_ok=True)

        if self.valves.CLAUDE_MD_TEMPLATE:
            template = Path(self.valves.CLAUDE_MD_TEMPLATE)
            claude_md = workdir / "CLAUDE.md"
            if template.exists() and not claude_md.exists():
                import shutil

                shutil.copy2(template, claude_md)

        allowed_tools = [
            t.strip() for t in self.valves.ALLOWED_TOOLS.split(",") if t.strip()
        ]
        resume_id = _chat_sessions.get(chat_id)

        kb_server, kb_tool_names, _ = _build_kb_mcp_server(
            _knowledge_collections(__metadata__, __files__),
            knowledge_row_ids=_knowledge_row_ids(__metadata__),
            user_dict=__user__,
            event_emitter=__event_emitter__,
        )
        allowed_tools = allowed_tools + kb_tool_names

        options_kwargs: Dict[str, Any] = {
            "cwd": str(workdir),
            "model": self._model,
            "permission_mode": self.valves.PERMISSION_MODE,
            "allowed_tools": allowed_tools,
            "setting_sources": [],
            "include_partial_messages": True,
        }
        if resume_id:
            options_kwargs["resume"] = resume_id
        if self.valves.MAX_TURNS:
            options_kwargs["max_turns"] = self.valves.MAX_TURNS
        if kb_server is not None:
            options_kwargs["mcp_servers"] = {"helm-kb": kb_server}

        system_prompt = _extract_system_prompt(body)
        cwd_instruction = f"Always write files to the current working directory ({workdir}), never use /tmp or absolute paths like /root or /home unless explicitly asked."
        append_text = (
            f"{cwd_instruction}\n\n{system_prompt}"
            if system_prompt
            else cwd_instruction
        )
        options_kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": append_text,
        }

        options = ClaudeAgentOptions(**options_kwargs)

        async def emit_status(description: str, done: bool = False) -> None:
            if __event_emitter__ is None:
                return
            await __event_emitter__(
                {"type": "status", "data": {"description": description, "done": done}}
            )

        await emit_status("Starting Claude Code…")
        scan_dirs = [workdir]
        artifact_snapshot = _snapshot_artifacts(scan_dirs)

        thinking_buffers: Dict[int, str] = {}
        active_tools: Dict[str, Dict[str, Any]] = {}
        heartbeat_task: Optional[asyncio.Task] = None

        async def _heartbeat() -> None:
            try:
                while active_tools:
                    await asyncio.sleep(2)
                    if not active_tools:
                        return
                    oldest = min(active_tools.values(), key=lambda t: t["started"])
                    elapsed = int(time.monotonic() - oldest["started"])
                    count = len(active_tools)
                    label = (
                        oldest["label"]
                        if count == 1
                        else f"{count} tools · longest {oldest['label']}"
                    )
                    await emit_status(f"⏳ {label} · running {elapsed}s…")
            except asyncio.CancelledError:
                pass

        def _ensure_heartbeat() -> None:
            nonlocal heartbeat_task
            if heartbeat_task is None or heartbeat_task.done():
                heartbeat_task = asyncio.create_task(_heartbeat())

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            session_id = message.data.get("session_id")
                            if session_id:
                                _chat_sessions[chat_id] = session_id
                        continue

                    if isinstance(message, StreamEvent):
                        ev = message.event or {}
                        etype = ev.get("type")
                        if etype == "message_start":
                            thinking_buffers.clear()
                        elif etype == "content_block_start":
                            block = ev.get("content_block") or {}
                            if block.get("type") == "thinking":
                                thinking_buffers[ev.get("index", 0)] = ""
                                yield "<thinking>"
                        elif etype == "content_block_delta":
                            delta = ev.get("delta") or {}
                            dt = delta.get("type")
                            if dt == "text_delta":
                                yield delta.get("text", "")
                            elif dt == "thinking_delta":
                                idx = ev.get("index", 0)
                                if idx in thinking_buffers:
                                    thinking_buffers[idx] += delta.get("thinking", "")
                                    yield delta.get("thinking", "")
                        elif etype == "content_block_stop":
                            idx = ev.get("index", 0)
                            if idx in thinking_buffers:
                                thinking_buffers.pop(idx)
                                yield "</thinking>"
                        continue

                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                preview = _tool_preview(block.name, block.input)
                                label = (
                                    f"{block.name}: {preview}"
                                    if preview
                                    else block.name
                                )
                                await emit_status(f"🔧 {label}")
                                active_tools[block.id] = {
                                    "label": label,
                                    "started": time.monotonic(),
                                }
                                _ensure_heartbeat()
                                summary_text = f"🔧 {block.name}" + (
                                    f" · {preview}" if preview else ""
                                )
                                tool_body = _tool_input_block(block.name, block.input)
                                yield (
                                    "\n\n<details>\n"
                                    f"<summary>{summary_text}</summary>\n\n"
                                    f"{tool_body}\n\n"
                                    "</details>\n\n"
                                )
                        continue

                    if isinstance(message, UserMessage):
                        content = message.content
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                active_tools.pop(block.tool_use_id, None)
                                if block.is_error:
                                    err_text = _format_tool_result(block.content)[:800]
                                    yield (
                                        "\n\n<details>\n<summary>"
                                        "⚙️ tool hiccup (retrying)"
                                        "</summary>\n\n"
                                        f"```\n{err_text}\n```\n\n"
                                        "</details>\n\n"
                                    )
                        continue

                    if isinstance(message, ResultMessage):
                        await emit_status("Done.", done=True)
                        for chunk in await _inline_new_artifacts(
                            scan_dirs,
                            artifact_snapshot,
                            (__user__ or {}).get("id"),
                        ):
                            yield chunk
                        if message.subtype != "success":
                            yield f"\n\n_Agent stopped: {message.subtype}_\n"
                        if message.total_cost_usd is not None:
                            yield f"\n\n_Cost: ${message.total_cost_usd:.4f} · {message.duration_ms}ms_\n"
                        return

        except Exception as exc:
            log.exception("Claude Agent SDK pipe failed")
            await emit_status(f"Error: {exc}", done=True)
            yield f"\n\n**Claude Code error:** `{type(exc).__name__}: {exc}`\n"
        finally:
            active_tools.clear()
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
