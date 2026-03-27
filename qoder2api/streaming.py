"""Qoder 流式事件解析。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


ToolCallDelta = dict[str, Any]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _join_text_blocks(value: Any) -> str:
    """从 message/content/delta 结构中提取可见正文。"""
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, list):
        return "".join(_join_text_blocks(item) for item in value)

    if not isinstance(value, dict):
        return ""

    block_type = _as_text(value.get("type")).strip()
    if block_type in {"text", "text_delta", "input_text", "output_text"}:
        return _as_text(value.get("text"))
    if block_type in {"thinking", "thinking_delta", "redacted_thinking", "signature_delta"}:
        return ""

    if isinstance(value.get("text"), str):
        return _as_text(value.get("text"))

    for key in ("content", "message", "delta", "result", "data"):
        if key in value:
            extracted = _join_text_blocks(value[key])
            if extracted:
                return extracted

    return ""


def _join_reasoning_blocks(value: Any) -> str:
    """从 message/content/delta 结构中提取 thinking/reasoning 文本。"""
    if value is None:
        return ""

    if isinstance(value, list):
        return "".join(_join_reasoning_blocks(item) for item in value)

    if not isinstance(value, dict):
        return ""

    block_type = _as_text(value.get("type")).strip()
    if block_type in {"thinking", "thinking_delta"}:
        return _as_text(value.get("thinking") or value.get("text"))
    if block_type in {"redacted_thinking", "signature_delta"}:
        return ""

    for key in ("reasoning_content", "reasoning", "thinking"):
        direct_value = value.get(key)
        if isinstance(direct_value, str):
            return _as_text(direct_value)
        extracted = _join_reasoning_blocks(direct_value)
        if extracted:
            return extracted

    for key in ("content", "message", "delta", "result", "data"):
        if key in value:
            extracted = _join_reasoning_blocks(value[key])
            if extracted:
                return extracted

    return ""


def _extract_reasoning_signature(value: Any) -> str:
    """提取 Anthropic thinking block 的 signature。"""
    if value is None:
        return ""

    if isinstance(value, list):
        return "".join(_extract_reasoning_signature(item) for item in value)

    if not isinstance(value, dict):
        return ""

    block_type = _as_text(value.get("type")).strip()
    if block_type in {"thinking", "signature_delta"}:
        return _as_text(value.get("signature"))

    if isinstance(value.get("signature"), str):
        return _as_text(value.get("signature"))

    for key in ("content", "message", "delta", "result", "data"):
        if key in value:
            extracted = _extract_reasoning_signature(value[key])
            if extracted:
                return extracted

    return ""


def _normalize_finish_reason(raw_value: Any) -> str | None:
    value = _as_text(raw_value).strip().lower()
    if not value:
        return None

    if value in {"stop", "end_turn", "completed", "success"}:
        return "stop"
    if value in {"length", "max_tokens"}:
        return "length"
    if value in {"tool_calls", "function_call"}:
        return "tool_calls"

    return "stop"


def _extract_message_id(payload: dict[str, Any], current_message_id: str) -> str:
    """尽量从事件中拿到 assistant message id。"""
    candidates = [
        payload.get("assistantMsgId"),
        payload.get("assistant_id"),
        payload.get("id"),
    ]

    message_payload = payload.get("message")
    if isinstance(message_payload, dict):
        candidates.append(message_payload.get("id"))

    event_payload = payload.get("event")
    if isinstance(event_payload, dict):
        candidates.extend(
            [
                event_payload.get("assistantMsgId"),
                event_payload.get("assistant_id"),
                event_payload.get("id"),
            ]
        )
        nested_message = event_payload.get("message")
        if isinstance(nested_message, dict):
            candidates.append(nested_message.get("id"))

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    return current_message_id


def _normalize_tool_call_delta_item(item: Any) -> ToolCallDelta | None:
    if not isinstance(item, dict):
        return None

    normalized: ToolCallDelta = {}
    index = item.get("index")
    if isinstance(index, (int, float)):
        normalized["index"] = int(index)
    elif isinstance(index, str) and index.strip().isdigit():
        normalized["index"] = int(index.strip())

    tool_id = _as_text(item.get("id")).strip()
    if tool_id:
        normalized["id"] = tool_id

    tool_type = _as_text(item.get("type")).strip()
    if tool_type:
        normalized["type"] = tool_type

    function_payload = item.get("function")
    if isinstance(function_payload, dict):
        normalized_function: dict[str, str] = {}
        function_name = _as_text(function_payload.get("name"))
        if function_name:
            normalized_function["name"] = function_name
        function_arguments = _as_text(function_payload.get("arguments"))
        if function_arguments:
            normalized_function["arguments"] = function_arguments
        if normalized_function:
            normalized["function"] = normalized_function

    return normalized or None


def _extract_tool_call_deltas(delta_payload: Any) -> list[ToolCallDelta]:
    if not isinstance(delta_payload, dict):
        return []

    raw_tool_calls = delta_payload.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    normalized: list[ToolCallDelta] = []
    for item in raw_tool_calls:
        tool_call = _normalize_tool_call_delta_item(item)
        if tool_call is not None:
            normalized.append(tool_call)
    return normalized


def _extract_openai_chunk_text(choice: dict[str, Any]) -> str:
    delta_payload = choice.get("delta")
    text = _join_text_blocks(delta_payload)
    if text:
        return text

    for key in ("message", "content", "text", "output_text", "response"):
        if key in choice:
            text = _join_text_blocks(choice.get(key))
            if text:
                return text

    return ""


def _extract_openai_chunk_reasoning(choice: dict[str, Any]) -> str:
    delta_payload = choice.get("delta")
    reasoning = _join_reasoning_blocks(delta_payload)
    if reasoning:
        return reasoning

    for key in ("reasoning_content", "reasoning", "thinking", "message", "content", "response"):
        if key in choice:
            reasoning = _join_reasoning_blocks(choice.get(key))
            if reasoning:
                return reasoning
            direct_value = choice.get(key)
            if isinstance(direct_value, str):
                return direct_value

    return ""


def _extract_openai_chunk_reasoning_signature(choice: dict[str, Any]) -> str:
    delta_payload = choice.get("delta")
    signature = _extract_reasoning_signature(delta_payload)
    if signature:
        return signature

    for key in ("message", "content", "response"):
        if key in choice:
            signature = _extract_reasoning_signature(choice.get(key))
            if signature:
                return signature

    return ""


def _merge_chunk_text(current: str, chunk: str) -> str:
    if not chunk:
        return current
    if not current:
        return chunk
    return current + chunk


def merge_tool_call_deltas(
    current_tool_calls: list[ToolCallDelta],
    delta_tool_calls: list[ToolCallDelta],
) -> list[ToolCallDelta]:
    """把流式 tool_call 增量合并成 OpenAI 最终结构。"""
    merged: dict[int, ToolCallDelta] = {}

    for index, item in enumerate(current_tool_calls):
        key = int(item.get("index", index))
        merged[key] = {
            "index": key,
            "id": _as_text(item.get("id")).strip(),
            "type": _as_text(item.get("type")).strip() or "function",
            "function": {
                "name": _as_text((item.get("function") or {}).get("name")).strip(),
                "arguments": _as_text((item.get("function") or {}).get("arguments")),
            },
        }

    next_index = len(merged)
    for item in delta_tool_calls:
        key = int(item.get("index", next_index))
        next_index = max(next_index, key + 1)
        target = merged.setdefault(
            key,
            {
                "index": key,
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        tool_id = _as_text(item.get("id")).strip()
        if tool_id:
            target["id"] = _merge_chunk_text(_as_text(target.get("id")), tool_id)

        tool_type = _as_text(item.get("type")).strip()
        if tool_type:
            target["type"] = tool_type

        function_payload = item.get("function") or {}
        if isinstance(function_payload, dict):
            target_function = target.setdefault("function", {"name": "", "arguments": ""})
            function_name = _as_text(function_payload.get("name"))
            if function_name:
                target_function["name"] = _merge_chunk_text(
                    _as_text(target_function.get("name")),
                    function_name,
                )
            function_arguments = _as_text(function_payload.get("arguments"))
            if function_arguments:
                target_function["arguments"] = _merge_chunk_text(
                    _as_text(target_function.get("arguments")),
                    function_arguments,
                )

    finalized: list[ToolCallDelta] = []
    for key in sorted(merged):
        item = merged[key]
        finalized.append(
            {
                "id": _as_text(item.get("id")).strip(),
                "type": _as_text(item.get("type")).strip() or "function",
                "function": {
                    "name": _as_text((item.get("function") or {}).get("name")).strip(),
                    "arguments": _as_text((item.get("function") or {}).get("arguments")),
                },
            }
        )
    return finalized


@dataclass(slots=True)
class ParsedStreamEvent:
    """解析后的单条 stream-json 事件。"""

    raw: dict[str, Any] = field(default_factory=dict)
    text_delta: str = ""
    reasoning_delta: str = ""
    reasoning_signature_delta: str = ""
    tool_call_deltas: list[ToolCallDelta] = field(default_factory=list)
    finish_reason: str | None = None
    error_message: str = ""
    error_type: str = "api_error"


class QoderStreamJSONParser:
    """把 Qoder 流式 JSON 事件流转换为增量文本。"""

    def __init__(self) -> None:
        self._current_message_id = "__default__"
        self._message_snapshots: dict[str, str] = {}
        self._reasoning_snapshots: dict[str, str] = {}
        self._reasoning_signatures: dict[str, str] = {}
        # Anthropic stream_event 中的 tool_use 状态跟踪
        # key: content_block index, value: {id, name, arguments, tool_index}
        self._tool_block_states: dict[int, dict[str, Any]] = {}
        self._next_tool_index: int = 0

    def _append_delta(self, message_id: str, delta_text: str) -> str:
        if not delta_text:
            return ""
        previous = self._message_snapshots.get(message_id, "")
        self._message_snapshots[message_id] = previous + delta_text
        return delta_text

    def _append_reasoning_delta(self, message_id: str, delta_text: str) -> str:
        if not delta_text:
            return ""
        previous = self._reasoning_snapshots.get(message_id, "")
        self._reasoning_snapshots[message_id] = previous + delta_text
        return delta_text

    def _append_reasoning_signature(self, message_id: str, signature_text: str) -> str:
        if not signature_text:
            return ""
        previous = self._reasoning_signatures.get(message_id, "")
        self._reasoning_signatures[message_id] = previous + signature_text
        return signature_text

    def _diff_snapshot(self, message_id: str, snapshot_text: str) -> str:
        if not snapshot_text:
            return ""

        previous = self._message_snapshots.get(message_id, "")
        self._message_snapshots[message_id] = snapshot_text

        if snapshot_text == previous:
            return ""
        if snapshot_text.startswith(previous):
            return snapshot_text[len(previous) :]
        return snapshot_text

    def _diff_reasoning_snapshot(self, message_id: str, snapshot_text: str) -> str:
        if not snapshot_text:
            return ""

        previous = self._reasoning_snapshots.get(message_id, "")
        self._reasoning_snapshots[message_id] = snapshot_text

        if snapshot_text == previous:
            return ""
        if snapshot_text.startswith(previous):
            return snapshot_text[len(previous) :]
        return snapshot_text

    def _diff_reasoning_signature(self, message_id: str, signature_text: str) -> str:
        if not signature_text:
            return ""

        previous = self._reasoning_signatures.get(message_id, "")
        self._reasoning_signatures[message_id] = signature_text

        if signature_text == previous:
            return ""
        if signature_text.startswith(previous):
            return signature_text[len(previous) :]
        return signature_text

    def feed_line(self, line: str) -> ParsedStreamEvent | None:
        stripped = line.strip()
        if not stripped:
            return None

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            message_id = self._current_message_id
            return ParsedStreamEvent(
                raw={"type": "assistant", "fallback_text": stripped},
                text_delta=self._append_delta(message_id, stripped),
            )

        return self.feed_event(payload)

    def feed_event(self, payload: dict[str, Any]) -> ParsedStreamEvent:
        event = ParsedStreamEvent(raw=payload)
        event_type = _as_text(payload.get("type")).strip()
        message_id = _extract_message_id(payload, self._current_message_id)
        self._current_message_id = message_id

        if event_type == "stream_event":
            return self._handle_stream_event(payload, message_id)

        if isinstance(payload.get("choices"), list):
            return self._handle_openai_chunk(payload, message_id)

        if event_type == "assistant":
            message_payload = payload.get("message") if isinstance(payload.get("message"), dict) else payload
            snapshot_text = _join_text_blocks(message_payload)
            snapshot_reasoning = _join_reasoning_blocks(message_payload)
            signature_text = _extract_reasoning_signature(message_payload)
            event.text_delta = self._diff_snapshot(message_id, snapshot_text)
            event.reasoning_delta = self._diff_reasoning_snapshot(message_id, snapshot_reasoning)
            event.reasoning_signature_delta = self._diff_reasoning_signature(message_id, signature_text)
            return event

        if event_type == "result":
            if payload.get("is_error"):
                event.error_message = (
                    _as_text(payload.get("error"))
                    or _as_text(payload.get("message"))
                    or _as_text(payload.get("result"))
                    or "Qoder 返回错误结果"
                ).strip()
                event.error_type = "api_error"
                return event

            result_payload = payload.get("result")
            result_text = _join_text_blocks(result_payload)
            reasoning_text = _join_reasoning_blocks(result_payload)
            signature_text = _extract_reasoning_signature(result_payload)
            if result_text:
                event.text_delta = self._diff_snapshot(message_id, result_text)
            if reasoning_text:
                event.reasoning_delta = self._diff_reasoning_snapshot(message_id, reasoning_text)
            if signature_text:
                event.reasoning_signature_delta = self._diff_reasoning_signature(
                    message_id,
                    signature_text,
                )

            event.finish_reason = _normalize_finish_reason(
                payload.get("stop_reason")
                or payload.get("final_finish_reason")
                or payload.get("subtype")
            )
            return event

        if event_type == "error":
            event.error_message = (
                _as_text(payload.get("message"))
                or _as_text(payload.get("error"))
                or "Qoder 输出了错误事件"
            ).strip()
            return event

        return event

    def _handle_stream_event(self, payload: dict[str, Any], message_id: str) -> ParsedStreamEvent:
        event = ParsedStreamEvent(raw=payload)
        stream_payload = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        subtype = _as_text(stream_payload.get("type") or payload.get("subtype")).strip().lower()

        if subtype == "message_start":
            nested_message_id = _extract_message_id(stream_payload, message_id)
            self._current_message_id = nested_message_id
            message_payload = stream_payload.get("message")
            snapshot_text = _join_text_blocks(message_payload)
            snapshot_reasoning = _join_reasoning_blocks(message_payload)
            signature_text = _extract_reasoning_signature(message_payload)
            event.text_delta = self._diff_snapshot(nested_message_id, snapshot_text)
            event.reasoning_delta = self._diff_reasoning_snapshot(nested_message_id, snapshot_reasoning)
            event.reasoning_signature_delta = self._diff_reasoning_signature(
                nested_message_id,
                signature_text,
            )
            return event

        if subtype == "content_block_start":
            content_block = stream_payload.get("content_block") or stream_payload.get("content")
            if isinstance(content_block, dict):
                block_type = _as_text(content_block.get("type")).strip().lower()
                if block_type == "tool_use":
                    # Anthropic 风格的 tool_use content_block_start
                    block_index = stream_payload.get("index")
                    if isinstance(block_index, int):
                        tool_id = _as_text(content_block.get("id")).strip()
                        tool_name = _as_text(content_block.get("name")).strip()
                        tool_index = self._next_tool_index
                        self._next_tool_index += 1
                        self._tool_block_states[block_index] = {
                            "id": tool_id,
                            "name": tool_name,
                            "arguments": "",
                            "tool_index": tool_index,
                        }
                        # 构造 OpenAI 风格的 tool_call_delta（用 tool_index 而非 block_index）
                        event.tool_call_deltas = [
                            {
                                "index": tool_index,
                                "id": tool_id,
                                "type": "function",
                                "function": {"name": tool_name, "arguments": ""},
                            }
                        ]
                    return event
            snapshot_text = _join_text_blocks(content_block)
            snapshot_reasoning = _join_reasoning_blocks(content_block)
            signature_text = _extract_reasoning_signature(content_block)
            event.text_delta = self._diff_snapshot(message_id, snapshot_text)
            event.reasoning_delta = self._diff_reasoning_snapshot(message_id, snapshot_reasoning)
            event.reasoning_signature_delta = self._diff_reasoning_signature(message_id, signature_text)
            return event

        if subtype == "content_block_delta":
            delta_payload = stream_payload.get("delta")
            if isinstance(delta_payload, dict):
                delta_type = _as_text(delta_payload.get("type")).strip().lower()
                if delta_type == "input_json_delta":
                    # Anthropic 风格的 tool_use input delta
                    block_index = stream_payload.get("index")
                    partial_json = _as_text(delta_payload.get("partial_json"))
                    if isinstance(block_index, int) and partial_json:
                        state = self._tool_block_states.get(block_index)
                        if state is not None:
                            state["arguments"] += partial_json
                            # 构造 OpenAI 风格的 tool_call_delta（用 tool_index 而非 block_index）
                            event.tool_call_deltas = [
                                {
                                    "index": state["tool_index"],
                                    "function": {"arguments": partial_json},
                                }
                            ]
                    return event
            event.text_delta = self._append_delta(message_id, _join_text_blocks(delta_payload))
            event.reasoning_delta = self._append_reasoning_delta(
                message_id,
                _join_reasoning_blocks(delta_payload),
            )
            event.reasoning_signature_delta = self._append_reasoning_signature(
                message_id,
                _extract_reasoning_signature(delta_payload),
            )
            return event

        if subtype == "message_delta":
            delta_payload = stream_payload.get("delta")
            event.finish_reason = _normalize_finish_reason(
                (delta_payload or {}).get("stop_reason") if isinstance(delta_payload, dict) else None
            )
            return event

        if subtype == "message_stop":
            event.finish_reason = "stop"
            return event

        return event

    def _handle_openai_chunk(
        self,
        payload: dict[str, Any],
        message_id: str,
    ) -> ParsedStreamEvent:
        event = ParsedStreamEvent(raw=payload)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return event

        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        delta_payload = first_choice.get("delta")
        event.text_delta = self._append_delta(message_id, _extract_openai_chunk_text(first_choice))
        event.reasoning_delta = self._append_reasoning_delta(
            message_id,
            _extract_openai_chunk_reasoning(first_choice),
        )
        event.reasoning_signature_delta = self._append_reasoning_signature(
            message_id,
            _extract_openai_chunk_reasoning_signature(first_choice),
        )
        event.tool_call_deltas = _extract_tool_call_deltas(delta_payload)
        event.finish_reason = _normalize_finish_reason(
            first_choice.get("finish_reason") or payload.get("finish_reason")
        )
        return event
