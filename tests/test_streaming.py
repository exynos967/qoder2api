from qoder2api.streaming import QoderStreamJSONParser, merge_tool_call_deltas


def test_stream_json_parser_assistant_snapshot_diff() -> None:
    parser = QoderStreamJSONParser()

    first = parser.feed_event(
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "content": [{"type": "text", "text": "Hel"}],
            },
        }
    )
    second = parser.feed_event(
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "content": [{"type": "text", "text": "Hello"}],
            },
        }
    )

    assert first.text_delta == "Hel"
    assert second.text_delta == "lo"


def test_stream_json_parser_stream_event_text_delta() -> None:
    parser = QoderStreamJSONParser()

    start_event = parser.feed_event(
        {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {"id": "msg-2"},
            },
        }
    )
    first_delta = parser.feed_event(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "你"},
            },
        }
    )
    second_delta = parser.feed_event(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "好"},
            },
        }
    )

    assert start_event.text_delta == ""
    assert first_delta.text_delta == "你"
    assert second_delta.text_delta == "好"


def test_stream_json_parser_result_error() -> None:
    parser = QoderStreamJSONParser()
    parsed = parser.feed_event(
        {
            "type": "result",
            "is_error": True,
            "message": "request failed",
        }
    )

    assert parsed.error_message == "request failed"
    assert parsed.error_type == "api_error"


def test_stream_json_parser_openai_chunk_delta() -> None:
    parser = QoderStreamJSONParser()

    first = parser.feed_event(
        {
            "choices": [
                {
                    "delta": {"content": "你"},
                    "index": 0,
                }
            ],
            "object": "chat.completion.chunk",
        }
    )
    second = parser.feed_event(
        {
            "choices": [
                {
                    "delta": {"content": "好"},
                    "index": 0,
                    "finish_reason": "stop",
                }
            ],
            "object": "chat.completion.chunk",
        }
    )

    assert first.text_delta == "你"
    assert second.text_delta == "好"
    assert second.finish_reason == "stop"


def test_stream_json_parser_openai_chunk_tool_calls() -> None:
    parser = QoderStreamJSONParser()

    first = parser.feed_event(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    },
                    "index": 0,
                }
            ],
            "object": "chat.completion.chunk",
        }
    )
    second = parser.feed_event(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "type": "function",
                                "function": {"arguments": '{"city":"北京"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                    "index": 0,
                }
            ],
            "object": "chat.completion.chunk",
        }
    )

    assert first.tool_call_deltas == [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather"},
        }
    ]
    assert second.tool_call_deltas == [
        {
            "index": 0,
            "type": "function",
            "function": {"arguments": '{"city":"北京"}'},
        }
    ]
    assert second.finish_reason == "tool_calls"


def test_merge_tool_call_deltas_reconstructs_final_calls() -> None:
    merged = merge_tool_call_deltas(
        [],
        [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather"},
            }
        ],
    )
    merged = merge_tool_call_deltas(
        merged,
        [
            {
                "index": 0,
                "type": "function",
                "function": {"arguments": '{"city":"北京"}'},
            }
        ],
    )

    assert merged == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "get_weather",
                "arguments": '{"city":"北京"}',
            },
        }
    ]


def test_stream_json_parser_openai_chunk_message_content_fallback() -> None:
    parser = QoderStreamJSONParser()

    parsed = parser.feed_event(
        {
            "choices": [
                {
                    "message": {"content": "OK"},
                    "index": 0,
                    "finish_reason": "stop",
                }
            ],
            "object": "chat.completion",
        }
    )

    assert parsed.text_delta == "OK"
    assert parsed.finish_reason == "stop"


def test_stream_json_parser_openai_chunk_text_fallback() -> None:
    parser = QoderStreamJSONParser()

    parsed = parser.feed_event(
        {
            "choices": [
                {
                    "text": "GLM fallback",
                    "index": 0,
                    "finish_reason": "stop",
                }
            ],
            "object": "chat.completion",
        }
    )

    assert parsed.text_delta == "GLM fallback"
    assert parsed.finish_reason == "stop"
