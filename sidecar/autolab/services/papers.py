"""论文登记与解析（D17：链接优先抓取 + 上传兜底；D42：文献对照证据）。

- arXiv 链接：export.arxiv.org API 取元数据 + PDF 下载；DOI/其他 URL 尽力抓
  （服务器无出网不影响控制机——抓取发生在控制机）
- 解析：pypdf 抽取全文（截断存 parsed_json：title/abstract/正文摘要段）
- 文献对照（M3-lite）：LLM + 已导入论文摘要集合（paper-qa RAG 后置，deviation #26）
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx

from ..db import to_thread
from ..errors import TxError
from ..ids import nid

ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(v[0-9]+)?", re.I)
DOI = re.compile(r"^10\.\d{4,9}/\S+$")


async def ingest_url(state, pid: str, url: str) -> dict:
    """链接登记：arXiv 优先（元数据+PDF），其余 URL 尝试直接下载 PDF。"""
    m = ARXIV_ID.search(url)
    meta: dict = {"url": url}
    pdf_bytes: bytes | None = None
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        if m:
            aid = m.group(1)
            r = await c.get("https://export.arxiv.org/api/query",
                            params={"id_list": aid})
            if r.status_code == 200:
                meta.update(_parse_arxiv_atom(r.text))
                pdf_url = meta.get("pdf_url") or f"https://arxiv.org/pdf/{aid}"
                pr = await c.get(pdf_url)
                if pr.status_code == 200 and pr.content[:4] == b"%PDF":
                    pdf_bytes = pr.content
        else:
            r = await c.get(url, headers={"User-Agent": "AutoLab/0.1"})
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                pdf_bytes = r.content
                meta["title"] = _guess_title_from_url(url)
    if pdf_bytes is None:
        raise TxError("GUARD_UNMET",
                      f"无法抓取 PDF（{url}）——付费墙/非 PDF？请用本地上传兜底（D17）")

    paper_dir = state.cfg.projects_dir / pid / "papers"
    paper_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(pdf_bytes).hexdigest()[:16]
    fname = f"paper-{sha}.pdf"
    (paper_dir / fname).write_bytes(pdf_bytes)
    parsed = _extract_pdf(paper_dir / fname, meta)

    def _ins(conn):
        paper_id = nid("paper")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO papers(id,project_id,source_kind,url,file_path,file_sha256,"
                "title,metadata_json,parsed_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (paper_id, pid, "url", url, str(paper_dir / fname), sha,
                 meta.get("title") or parsed.get("title") or url,
                 json.dumps(meta, ensure_ascii=False),
                 json.dumps(parsed, ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return paper_id
    paper_id = await to_thread(state.db, _ins)
    return {"paper_id": paper_id, "title": meta.get("title"), "sha": sha}


async def ingest_upload(state, pid: str, filename: str, content: bytes) -> dict:
    """本地上传兜底。"""
    if content[:4] != b"%PDF":
        raise TxError("VAL_SEMANTIC_INVALID", "仅支持 PDF 上传")
    paper_dir = state.cfg.projects_dir / pid / "papers"
    paper_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(content).hexdigest()[:16]
    fname = f"paper-{sha}.pdf"
    (paper_dir / fname).write_bytes(content)
    parsed = _extract_pdf(paper_dir / fname, {"filename": filename})
    title = parsed.get("title") or filename

    def _ins(conn):
        paper_id = nid("paper")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO papers(id,project_id,source_kind,url,file_path,file_sha256,"
                "title,metadata_json,parsed_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (paper_id, pid, "upload", None, str(paper_dir / fname), sha, title,
                 json.dumps({"filename": filename}, ensure_ascii=False),
                 json.dumps(parsed, ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return paper_id
    paper_id = await to_thread(state.db, _ins)
    return {"paper_id": paper_id, "title": title, "sha": sha}


def _parse_arxiv_atom(xml: str) -> dict:
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml)
    entry = root.find("a:entry", ns)
    if entry is None:
        return {}
    title = (entry.findtext("a:title", "", ns) or "").strip().replace("\n", " ")
    abstract = (entry.findtext("a:summary", "", ns) or "").strip().replace("\n", " ")
    authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
    pdf_url = None
    for link in entry.findall("a:link", ns):
        if link.get("title") == "pdf":
            pdf_url = link.get("href")
    return {"arxiv_id": entry.findtext("a:id", "", ns).rsplit("/", 1)[-1],
            "title": title, "abstract": abstract, "authors": authors,
            "pdf_url": pdf_url}


def _guess_title_from_url(url: str) -> str:
    return url.rsplit("/", 1)[-1].removesuffix(".pdf") or url


def _extract_pdf(path: Path, meta: dict) -> dict:
    """pypdf 抽取：首页标题启发 + 摘要段 + 正文截断。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"note": "pypdf 未安装，正文未解析", **{k: v for k, v in meta.items()
                                                 if k in ("title", "abstract")}}
    try:
        reader = PdfReader(str(path))
        n_pages = len(reader.pages)
        first = reader.pages[0].extract_text() or ""
        body = "\n".join((p.extract_text() or "") for p in reader.pages[: min(8, n_pages)])
        title = meta.get("title")
        if not title:
            for line in first.splitlines():
                line = line.strip()
                if len(line) > 8 and not line.islower():
                    title = line[:120]
                    break
        abstract = meta.get("abstract", "")
        if not abstract:
            m = re.search(r"Abstract[:\s．]?(.{80,1200}?)(?:\n\s*\n|1\s+Introduction|I\.)",
                          body, re.S | re.I)
            if m:
                abstract = m.group(1).strip()[:1500]
        return {"title": title, "abstract": abstract, "pages": n_pages,
                "body_excerpt": body[:6000]}
    except Exception as e:  # noqa: BLE001 —— 解析失败不阻断登记
        return {"title": meta.get("title"), "parse_error": str(e)[:200]}


def paper_context(conn, pid: str, max_chars: int = 12000) -> tuple[str, list[dict]]:
    """已导入论文的上下文（标题+摘要拼接）与来源清单（文献对照证据用）。"""
    rows = conn.execute("SELECT id, title, parsed_json FROM papers WHERE project_id=? "
                        "ORDER BY created_at", (pid,)).fetchall()
    ctx, sources, used = [], [], 0
    for r in rows:
        parsed = json.loads(r["parsed_json"] or "{}")
        abstract = (parsed.get("abstract") or "")[:1500]
        snippet = f"《{r['title']}》{'：' + abstract if abstract else ''}"
        if used + len(snippet) > max_chars:
            break
        ctx.append(snippet)
        sources.append({"paper_id": r["id"], "title": r["title"]})
        used += len(snippet)
    return "\n\n".join(ctx), sources
