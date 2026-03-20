"""把 OpenAI chat 消息压平为 Qoder 可接受的 prompt。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .schemas import ChatMessage

_ROLE_LABELS = {
    "system": "系统",
    "user": "用户",
    "assistant": "助手",
    "tool": "工具",
}


@dataclass(slots=True)
class PreparedPrompt:
    """压平后的请求。"""

    prompt: str
    attachments: list[Path]


def _resolve_local_attachment(raw_url: str | None) -> Path:
    if not raw_url:
        raise ValueError("图片内容缺少可用的 url")

    parsed = urlparse(raw_url)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).expanduser().resolve()

    candidate = Path(raw_url).expanduser()
    if candidate.exists():
        return candidate.resolve()

    raise ValueError("qoder2api 当前只支持本地图片附件（绝对路径或 file:// URL）")


def _append_unique_attachment(attachments: list[Path], path: Path) -> None:
    if path not in attachments:
        attachments.append(path)


def _serialize_unknown_block(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def _content_to_text(
    content: str | list[dict[str, Any]] | None,
    attachments: list[Path],
) -> str:
    if content is None:
        return ""

    if isinstance(content, str):
        return content.strip()

    lines: list[str] = []
    for item in content:
        block_type = str(item.get("type") or "").strip()
        if block_type in {"text", "input_text"}:
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(text)
            continue

        if block_type in {"image_url", "input_image"}:
            raw_image = item.get("image_url") or item.get("source") or {}
            if isinstance(raw_image, dict):
                raw_url = raw_image.get("url") or raw_image.get("path")
            else:
                raw_url = str(raw_image)

            attachment = _resolve_local_attachment(raw_url)
            _append_unique_attachment(attachments, attachment)
            lines.append(f"[图片附件: {attachment.name}]")
            continue

        lines.append(f"[未识别内容块] {_serialize_unknown_block(item)}")

    return "\n".join(line for line in lines if line).strip()


def _render_message_block(message: ChatMessage, attachments: list[Path]) -> str:
    role = _ROLE_LABELS.get(message.role, message.role)
    content_text = _content_to_text(message.content, attachments) or "(空内容)"

    extras: list[str] = []
    if message.name:
        extras.append(f"name={message.name}")
    if message.tool_call_id:
        extras.append(f"tool_call_id={message.tool_call_id}")
    if message.tool_calls:
        extras.append(
            "tool_calls=" + json.dumps(message.tool_calls, ensure_ascii=False, sort_keys=True)
        )

    header = f"[{role}]"
    if extras:
        header += " " + " ".join(extras)

    return f"{header}\n{content_text}"


def _render_response_format_hint(response_format: dict[str, Any] | None) -> str:
    if not response_format:
        return ""

    hint_type = str(response_format.get("type") or "").strip()
    if hint_type == "json_object":
        return "输出要求：请只输出一个合法 JSON 对象，不要使用 Markdown 代码块。"

    if hint_type == "json_schema":
        json_schema = response_format.get("json_schema")
        serialized_schema = ""
        if isinstance(json_schema, dict):
            serialized_schema = json.dumps(json_schema, ensure_ascii=False, sort_keys=True)
        return (
            "输出要求：请只输出符合以下 JSON Schema 的合法 JSON，不要使用 Markdown 代码块。\n"
            f"{serialized_schema}"
        ).strip()

    return ""


def render_openai_messages(
    messages: list[ChatMessage],
    response_format: dict[str, Any] | None = None,
) -> PreparedPrompt:
    """把 OpenAI 对话转换成单条 Qoder prompt。"""
    if not messages:
        raise ValueError("messages 不能为空")

    attachments: list[Path] = []
    system_blocks: list[str] = []
    dialogue_blocks: list[str] = []

    for message in messages:
        rendered = _render_message_block(message, attachments)
        if message.role == "system":
            system_blocks.append(rendered)
        else:
            dialogue_blocks.append(rendered)

    if not dialogue_blocks:
        raise ValueError("至少需要一条非 system 消息")

    prompt_lines = [
        "你正在处理一个经由 qoder2api 转发的 OpenAI Chat Completions 请求。",
        "请结合系统指令和完整对话历史，直接继续回答最后一个用户问题。",
        "保持与用户相同的语言；如果用户明确要求特定格式，请严格遵守。",
    ]

    if system_blocks:
        prompt_lines.extend(
            [
                "",
                "=== 系统指令 ===",
                "\n\n".join(system_blocks),
            ]
        )

    prompt_lines.extend(
        [
            "",
            "=== 对话历史 ===",
            "\n\n".join(dialogue_blocks),
        ]
    )

    response_format_hint = _render_response_format_hint(response_format)
    if response_format_hint:
        prompt_lines.extend(["", "=== 输出约束 ===", response_format_hint])

    return PreparedPrompt(
        prompt="\n".join(prompt_lines).strip(),
        attachments=attachments,
    )
