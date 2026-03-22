"""Qoder 流式事件解析。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _join_text_blocks(value: Any) -> str:
    """从 message/content/delta 结构中提取纯文本。"""
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
    if block_type == "thinking_delta":
        return _as_text(value.get("thinking"))

    if isinstance(value.get("text"), str):
        return _as_text(value.get("text"))

    for key in ("content", "message", "delta", "result", "data"):
        if key in value:
            extracted = _join_text_blocks(value[key])
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


@dataclass(slots=True)
class ParsedStreamEvent:
    """解析后的单条 stream-json 事件。"""

    raw: dict[str, Any] = field(default_factory=dict)
    text_delta: str = ""
    finish_reason: str | None = None
    error_message: str = ""
    error_type: str = "api_error"


class QoderStreamJSONParser:
    """把 Qoder 流式 JSON 事件流转换为增量文本。"""

    def __init__(self) -> None:
        self._current_message_id = "__default__"
        self._message_snapshots: dict[str, str] = {}

    def _append_delta(self, message_id: str, delta_text: str) -> str:
        if not delta_text:
            return ""
        previous = self._message_snapshots.get(message_id, "")
        self._message_snapshots[message_id] = previous + delta_text
        return delta_text

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
            event.text_delta = self._diff_snapshot(message_id, snapshot_text)
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

            result_text = _join_text_blocks(payload.get("result"))
            if result_text:
                event.text_delta = self._diff_snapshot(message_id, result_text)

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
            snapshot_text = _join_text_blocks(stream_payload.get("message"))
            event.text_delta = self._diff_snapshot(nested_message_id, snapshot_text)
            return event

        if subtype == "content_block_start":
            content_block = stream_payload.get("content_block") or stream_payload.get("content")
            snapshot_text = _join_text_blocks(content_block)
            event.text_delta = self._diff_snapshot(message_id, snapshot_text)
            return event

        if subtype == "content_block_delta":
            delta_payload = stream_payload.get("delta")
            event.text_delta = self._append_delta(message_id, _join_text_blocks(delta_payload))
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
        event.text_delta = self._append_delta(message_id, _join_text_blocks(delta_payload))
        event.finish_reason = _normalize_finish_reason(first_choice.get("finish_reason"))
        return event
