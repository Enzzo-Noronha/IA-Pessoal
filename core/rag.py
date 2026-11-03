import logging
import re
import time
from pathlib import Path

import numpy as np
import requests

from . import db

TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".html", ".css", ".csv", ".sql",
            ".log", ".yaml", ".yml", ".xml", ".bat", ".ini"}
SUPPORTED = TEXT_EXT | {".pdf", ".docx", ".xlsx", ".xlsm"}
CHUNK, OVERLAP, BATCH = 900, 150, 8

# a pypdf avisa (e escreve páginas de números no terminal) de cada fonte que não entende; isso não afeta o texto
logging.getLogger("pypdf").setLevel(logging.ERROR)
_cache = None


def extract(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        out = []
        for page in PdfReader(str(path)).pages:
            try:
                out.append(page.extract_text() or "")
            except Exception:  # página com defeito não derruba o arquivo inteiro
                out.append("")
            time.sleep(0.002)  # cede a vez para a janela do app
        return "\n".join(out)
    if ext == ".docx":
        from docx import Document
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for t in doc.tables:
            parts += [" | ".join(c.text for c in r.cells) for r in t.rows]
        return "\n".join(parts)
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"[Planilha: {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    out.append(" | ".join("" if c is None else str(c) for c in row))
        return "\n".join(out)
    if ext in TEXT_EXT:
        raw = path.read_bytes()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", errors="replace")
    raise ValueError(f"formato não suportado: {ext}")


def chunk(text: str) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out, i = [], 0
    while i < len(text):
        end = min(i + CHUNK, len(text))
        if end < len(text):
            cut = max(text.rfind("\n", i, end), text.rfind(". ", i, end))
            if cut > i + CHUNK // 2:
                end = cut + 1
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        i = max(end - OVERLAP, i + 1)
    return out


def embed(cfg: dict, texts: list[str], prefix: str) -> list[list[float]]:
    r = requests.post(f"{cfg['ollama_url']}/api/embed", timeout=300,
                      json={"model": cfg["embed_model"], "input": [prefix + t for t in texts]})
    if r.status_code == 404:
        raise RuntimeError(f"modelo de embeddings ausente. Rode: ollama pull {cfg['embed_model']}")
    if r.status_code != 200:
        raise RuntimeError(f"Ollama {r.status_code}: {r.text[:200]}")
    return r.json()["embeddings"]


def ingest(cfg: dict, path: str, name: str = None, mtime: float = None, managed: int = 0) -> int:
    p = Path(path)
    pieces = chunk(extract(p))
    if not pieces:
        raise ValueError("nenhum texto encontrado (PDF escaneado?)")
    rows = []
    for i in range(0, len(pieces), BATCH):
        batch = pieces[i:i + BATCH]
        embs = embed(cfg, batch, "search_document: ")
        rows += [(t, np.asarray(e, dtype=np.float32).tobytes()) for t, e in zip(batch, embs)]
        time.sleep(0.05)
    db.add_chunks(db.add_doc(name or p.name, str(p), mtime or p.stat().st_mtime, managed), rows)
    invalidate()
    return len(rows)


def invalidate():
    global _cache
    _cache = None


def _load():
    global _cache
    if _cache is None:
        rows = db.all_chunks()
        if rows:
            m = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
            m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
            _cache = (m, [(r["text"], r["name"]) for r in rows])
        else:
            _cache = (None, [])
    return _cache


def search(cfg: dict, query: str, k: int = 5, min_score: float = 0.3):
    m, meta = _load()
    if m is None:
        return []
    q = np.asarray(embed(cfg, [query], "search_query: ")[0], dtype=np.float32)
    q /= np.linalg.norm(q) + 1e-9
    scores = m @ q
    top = np.argsort(-scores)[:k]
    return [(meta[i][1], meta[i][0], float(scores[i])) for i in top if scores[i] >= min_score]


def build_prompt(question: str, hits) -> str:
    ctx = "\n\n".join(f"[{i}] ({name})\n{text}" for i, (name, text, _) in enumerate(hits, 1))
    return ("Use os trechos dos documentos do usuário abaixo para responder. Se a resposta não estiver "
            "neles, diga isso claramente antes de responder com conhecimento geral.\n\n"
            f"{ctx}\n\nPergunta: {question}")
