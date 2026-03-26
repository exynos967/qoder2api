"""Anthropic Messages API 兼容层。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schemas import ChatCompletionRequest, ChatMessage
from .streaming import ParsedStreamEvent, merge_tool_call_deltas


class AnthropicMessageParam(BaseModel):
    """Anthropic messages[] 输入项。"""

    role: str
    content: str | list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="ignore")


class AnthropicMessagesRequest(BaseModel):
    """Anthropic /v1/messages 请求。"""

    model: str = "auto"
    max_tokens: int = Field(default=1024, ge=1)
    messages: list[AnthropicMessageParam] = Field(default_factory=list)
    system: str | list[dict[str, Any]] | None = None
    stream: bool | None = None
    temperature: float | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    stop_sequences: list[str] | None = None
    metadata: dict[str, Any] | None = None
    top_k: int | None = None
    top_p: float | None = None
    service_tier: str | None = None
    thinking: dict[str, Any] | None = None

    model_config = ConfigDict(extra="ignore")


@dataclass(slots=True)
class _ToolBlockState:
    content_index: int
    tool_id: str
    tool_name: str
    emitted_arguments_length: int = 0
    stopped: bool = False


@dataclass(slots=True)
class StopSequenceResult:
    text: str
    matched: str | None = None


_SCHEMA_MISSING = object()




def _normalize_stop_sequences(stop_sequences: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for item in stop_sequences or []:
        sequence = str(item).strip()
        if sequence and sequence not in normalized:
            normalized.append(sequence)
    return normalized


def _apply_stop_sequences(text: str, stop_sequences: list[str] | None) -> StopSequenceResult:
    if not text:
        return StopSequenceResult(text="")

    best_index: int | None = None
    best_sequence: str | None = None
    for sequence in _normalize_stop_sequences(stop_sequences):
        index = text.find(sequence)
        if index < 0:
            continue
        if best_index is None or index < best_index or (index == best_index and len(sequence) > len(best_sequence or "")):
            best_index = index
            best_sequence = sequence

    if best_index is None or best_sequence is None:
        return StopSequenceResult(text=text)
    return StopSequenceResult(text=text[:best_index], matched=best_sequence)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    ascii_count = max(len(text) - cjk_count, 0)
    return max(1, int(cjk_count / 1.5 + ascii_count / 4.0))


def _anthropic_sse_payload(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _block_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _normalize_content_blocks(content: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        stripped = content.strip()
        return [_block_text(stripped)] if stripped else []
    return list(content)


def _serialize_unknown_block(block: dict[str, Any]) -> dict[str, Any]:
    return _block_text(
        "[未识别 Anthropic 内容块] " + json.dumps(block, ensure_ascii=False, sort_keys=True)
    )


def _anthropic_image_block_to_openai(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source") or {}
    if not isinstance(source, dict):
        raise ValueError("Anthropic image block 缺少合法的 source")

    source_type = str(source.get("type") or "").strip().lower()
    if source_type == "base64":
        media_type = str(source.get("media_type") or "").strip().lower()
        data = str(source.get("data") or "").strip()
        if not media_type or not data:
            raise ValueError("Anthropic base64 图片缺少 media_type 或 data")
        image_url = f"data:{media_type};base64,{data}"
    elif source_type == "url":
        image_url = str(source.get("url") or "").strip()
        if not image_url:
            raise ValueError("Anthropic url 图片缺少 url")
    else:
        raise ValueError(f"不支持的 Anthropic 图片 source.type: {source_type or '<empty>'}")

    return {"type": "image_url", "image_url": {"url": image_url}}


def _content_blocks_to_openai_content(
    blocks: str | list[dict[str, Any]] | None,
) -> str | list[dict[str, Any]]:
    if isinstance(blocks, str):
        return blocks

    normalized_blocks: list[dict[str, Any]] = []
    for block in _normalize_content_blocks(blocks):
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            if text:
                normalized_blocks.append(_block_text(text))
            continue
        if block_type == "image":
            normalized_blocks.append(_anthropic_image_block_to_openai(block))
            continue
        normalized_blocks.append(_serialize_unknown_block(block))

    if not normalized_blocks:
        return ""
    if all(item.get("type") == "text" for item in normalized_blocks):
        return "\n".join(str(item.get("text") or "") for item in normalized_blocks).strip()
    return normalized_blocks


def _tool_result_content_to_openai_content(block: dict[str, Any]) -> str | list[dict[str, Any]]:
    content = _content_blocks_to_openai_content(block.get("content"))
    if not block.get("is_error"):
        return content

    error_prefix = "[tool_result error]"
    if isinstance(content, str):
        stripped = content.strip()
        return f"{error_prefix}\n{stripped}" if stripped else error_prefix

    return [_block_text(error_prefix), *content]


def _anthropic_tool_use_to_openai(block: dict[str, Any]) -> dict[str, Any]:
    name = str(block.get("name") or "").strip()
    if not name:
        raise ValueError("Anthropic tool_use 缺少 name")

    input_payload = block.get("input")
    if input_payload is None:
        input_payload = {}

    return {
        "id": str(block.get("id") or f"toolu_{uuid.uuid4().hex}"),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(input_payload, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _assistant_blocks_to_message(blocks: str | list[dict[str, Any]] | None) -> ChatMessage:
    content_blocks = _normalize_content_blocks(blocks)
    text_blocks: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []

    for block in content_blocks:
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            if text:
                text_blocks.append(_block_text(text))
            continue
        if block_type == "tool_use":
            tool_calls.append(_anthropic_tool_use_to_openai(block))
            continue
        if block_type in {"thinking", "redacted_thinking"}:
            continue
        text_blocks.append(_serialize_unknown_block(block))

    if not text_blocks and not tool_calls:
        return ChatMessage(role="assistant", content="")

    content: str | list[dict[str, Any]] | None
    if not text_blocks:
        content = ""
    elif len(text_blocks) == 1:
        content = text_blocks[0]["text"]
    else:
        content = text_blocks

    return ChatMessage(role="assistant", content=content, tool_calls=tool_calls or None)


def _user_blocks_to_messages(blocks: str | list[dict[str, Any]] | None) -> list[ChatMessage]:
    result: list[ChatMessage] = []
    pending_blocks: list[dict[str, Any]] = []

    def flush_pending() -> None:
        if not pending_blocks:
            return
        if len(pending_blocks) == 1 and pending_blocks[0].get("type") == "text":
            result.append(ChatMessage(role="user", content=str(pending_blocks[0].get("text") or "")))
        else:
            result.append(ChatMessage(role="user", content=list(pending_blocks)))
        pending_blocks.clear()

    for block in _normalize_content_blocks(blocks):
        block_type = str(block.get("type") or "").strip()
        if block_type == "tool_result":
            flush_pending()
            tool_use_id = str(block.get("tool_use_id") or "").strip()
            if not tool_use_id:
                raise ValueError("Anthropic tool_result 缺少 tool_use_id")
            result.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=tool_use_id,
                    content=_tool_result_content_to_openai_content(block),
                )
            )
            continue
        if block_type == "image":
            pending_blocks.append(_anthropic_image_block_to_openai(block))
            continue
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            if text:
                pending_blocks.append(_block_text(text))
            continue
        pending_blocks.append(_serialize_unknown_block(block))

    flush_pending()
    if not result:
        result.append(ChatMessage(role="user", content=""))
    return result


def _system_to_messages(system: str | list[dict[str, Any]] | None) -> list[ChatMessage]:
    if system is None:
        return []
    if isinstance(system, str):
        stripped = system.strip()
        return [ChatMessage(role="system", content=stripped)] if stripped else []

    content = _content_blocks_to_openai_content(system)
    if isinstance(content, str):
        return [ChatMessage(role="system", content=content)] if content else []
    return [ChatMessage(role="system", content=content)] if content else []


def _anthropic_tool_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    function_name = str(tool.get("name") or "").strip()
    if not function_name:
        raise ValueError("Anthropic tool 缺少 name")

    function_payload: dict[str, Any] = {
        "name": function_name,
        "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
    }
    description = str(tool.get("description") or "").strip()
    if description:
        function_payload["description"] = description
    if tool.get("strict") is True:
        function_payload["strict"] = True
    return {"type": "function", "function": function_payload}


def _build_tool_schema_map(tools: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    tool_schemas: dict[str, dict[str, Any]] = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        input_schema = tool.get("input_schema")
        if not name or not isinstance(input_schema, dict):
            continue
        tool_schemas[name] = input_schema
    return tool_schemas


def _parse_tool_arguments(raw_arguments: str) -> Any:
    stripped = raw_arguments.strip()
    if not stripped:
        return {}

    candidates = [stripped]
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            candidates.append("\n".join(lines[1:-1]).strip())
    first_object = stripped.find("{")
    last_object = stripped.rfind("}")
    if 0 <= first_object < last_object:
        candidates.append(stripped[first_object : last_object + 1].strip())

    seen: set[str] = set()
    while candidates:
        candidate = candidates.pop(0).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, str):
            nested = parsed.strip()
            if nested.startswith(("{", "[")) and nested not in seen:
                candidates.append(nested)
                continue
            return parsed
        return parsed

    return stripped


def _coerce_tool_value(value: Any, schema: dict[str, Any]) -> Any:
    schema_type = str(schema.get("type") or "").strip().lower()
    if not schema_type:
        return value

    if schema_type == "object":
        return _coerce_tool_object(value, schema)
    if schema_type == "string":
        if value is None:
            return _SCHEMA_MISSING
        if isinstance(value, (dict, list)):
            return _SCHEMA_MISSING
        return str(value)
    if schema_type == "integer":
        if isinstance(value, bool):
            return _SCHEMA_MISSING
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.lstrip("-").isdigit():
                return int(stripped)
        return _SCHEMA_MISSING
    if schema_type == "number":
        if isinstance(value, bool):
            return _SCHEMA_MISSING
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            try:
                return float(stripped)
            except ValueError:
                return _SCHEMA_MISSING
        return _SCHEMA_MISSING
    if schema_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            stripped = value.strip().lower()
            if stripped in {"true", "1", "yes", "y", "on"}:
                return True
            if stripped in {"false", "0", "no", "n", "off"}:
                return False
        return _SCHEMA_MISSING
    if schema_type == "array":
        item_schema = schema.get("items")
        normalized_items: list[Any] = []
        if isinstance(value, list):
            raw_items = value
        else:
            raw_items = [value]
        for item in raw_items:
            if isinstance(item_schema, dict):
                coerced = _coerce_tool_value(item, item_schema)
                if coerced is not _SCHEMA_MISSING:
                    normalized_items.append(coerced)
            else:
                normalized_items.append(item)
        return normalized_items
    return value


def _get_single_required_field_name(schema: dict[str, Any], schema_properties: dict[str, Any]) -> str | None:
    required_fields = schema.get("required")
    if not isinstance(required_fields, list):
        return None

    normalized_required = [
        str(field_name).strip()
        for field_name in required_fields
        if isinstance(field_name, str) and str(field_name).strip() in schema_properties
    ]
    if len(normalized_required) == 1:
        return normalized_required[0]
    return None



def _coerce_tool_object(value: Any, schema: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, str):
        value = _parse_tool_arguments(value)

    properties = schema.get("properties")
    schema_properties = properties if isinstance(properties, dict) else {}
    required_field_name = _get_single_required_field_name(schema, schema_properties)

    if not isinstance(value, dict):
        candidate_field_name = required_field_name
        if candidate_field_name is None and len(schema_properties) == 1:
            candidate_field_name = next(iter(schema_properties))
        if candidate_field_name is not None:
            field_schema = schema_properties.get(candidate_field_name)
            if isinstance(field_schema, dict):
                coerced = _coerce_tool_value(value, field_schema)
                if coerced is not _SCHEMA_MISSING:
                    return {candidate_field_name: coerced}
        return {}

    if not schema_properties:
        return dict(value)

    normalized: dict[str, Any] = {}
    for field_name, field_schema in schema_properties.items():
        if field_name not in value:
            continue
        if isinstance(field_schema, dict):
            coerced = _coerce_tool_value(value[field_name], field_schema)
            if coerced is not _SCHEMA_MISSING:
                normalized[field_name] = coerced
        else:
            normalized[field_name] = value[field_name]

    if not normalized and len(schema_properties) == 1:
        field_name, field_schema = next(iter(schema_properties.items()))
        if isinstance(field_schema, dict):
            for fallback_key in ("value", "input", "_arguments_text", "text"):
                if fallback_key not in value:
                    continue
                coerced = _coerce_tool_value(value[fallback_key], field_schema)
                if coerced is not _SCHEMA_MISSING:
                    normalized[field_name] = coerced
                    break

    additional_properties = schema.get("additionalProperties", True)
    if additional_properties is not False:
        for key, item in value.items():
            if key in normalized or key in schema_properties:
                continue
            normalized[key] = item

    return normalized


def _sanitize_tool_input(raw_arguments: str, input_schema: dict[str, Any] | None) -> dict[str, Any]:
    parsed_arguments = _parse_tool_arguments(raw_arguments)
    if not isinstance(input_schema, dict) or not input_schema:
        return parsed_arguments if isinstance(parsed_arguments, dict) else {}

    normalized = _coerce_tool_value(parsed_arguments, input_schema)
    return normalized if isinstance(normalized, dict) else {}


def _anthropic_tool_choice_to_openai(
    tool_choice: str | dict[str, Any] | None,
) -> str | dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return None

    choice_type = str(tool_choice.get("type") or "").strip().lower()
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        tool_name = str(tool_choice.get("name") or "").strip()
        if not tool_name:
            raise ValueError("Anthropic tool_choice=tool 缺少 name")
        return {"type": "function", "function": {"name": tool_name}}
    return tool_choice


def convert_anthropic_request_to_chat_request(
    request: AnthropicMessagesRequest,
) -> ChatCompletionRequest:
    messages: list[ChatMessage] = []
    messages.extend(_system_to_messages(request.system))

    for message in request.messages:
        if message.role == "assistant":
            messages.append(_assistant_blocks_to_message(message.content))
            continue
        if message.role == "user":
            messages.extend(_user_blocks_to_messages(message.content))
            continue
        raise ValueError(f"不支持的 Anthropic 消息角色: {message.role}")

    return ChatCompletionRequest(
        model=request.model,
        messages=messages,
        stream=request.stream,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        tools=[_anthropic_tool_to_openai(tool) for tool in request.tools or []] or None,
        tool_choice=_anthropic_tool_choice_to_openai(request.tool_choice),
    )


def _tool_call_to_anthropic_block(
    tool_call: dict[str, Any],
    tool_schemas: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    function_payload = tool_call.get("function") or {}
    function_name = str(function_payload.get("name") or "").strip()
    raw_arguments = str(function_payload.get("arguments") or "").strip()
    parsed_arguments = _sanitize_tool_input(
        raw_arguments,
        (tool_schemas or {}).get(function_name),
    )

    return {
        "type": "tool_use",
        "id": str(tool_call.get("id") or f"toolu_{uuid.uuid4().hex}"),
        "name": function_name,
        "input": parsed_arguments,
    }


def _map_finish_reason_to_stop_reason(
    finish_reason: str | None,
    *,
    has_tool_calls: bool,
) -> str:
    normalized = str(finish_reason or "").strip().lower()
    if normalized == "tool_calls" or has_tool_calls:
        return "tool_use"
    if normalized == "length":
        return "max_tokens"
    return "end_turn"


def build_anthropic_message_response(
    *,
    message_id: str,
    model: str,
    output_text: str,
    reasoning_text: str = "",
    reasoning_signature: str = "",
    tool_calls: list[dict[str, Any]] | None,
    finish_reason: str | None,
    input_tokens: int,
    output_tokens: int,
    stop_sequence: str | None = None,
    tool_schemas: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    normalized_reasoning = reasoning_text.strip()
    normalized_signature = reasoning_signature.strip()
    normalized_text = output_text.strip()
    if normalized_reasoning:
        thinking_block = {"type": "thinking", "thinking": normalized_reasoning}
        if normalized_signature:
            thinking_block["signature"] = normalized_signature
        content.append(thinking_block)
    if normalized_text:
        content.append({"type": "text", "text": normalized_text})
    for tool_call in tool_calls or []:
        content.append(_tool_call_to_anthropic_block(tool_call, tool_schemas))

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": (
            "stop_sequence"
            if stop_sequence
            else _map_finish_reason_to_stop_reason(
                finish_reason,
                has_tool_calls=bool(tool_calls),
            )
        ),
        "stop_sequence": stop_sequence,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


class AnthropicStreamTranslator:
    """把内部 ParsedStreamEvent 转成 Anthropic SSE。"""

    def __init__(
        self,
        *,
        message_id: str,
        prompt_text: str,
        stop_sequences: list[str] | None = None,
        tool_schemas: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.message_id = message_id
        self.prompt_tokens = _estimate_tokens(prompt_text)
        self.text_output = ""
        self.reasoning_output = ""
        self.reasoning_signature = ""
        self.tool_calls: list[dict[str, Any]] = []
        self.finish_reason: str | None = None
        self.stop_sequences = _normalize_stop_sequences(stop_sequences)
        self._pending_text = ""
        self.matched_stop_sequence: str | None = None
        self._model = "auto"
        self._thinking_block_index: int | None = None
        self._thinking_block_stopped = False
        self._text_block_index: int | None = None
        self._text_block_stopped = False
        self._tool_states: dict[int, _ToolBlockState] = {}
        self._next_content_index = 0
        self.tool_schemas = dict(tool_schemas or {})

    def start(self, *, model: str) -> list[str]:
        self._model = model
        message = {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": self.prompt_tokens,
                "output_tokens": 0,
            },
        }
        return [_anthropic_sse_payload("message_start", {"type": "message_start", "message": message})]

    def feed(self, stream_event: ParsedStreamEvent) -> list[str]:
        payloads: list[str] = []
        if self.matched_stop_sequence:
            return payloads
        if stream_event.reasoning_delta:
            payloads.extend(self._emit_reasoning_delta(stream_event.reasoning_delta))
        if stream_event.reasoning_signature_delta:
            payloads.extend(self._emit_reasoning_signature_delta(stream_event.reasoning_signature_delta))
        if stream_event.text_delta:
            payloads.extend(self._process_text_delta(stream_event.text_delta))
        if stream_event.tool_call_deltas and not self.matched_stop_sequence:
            payloads.extend(self._emit_tool_call_deltas(stream_event.tool_call_deltas))
        if stream_event.finish_reason:
            self.finish_reason = stream_event.finish_reason
        return payloads

    def finish(self) -> list[str]:
        payloads: list[str] = []
        payloads.extend(self._flush_remaining_text())
        if self.matched_stop_sequence:
            self.finish_reason = "stop_sequence"

        payloads.extend(self._flush_late_tool_blocks())
        payloads.extend(self._close_thinking_block())
        if self._text_block_index is not None and not self._text_block_stopped:
            payloads.append(
                _anthropic_sse_payload(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._text_block_index},
                )
            )
            self._text_block_stopped = True

        for state in self._tool_states.values():
            if state.stopped:
                continue
            payloads.extend(self._emit_pending_tool_arguments(state, final=True))
            payloads.append(
                _anthropic_sse_payload(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": state.content_index},
                )
            )
            state.stopped = True

        completion_basis_parts: list[str] = []
        if self.reasoning_output.strip():
            completion_basis_parts.append(self.reasoning_output.strip())
        if self.text_output.strip():
            completion_basis_parts.append(self.text_output.strip())
        if not completion_basis_parts and self.tool_calls:
            completion_basis_parts.append(json.dumps(self.tool_calls, ensure_ascii=False))
        output_tokens = _estimate_tokens("\n".join(completion_basis_parts))
        payloads.append(
            _anthropic_sse_payload(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": (
                            "stop_sequence"
                            if self.matched_stop_sequence
                            else _map_finish_reason_to_stop_reason(
                                self.finish_reason,
                                has_tool_calls=bool(self.tool_calls),
                            )
                        ),
                        "stop_sequence": self.matched_stop_sequence,
                    },
                    "usage": {"output_tokens": output_tokens},
                },
            )
        )
        payloads.append(_anthropic_sse_payload("message_stop", {"type": "message_stop"}))
        return payloads

    def error(self, *, message: str, error_type: str) -> list[str]:
        payload = {
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        }
        return [_anthropic_sse_payload("error", payload)]

    def _ensure_thinking_block_started(self) -> list[str]:
        if self._thinking_block_index is not None:
            return []
        self._thinking_block_index = self._next_content_index
        self._next_content_index += 1
        return [
            _anthropic_sse_payload(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._thinking_block_index,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
        ]

    def _emit_reasoning_delta(self, delta_text: str) -> list[str]:
        if not delta_text:
            return []
        payloads = self._ensure_thinking_block_started()
        self.reasoning_output += delta_text
        payloads.append(
            _anthropic_sse_payload(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._thinking_block_index,
                    "delta": {"type": "thinking_delta", "thinking": delta_text},
                },
            )
        )
        return payloads

    def _emit_reasoning_signature_delta(self, signature_text: str) -> list[str]:
        if not signature_text:
            return []
        payloads = self._ensure_thinking_block_started()
        self.reasoning_signature += signature_text
        payloads.append(
            _anthropic_sse_payload(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._thinking_block_index,
                    "delta": {"type": "signature_delta", "signature": signature_text},
                },
            )
        )
        return payloads

    def _close_thinking_block(self) -> list[str]:
        if self._thinking_block_index is None or self._thinking_block_stopped:
            return []
        self._thinking_block_stopped = True
        return [
            _anthropic_sse_payload(
                "content_block_stop",
                {"type": "content_block_stop", "index": self._thinking_block_index},
            )
        ]

    def _process_text_delta(self, delta_text: str) -> list[str]:
        self._pending_text += delta_text
        return self._drain_pending_text(final=False)

    def _flush_remaining_text(self) -> list[str]:
        return self._drain_pending_text(final=True)

    def _drain_pending_text(self, *, final: bool) -> list[str]:
        payloads: list[str] = []
        if not self._pending_text:
            return payloads

        stop_result = _apply_stop_sequences(self._pending_text, self.stop_sequences)
        if stop_result.matched is not None:
            if stop_result.text:
                payloads.extend(self._emit_text_delta(stop_result.text))
            self._pending_text = ""
            self.matched_stop_sequence = stop_result.matched
            return payloads

        if final:
            if self._pending_text:
                payloads.extend(self._emit_text_delta(self._pending_text))
                self._pending_text = ""
            return payloads

        holdback = max((len(item) - 1 for item in self.stop_sequences), default=0)
        if holdback <= 0 or len(self._pending_text) > holdback:
            emit_length = len(self._pending_text) - holdback if holdback > 0 else len(self._pending_text)
            emit_text = self._pending_text[:emit_length]
            self._pending_text = self._pending_text[emit_length:]
            if emit_text:
                payloads.extend(self._emit_text_delta(emit_text))
        return payloads

    def _emit_text_delta(self, delta_text: str) -> list[str]:
        payloads: list[str] = []
        payloads.extend(self._close_thinking_block())
        if self._text_block_index is None:
            self._text_block_index = self._next_content_index
            self._next_content_index += 1
            payloads.append(
                _anthropic_sse_payload(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self._text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        self.text_output += delta_text
        payloads.append(
            _anthropic_sse_payload(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._text_block_index,
                    "delta": {"type": "text_delta", "text": delta_text},
                },
            )
        )
        return payloads

    def _emit_tool_call_deltas(self, delta_tool_calls: list[dict[str, Any]]) -> list[str]:
        payloads: list[str] = []
        payloads.extend(self._close_thinking_block())
        if self._text_block_index is not None and not self._text_block_stopped:
            payloads.append(
                _anthropic_sse_payload(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._text_block_index},
                )
            )
            self._text_block_stopped = True
        self.tool_calls = merge_tool_call_deltas(self.tool_calls, delta_tool_calls)

        for item in delta_tool_calls:
            raw_index = item.get("index")
            if not isinstance(raw_index, int):
                continue
            state = self._tool_states.get(raw_index)
            merged_call = self.tool_calls[raw_index] if raw_index < len(self.tool_calls) else {}
            function_payload = merged_call.get("function") or {}
            tool_name = str(function_payload.get("name") or "").strip()
            tool_id = str(merged_call.get("id") or "").strip()

            if state is None:
                if not tool_name:
                    continue
                state = _ToolBlockState(
                    content_index=self._next_content_index,
                    tool_id=tool_id or f"toolu_{uuid.uuid4().hex}",
                    tool_name=tool_name,
                )
                self._tool_states[raw_index] = state
                self._next_content_index += 1
                payloads.append(
                    _anthropic_sse_payload(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": state.content_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": state.tool_id,
                                "name": state.tool_name,
                                "input": {},
                            },
                        },
                    )
                )
        return payloads

    def _emit_pending_tool_arguments(
        self,
        state: _ToolBlockState,
        *,
        final: bool = False,
    ) -> list[str]:
        payloads: list[str] = []
        tool_index = next(
            (index for index, tool_state in self._tool_states.items() if tool_state is state),
            None,
        )
        if tool_index is None or tool_index >= len(self.tool_calls):
            return payloads

        function_payload = self.tool_calls[tool_index].get("function") or {}
        if not final:
            return payloads

        tool_name = str(function_payload.get("name") or "").strip()
        arguments_text = json.dumps(
            _sanitize_tool_input(
                str(function_payload.get("arguments") or ""),
                self.tool_schemas.get(tool_name),
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(arguments_text) <= state.emitted_arguments_length:
            return payloads

        partial_json = arguments_text[state.emitted_arguments_length :]
        state.emitted_arguments_length = len(arguments_text)
        payloads.append(
            _anthropic_sse_payload(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": state.content_index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": partial_json,
                    },
                },
            )
        )
        return payloads

    def _flush_late_tool_blocks(self) -> list[str]:
        payloads: list[str] = []
        payloads.extend(self._close_thinking_block())
        if self._text_block_index is not None and not self._text_block_stopped:
            payloads.append(
                _anthropic_sse_payload(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._text_block_index},
                )
            )
            self._text_block_stopped = True
        for index, tool_call in enumerate(self.tool_calls):
            if index in self._tool_states:
                continue
            function_payload = tool_call.get("function") or {}
            tool_name = str(function_payload.get("name") or "").strip()
            if not tool_name:
                continue
            tool_id = str(tool_call.get("id") or f"toolu_{uuid.uuid4().hex}")
            state = _ToolBlockState(
                content_index=self._next_content_index,
                tool_id=tool_id,
                tool_name=tool_name,
            )
            self._tool_states[index] = state
            self._next_content_index += 1
            payloads.append(
                _anthropic_sse_payload(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": state.content_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": state.tool_id,
                            "name": state.tool_name,
                            "input": {},
                        },
                    },
                )
            )
            payloads.extend(self._emit_pending_tool_arguments(state, final=True))
        return payloads
