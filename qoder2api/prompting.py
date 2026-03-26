"""把 OpenAI chat 消息压平为 Qoder 可接受的 prompt。"""

from __future__ import annotations

import base64
import json
import mimetypes
from os.path import basename
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .schemas import ChatMessage

_ROLE_LABELS = {
    "system": "系统",
    "user": "用户",
    "assistant": "助手",
    "tool": "工具",
}
_COMMON_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}
_REMOTE_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_TOOL_RESULT_MAX_CHARS = 12000
_TOOL_RESULT_HEAD_CHARS = 8000
_TOOL_RESULT_TAIL_CHARS = 4000

_INLINE_IMAGE_NAMES = {
    "image/jpeg": "inline-image.jpg",
    "image/png": "inline-image.png",
    "image/gif": "inline-image.gif",
    "image/webp": "inline-image.webp",
    "image/bmp": "inline-image.bmp",
    "image/svg+xml": "inline-image.svg",
}


@dataclass(slots=True)
class PreparedAttachment:
    """待发送给 Qoder 的附件。"""

    url: str
    display_name: str
    local_path: Path | None = None


@dataclass(slots=True)
class PreparedPrompt:
    """压平后的请求。"""

    prompt: str
    attachments: list[PreparedAttachment]
    messages: list[dict[str, Any]]


def _inline_image_name(mime_type: str) -> str:
    normalized = mime_type.strip().lower()
    if normalized in _INLINE_IMAGE_NAMES:
        return _INLINE_IMAGE_NAMES[normalized]

    extension = normalized.split("/", 1)[-1].replace("+xml", "")
    extension = extension.replace("jpeg", "jpg") or "bin"
    return f"inline-image.{extension}"


def _guess_image_mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed

    override = _COMMON_IMAGE_MIME_TYPES.get(path.suffix.lower())
    if override:
        return override

    raise ValueError(f"不支持的图片类型: {path.name}")


def _build_local_attachment(path: Path) -> PreparedAttachment:
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"图片附件不存在: {resolved}")

    mime_type = _guess_image_mime_type(resolved)
    encoded = base64.b64encode(resolved.read_bytes()).decode("ascii")
    return PreparedAttachment(
        url=f"data:{mime_type};base64,{encoded}",
        display_name=resolved.name,
        local_path=resolved,
    )




def _build_remote_attachment(raw_url: str) -> PreparedAttachment:
    parsed = urlparse(raw_url)
    timeout = httpx.Timeout(20.0, connect=10.0)
    with httpx.stream("GET", raw_url, follow_redirects=True, timeout=timeout) as response:
        response.raise_for_status()
        header_value = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        mime_type = header_value if header_value.startswith("image/") else ""
        if not mime_type:
            suffix = Path(unquote(parsed.path)).suffix.lower()
            if suffix:
                mime_type = _COMMON_IMAGE_MIME_TYPES.get(suffix, "")
        if not mime_type.startswith("image/"):
            raise ValueError("远程图片 URL 未返回可识别的 image/* Content-Type")

        chunks: list[bytes] = []
        total_size = 0
        for chunk in response.iter_bytes():
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > _REMOTE_IMAGE_MAX_BYTES:
                raise ValueError("远程图片过大，超过 20 MiB 安全限制")
            chunks.append(chunk)

    encoded = base64.b64encode(b"".join(chunks)).decode("ascii")
    filename = basename(unquote(parsed.path)) or _inline_image_name(mime_type)
    return PreparedAttachment(
        url=f"data:{mime_type};base64,{encoded}",
        display_name=filename,
    )


def _build_data_url_attachment(raw_url: str) -> PreparedAttachment:
    header, separator, _ = raw_url.partition(",")
    if not separator:
        raise ValueError("图片 data URL 格式不合法")

    mime_type = header[5:].split(";", 1)[0].strip().lower()
    if not mime_type.startswith("image/"):
        raise ValueError("当前只支持图片类型的 data URL")

    return PreparedAttachment(
        url=raw_url,
        display_name=_inline_image_name(mime_type),
    )


def _resolve_local_attachment(raw_url: str | None) -> PreparedAttachment:
    if not raw_url:
        raise ValueError("图片内容缺少可用的 url")

    parsed = urlparse(raw_url)
    if parsed.scheme == "file":
        return _build_local_attachment(Path(unquote(parsed.path)))

    if parsed.scheme == "data":
        return _build_data_url_attachment(raw_url)

    if parsed.scheme in {"http", "https"}:
        return _build_remote_attachment(raw_url)

    return _build_local_attachment(Path(raw_url).expanduser())


def _append_unique_attachment(
    attachments: list[PreparedAttachment],
    attachment: PreparedAttachment,
) -> None:
    if attachment not in attachments:
        attachments.append(attachment)


def _serialize_unknown_block(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def _extract_image_url(item: dict[str, Any]) -> str | None:
    raw_image = item.get("image_url") or item.get("source") or {}
    if isinstance(raw_image, dict):
        raw_url = raw_image.get("url") or raw_image.get("path")
        if raw_url:
            return str(raw_url)

        media_type = str(raw_image.get("media_type") or raw_image.get("mime_type") or "").strip()
        raw_data = str(raw_image.get("data") or "").strip()
        if media_type and raw_data:
            return f"data:{media_type};base64,{raw_data}"
        return None

    return str(raw_image)


def _normalize_message_text_content(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()

    parts: list[str] = []
    for item in content:
        block_type = str(item.get("type") or "").strip()
        if block_type in {"text", "input_text"}:
            block_text = str(item.get("text") or "").strip()
            if block_text:
                parts.append(block_text)
    return "\n".join(parts).strip()


def _truncate_tool_result_text(text: str) -> str:
    normalized = text.strip()
    if len(normalized) <= _TOOL_RESULT_MAX_CHARS:
        return normalized

    head = normalized[:_TOOL_RESULT_HEAD_CHARS].rstrip()
    tail = normalized[-_TOOL_RESULT_TAIL_CHARS:].lstrip()
    return (
        f"[tool_result 内容过长，已截断。原始长度 {len(normalized)} 字符，"
        f"仅保留前 {_TOOL_RESULT_HEAD_CHARS} 与后 {_TOOL_RESULT_TAIL_CHARS} 字符。]\n"
        f"<tool_result_head>\n{head}\n</tool_result_head>\n"
        f"<tool_result_tail>\n{tail}\n</tool_result_tail>"
    )


def _tool_message_content_to_text(
    content: str | list[dict[str, Any]] | None,
    attachments: list[PreparedAttachment],
) -> str:
    return _truncate_tool_result_text(_content_to_text(content, attachments))


def _build_message_contents(
    content: str | list[dict[str, Any]] | None,
    attachments: list[PreparedAttachment],
) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        normalized = content.strip()
        return [{"type": "text", "text": normalized}] if normalized else []

    contents: list[dict[str, Any]] = []
    for item in content:
        block_type = str(item.get("type") or "").strip()
        if block_type in {"text", "input_text"}:
            block_text = str(item.get("text") or "").strip()
            if block_text:
                contents.append({"type": "text", "text": block_text})
            continue

        if block_type in {"image_url", "input_image"}:
            attachment = _resolve_local_attachment(_extract_image_url(item))
            _append_unique_attachment(attachments, attachment)
            contents.append({"type": "image_url", "image_url": {"url": attachment.url}})
            if attachment.local_path is not None:
                contents.append(
                    {
                        "type": "text",
                        "text": (
                            f"<system-reminder>\n\nabove is an image file in {attachment.local_path}"
                            "\n\n</system-reminder>"
                        ),
                    }
                )
            continue

        contents.append(item)
    return contents


def _build_upstream_message(
    message: ChatMessage,
    attachments: list[PreparedAttachment],
) -> dict[str, Any]:
    upstream: dict[str, Any] = {"role": message.role}
    if message.name:
        upstream["name"] = message.name
    if message.tool_call_id:
        upstream["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        upstream["tool_calls"] = message.tool_calls

    if message.role == "tool":
        upstream["content"] = _tool_message_content_to_text(message.content, attachments)
        return upstream

    if isinstance(message.content, list):
        contents = _build_message_contents(message.content, attachments)
        upstream["content"] = _normalize_message_text_content(message.content)
        if contents:
            upstream["contents"] = contents
        return upstream

    normalized_text = _normalize_message_text_content(message.content)
    if normalized_text or message.role != "assistant" or not message.tool_calls:
        upstream["content"] = normalized_text
    else:
        upstream["content"] = ""
    return upstream


def _content_to_text(
    content: str | list[dict[str, Any]] | None,
    attachments: list[PreparedAttachment],
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
            attachment = _resolve_local_attachment(_extract_image_url(item))
            _append_unique_attachment(attachments, attachment)
            lines.append(f"[图片附件: {attachment.display_name}]")
            continue

        lines.append(f"[未识别内容块] {_serialize_unknown_block(item)}")

    return "\n".join(line for line in lines if line).strip()


def _render_message_block(
    message: ChatMessage,
    attachments: list[PreparedAttachment],
) -> str:
    role = _ROLE_LABELS.get(message.role, message.role)
    if message.role == "tool":
        content_text = _tool_message_content_to_text(message.content, attachments) or "(空内容)"
    else:
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

    attachments: list[PreparedAttachment] = []
    system_blocks: list[str] = []
    dialogue_blocks: list[str] = []
    upstream_messages: list[dict[str, Any]] = []

    for message in messages:
        upstream_messages.append(_build_upstream_message(message, attachments))
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
        messages=upstream_messages,
    )
