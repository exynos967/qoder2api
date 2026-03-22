from qoder2api.streaming import QoderStreamJSONParser


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
