"""论文:PDF 存在工作区 .autolab/papers/,agent 按页读取文本。"""

from __future__ import annotations

import io

from ..core.errors import UserError
from .fs import Workspace

PAPERS_REL = ".autolab/papers"


def _safe_name(filename: str) -> str:
    name = (filename or "paper.pdf").replace("/", "_").replace("..", "_").strip()
    return name or "paper.pdf"


def save_paper(ws: Workspace, filename: str, content: bytes) -> str:
    if not content:
        raise UserError("上传内容为空")
    rel = f"{PAPERS_REL}/{_safe_name(filename)}"
    ws.write_bytes(rel, content)
    return ws.abs(rel)


def list_papers(ws: Workspace) -> list[str]:
    return sorted(name for name in ws.listdir(PAPERS_REL) if not name.startswith("."))


def read_pages(ws: Workspace, filename: str, first: int = 1, last: int | None = None) -> str:
    from pypdf import PdfReader

    data = ws.read_bytes(f"{PAPERS_REL}/{_safe_name(filename)}")
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    first = max(1, first)
    last = min(total, last or first + 7)
    parts = []
    for index in range(first - 1, last):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页提取失败不影响其他页
            text = "(提取失败)"
        parts.append(f"--- 第 {index + 1} 页 ---\n{text}")
    return f"共 {total} 页\n" + "\n".join(parts)
