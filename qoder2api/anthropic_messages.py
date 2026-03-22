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
    stream: bool = False
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


def _tool_call_to_anthropic_block(tool_call: dict[str, Any]) -> dict[str, Any]:
    function_payload = tool_call.get("function") or {}
    raw_arguments = str(function_payload.get("arguments") or "").strip()
    if raw_arguments:
        try:
            parsed_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            parsed_arguments = {"_arguments_text": raw_arguments}
    else:
        parsed_arguments = {}

    return {
        "type": "tool_use",
        "id": str(tool_call.get("id") or f"toolu_{uuid.uuid4().hex}"),
        "name": str(function_payload.get("name") or "").strip(),
        "input": parsed_arguments if isinstance(parsed_arguments, dict) else {"value": parsed_arguments},
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
    tool_calls: list[dict[str, Any]] | None,
    finish_reason: str | None,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    normalized_text = output_text.strip()
    if normalized_text:
        content.append({"type": "text", "text": normalized_text})
    for tool_call in tool_calls or []:
        content.append(_tool_call_to_anthropic_block(tool_call))

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": _map_finish_reason_to_stop_reason(
            finish_reason,
            has_tool_calls=bool(tool_calls),
        ),
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


class AnthropicStreamTranslator:
    """把内部 ParsedStreamEvent 转成 Anthropic SSE。"""

    def __init__(self, *, message_id: str, prompt_text: str) -> None:
        self.message_id = message_id
        self.prompt_tokens = _estimate_tokens(prompt_text)
        self.text_output = ""
        self.tool_calls: list[dict[str, Any]] = []
        self.finish_reason: str | None = None
        self._model = "auto"
        self._text_block_index: int | None = None
        self._text_block_stopped = False
        self._tool_states: dict[int, _ToolBlockState] = {}
        self._next_content_index = 0

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
        if stream_event.text_delta:
            payloads.extend(self._emit_text_delta(stream_event.text_delta))
        if stream_event.tool_call_deltas:
            payloads.extend(self._emit_tool_call_deltas(stream_event.tool_call_deltas))
        if stream_event.finish_reason:
            self.finish_reason = stream_event.finish_reason
        return payloads

    def finish(self) -> list[str]:
        payloads: list[str] = []
        payloads.extend(self._flush_late_tool_blocks())
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
            payloads.extend(self._emit_pending_tool_arguments(state))
            payloads.append(
                _anthropic_sse_payload(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": state.content_index},
                )
            )
            state.stopped = True

        completion_basis = self.text_output.strip()
        if not completion_basis and self.tool_calls:
            completion_basis = json.dumps(self.tool_calls, ensure_ascii=False)
        output_tokens = _estimate_tokens(completion_basis)
        payloads.append(
            _anthropic_sse_payload(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _map_finish_reason_to_stop_reason(
                            self.finish_reason,
                            has_tool_calls=bool(self.tool_calls),
                        ),
                        "stop_sequence": None,
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

    def _emit_text_delta(self, delta_text: str) -> list[str]:
        payloads: list[str] = []
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

            payloads.extend(self._emit_pending_tool_arguments(state))
        return payloads

    def _emit_pending_tool_arguments(self, state: _ToolBlockState) -> list[str]:
        payloads: list[str] = []
        tool_index = next(
            (index for index, tool_state in self._tool_states.items() if tool_state is state),
            None,
        )
        if tool_index is None or tool_index >= len(self.tool_calls):
            return payloads

        function_payload = self.tool_calls[tool_index].get("function") or {}
        arguments_text = str(function_payload.get("arguments") or "")
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
            payloads.extend(self._emit_pending_tool_arguments(state))
        return payloads
