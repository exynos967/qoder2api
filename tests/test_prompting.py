from pathlib import Path

from qoder2api.prompting import render_openai_messages
from qoder2api.schemas import ChatMessage


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
    assert prepared.attachments == [image_path.resolve()]

