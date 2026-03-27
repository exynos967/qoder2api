from pathlib import Path


from qoder2api.prompting import render_openai_messages
from qoder2api.schemas import ChatMessage


class _FakeRemoteImageResponse:
    def __init__(self, *, content_type: str, chunks: list[bytes]) -> None:
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        yield from self._chunks



def test_render_openai_messages_basic() -> None:
    prepared = render_openai_messages(
        [
            ChatMessage(role="system", content="你是测试助手。"),
            ChatMessage(role="user", content="你好"),
            ChatMessage(role="assistant", content="你好，请讲。"),
            ChatMessage(role="user", content="只回复 ok"),
        ]
    )

    assert "=== 系统指令 ===" in prepared.prompt
    assert "[用户]\n你好" in prepared.prompt
    assert "[助手]\n你好，请讲。" in prepared.prompt
    assert "[用户]\n只回复 ok" in prepared.prompt
    assert prepared.attachments == []



def test_render_openai_messages_with_local_attachment(tmp_path: Path) -> None:
    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"fake-image")

    prepared = render_openai_messages(
        [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "帮我看图"},
                    {"type": "image_url", "image_url": {"url": str(image_path)}},
                ],
            )
        ]
    )

    assert "[图片附件: demo.png]" in prepared.prompt
    assert len(prepared.attachments) == 1
    attachment = prepared.attachments[0]
    assert attachment.display_name == "demo.png"
    assert attachment.local_path == image_path.resolve()
    assert attachment.url.startswith("data:image/png;base64,")



def test_render_openai_messages_accepts_data_url_image() -> None:
    prepared = render_openai_messages(
        [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "帮我看图"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
                        },
                    },
                ],
            )
        ]
    )

    assert "[图片附件: inline-image.png]" in prepared.prompt
    assert len(prepared.attachments) == 1
    attachment = prepared.attachments[0]
    assert attachment.local_path is None
    assert attachment.display_name == "inline-image.png"
    assert attachment.url.startswith("data:image/png;base64,")



def test_render_openai_messages_downloads_http_image(monkeypatch) -> None:
    def _fake_stream(method: str, url: str, **_: object) -> _FakeRemoteImageResponse:
        assert method == "GET"
        assert url == "https://example.com/demo.png"
        return _FakeRemoteImageResponse(
            content_type="image/png",
            chunks=[b"fake", b"-image"],
        )

    monkeypatch.setattr("qoder2api.prompting.httpx.stream", _fake_stream)

    prepared = render_openai_messages(
        [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "帮我看图"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/demo.png"}},
                ],
            )
        ]
    )

    assert "[图片附件: demo.png]" in prepared.prompt
    assert len(prepared.attachments) == 1
    attachment = prepared.attachments[0]
    assert attachment.local_path is None
    assert attachment.display_name == "demo.png"
    assert attachment.url.startswith("data:image/png;base64,")



def test_render_openai_messages_builds_structured_tool_history() -> None:
    prepared = render_openai_messages(
        [
            ChatMessage(role="user", content="请调用工具"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                    }
                ],
            ),
            ChatMessage(
                role="tool",
                tool_call_id="call_1",
                content='{"city":"北京","weather":"晴"}',
            ),
        ]
    )

    assert prepared.messages == [
        {"role": "user", "content": "请调用工具"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"city":"北京","weather":"晴"}',
        },
    ]
