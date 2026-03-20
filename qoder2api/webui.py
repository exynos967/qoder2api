"""WebUI 静态资源与辅助函数。"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any


def get_webui_static_dir() -> Path:
    """返回内置 WebUI 静态目录。"""
    return Path(__file__).resolve().parent / 'static' / 'webui'


def get_webui_index_path() -> Path:
    """返回 WebUI 入口文件。"""
    return get_webui_static_dir() / 'index.html'


def tail_text_file(path: Path, *, lines: int = 200) -> str:
    """读取文本文件末尾若干行。"""
    if lines <= 0:
        return ''

    if not path.is_file():
        return ''

    recent_lines: deque[str] = deque(maxlen=lines)
    with path.open('r', encoding='utf-8', errors='replace') as handle:
        for line in handle:
            recent_lines.append(line)
    return ''.join(recent_lines).rstrip()


def build_logs_payload(path: Path, *, lines: int = 200) -> dict[str, Any]:
    """构造日志接口响应。"""
    return {
        'path': str(path),
        'exists': path.is_file(),
        'lines': lines,
        'content': tail_text_file(path, lines=lines),
    }
