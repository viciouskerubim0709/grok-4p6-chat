"""memory.md layer for the 4.6 app.

The row in memory_docs is the file. Do not copy it into chats.messages.
Inject content on every request. Mutate only through edit_memory.
"""

from __future__ import annotations

DOC_ID = "default"

MAX_OPS = 16

MEMORY_RULES = """\
You have a persistent memory file named memory.md.
It is already loaded below. Do not ask to open it.

Rules:
- One fact per line. Keep existing wording unless you are correcting it.
- To add a fact, call edit_memory with old_str="" and new_str set to the new line(s).
- To change a fact, old_str must be an exact excerpt that appears once.
- To delete a fact, new_str="".
- Never rewrite the whole file.
- Do not store passwords, API keys, tokens, or one-off mood.
- After a successful edit, answer the user. Do not mention the tool unless they asked.
"""


def load_memory(sb, doc_id: str = DOC_ID) -> dict:
    res = (
        sb.table("memory_docs")
        .select("content, version")
        .eq("id", doc_id)
        .single()
        .execute()
    )
    row = res.data or {}
    return {"content": row.get("content") or "", "version": int(row.get("version") or 1)}


def memory_block(content: str) -> str:
    body = content.strip()
    if not body:
        body = "(empty)"
    return f"<memory.md>\n{body}\n</memory.md>"


def edit_memory_tool() -> dict:
    """Chat Completions shape. Do not send this to responses.create."""
    return {
        "type": "function",
        "function": {
            "name": "edit_memory",
            "description": (
                "Edit memory.md. Apply operations strictly in order on one in-memory copy. "
                "Call this once per assistant message. Put every change in operations[]. "
                "old_str must match exactly once in the current text after previous ops. "
                "Empty old_str appends new_str."
                "Empty new_str deletes the matched text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_str": {
                                    "type": "string",
                                    "description": "Exact excerpt. Empty string appends.",
                                },
                                "new_str": {
                                    "type": "string",
                                    "description": "Replacement. Empty string deletes old_str.",
                                },
                            },
                            "required": ["old_str", "new_str"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["operations"],
                "additionalProperties": False,
            },
        }

def edit_memory_tool_responses() -> dict:
    """Responses API shape. Flat name, not nested under function."""
    nested = edit_memory_tool()["function"]
    return {"type": "function", **nested}

def apply_edit(content: str, old_str: str, new_str: str) -> str:
    if old_str == "":
        if new_str == "":
            raise ValueError("both old_str and new_str are empty")
        sep = "" if content.endswith("\n") or content == "" else "\n"
        return content + sep + new_str

    count = content.count(old_str)
    if count == 0:
        raise ValueError("old_str not found")
    if count > 1:
        raise ValueError(f"old_str matched {count} times")
    return content.replace(old_str, new_str, 1)

def apply_operations(content: str, operations: list[dict]) -> str:
    if not operations:
        raise ValueError("operations is empty")
    if len(operations) > MAX_OPS:
        raise ValueError(f"too many operations ({len(operations)} > {MAX_OPS})")

    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            raise ValueError(f"op[{i}]: not an object")
        old_str = op.get("old_str", "")
        new_str = op.get("new_str", "")
        if not isinstance(old_str, str) or not isinstance(new_str, str):
            raise ValueError(f"op[{i}]: old_str/new_str must be strings")
        try:
            content = apply_edit(content, old_str, new_str)
        except ValueError as exc:
            raise ValueError(f"op[{i}]: {exc}") from exc
    return content

def _op_type(old_str: str, new_str: str) -> str:
    if old_str == "":
        return "append"
    if new_str == "":
        return "delete"
    return "replace"

def _log(sb, *, version_before, version_after, operation, old_str, new_str, success, error, chat_id):
    sb.table("memory_edits").insert(
        {
            "doc_id": DOC_ID,
            "version_before": version_before,
            "version_after": version_after,
            "operation": operation,
            "old_str": old_str,
            "new_str": new_str,
            "success": success,
            "error": error,
            "chat_id": chat_id,
        }
    ).execute()


def commit_edit(
    sb,
    old_str: str = "",
    new_str: str = "",
    operations: list | None = None,
    chat_id: str | None = None,
    retries: int = 2,
) -> dict:
    try:
        ops = operations if operations is not None else [
            {"old_str": old_str, "new_str": new_str}
        ]
        if not ops:
            raise ValueError("operations is empty")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    for _ in range(retries + 1):
        current = load_memory(sb)
        try:
            new_content = apply_operations(current["content"], ops)
        except ValueError as exc:
            fail_op = ops[0] if not ops else ops[min(
                _fail_index(str(exc)), len(ops) - 1
            )]
            _log(
                sb,
                version_before=current["version"],
                version_after=None,
                operation=_op_type(fail_op.get("old_str", ""), fail_op.get("new_str", "")),
                old_str=fail_op.get("old_str", ""),
                new_str=fail_op.get("new_str", ""),
                success=False,
                error=str(exc),
                chat_id=chat_id,
            )
            return {
                "ok": False,
                "error": str(exc),
                "content": current["content"],
                "version": current["version"],
            }

        updated = (
            sb.table("memory_docs")
            .update({"content": new_content, "version": current["version"] + 1})
            .eq("id", DOC_ID)
            .eq("version", current["version"])
            .execute()
        )
        if updated.data:
            version_after = current["version"] + 1
            for op in ops:
                o = op.get("old_str", "")
                n = op.get("new_str", "")
                _log(
                    sb,
                    version_before=current["version"],
                    version_after=version_after,
                    operation=_op_type(o, n),
                    old_str=o,
                    new_str=n,
                    success=True,
                    error=None,
                    chat_id=chat_id,
                )
            return {
                "ok": True,
                "applied": len(ops),
                "content": new_content,
                "version": version_after,
            }

    current = load_memory(sb)
    return {
        "ok": False,
        "error": "version conflict",
        "content": current["content"],
        "version": current["version"],
    }


def _fail_index(error: str) -> int:
    # "op[2]: old_str not found" → 2
    if error.startswith("op[") and "]" in error:
        try:
            return int(error[3:error.index("]")])
        except ValueError:
            return 0
    return 0
