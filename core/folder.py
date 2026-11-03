"""Pasta vigiada: tudo que for colocado na pasta é indexado; o que for apagado de lá sai da base."""
import time
from pathlib import Path

import requests

from . import db, rag


def _scan(folder: Path) -> dict:
    found = {}
    for p in folder.rglob("*"):
        try:
            if p.is_file() and p.suffix.lower() in rag.SUPPORTED and not p.name.startswith(("~$", ".")):
                found[str(p)] = (p, p.stat().st_mtime)
        except OSError:
            continue
    return found


def sync(cfg: dict, failed: dict, progress=lambda m: None, should_pause=lambda: False) -> dict:
    """Compara a pasta com a base. `failed` guarda {caminho: mtime} dos arquivos que deram erro,
    para não tentar de novo até o arquivo mudar (ou até o usuário clicar em Sincronizar)."""
    folder = Path(cfg["watch_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    found = _scan(folder)
    known = {d["path"]: d for d in db.list_docs() if d["managed"]}
    out = {"added": 0, "removed": 0, "failed": [], "offline": False, "pending": 0}

    for path, d in known.items():  # apagou da pasta -> sai da base
        if path not in found:
            db.delete_doc(d["id"])
            out["removed"] += 1
    if out["removed"]:
        rag.invalidate()

    todo = [(p, m) for path, (p, m) in found.items()
            if (path not in known or abs((known[path]["mtime"] or 0) - m) > 1) and failed.get(path) != m]
    for i, (p, m) in enumerate(todo, 1):
        if should_pause():  # você começou a conversar: deixa o computador livre e continua depois
            out["pending"] = len(todo) - i + 1
            break
        name = p.relative_to(folder).as_posix()
        progress(f"Indexando {i}/{len(todo)}: {name}...")
        try:
            rag.ingest(cfg, str(p), name=name, mtime=m, managed=1)
            failed.pop(str(p), None)
            out["added"] += 1
        except requests.exceptions.ConnectionError:  # Ollama desligado: tenta de novo depois
            out["offline"] = True
            break
        except Exception as e:
            failed[str(p)] = m
            out["failed"].append((name, str(e)[:120]))
        time.sleep(0.2)  # respiro entre arquivos
    return out


def summary(out: dict) -> str:
    parts = []
    if out["added"]:
        parts.append(f"{out['added']} arquivo(s) indexado(s)")
    if out["removed"]:
        parts.append(f"{out['removed']} removido(s)")
    if out["failed"]:
        n, e = out["failed"][0]
        extra = f" (+{len(out['failed']) - 1})" if len(out["failed"]) > 1 else ""
        parts.append(f"falha em {n}: {e}{extra}")
    if out.get("pending"):
        parts.append(f"indexação pausada, faltam {out['pending']}")
    if out["offline"]:
        parts.append("Ollama desligado, tento de novo em instantes")
    return " · ".join(parts)
