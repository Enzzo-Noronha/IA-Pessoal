"""Ferramentas do modo Projeto. Tudo fica preso à pasta do projeto: a IA não sai dela."""
import difflib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from . import config

IGNORE = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build",
          ".next", ".cache", ".pytest_cache"}
MAX_READ, MAX_OUT = 20000, 8000
DANGEROUS = re.compile(r"(rm\s+-rf\s+[/~]|rmdir\s+/s\s+/q\s+[a-z]:\\?\s*$|format\s+[a-z]:|shutdown|mkfs|:\(\)\s*\{)", re.I)


class ToolError(Exception):
    pass


def A(args: dict, *names, default=None):
    """Lê um argumento aceitando apelidos (modelos pequenos erram o nome do parâmetro)."""
    for n in names:
        if args.get(n) is not None:
            return args[n]
    return default


class Plan:
    """Uma ação planejada: dá para mostrar o que vai acontecer antes de executar."""
    def __init__(self, kind, summary, preview="", mutating=False, apply=None):
        self.kind, self.summary, self.preview, self.mutating, self.apply = kind, summary, preview, mutating, apply


def _diff(old: str, new: str, name: str, limit=120) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"antes/{name}", f"depois/{name}",
                                      lineterm="", n=2))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... (+{len(lines) - limit} linhas)"]
    return "\n".join(lines)


def _read(p: Path):
    raw = p.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ToolError("arquivo binário")
    text = raw.decode("utf-8", errors="replace")
    crlf = "\r\n" in text
    return text.replace("\r\n", "\n"), crlf


def _write(p: Path, text: str, crlf=False):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", "\r\n") if crlf else text)


class Workspace:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.run_id = uuid.uuid4().hex[:8]
        self.journal, self._n = [], 0

    # ---------- caminhos ----------
    def path(self, rel) -> Path:
        rel = str(rel or ".").strip().replace("\\", "/")
        p = (self.root / rel).resolve()
        if p != self.root and not p.is_relative_to(self.root):
            raise ToolError("caminho fora da pasta do projeto")
        return p

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix() or "."

    def _guard(self, p: Path):
        if p == self.root:
            raise ToolError("não é permitido alterar a pasta raiz do projeto")
        if ".git" in p.relative_to(self.root).parts:
            raise ToolError("a pasta .git não pode ser alterada")

    def _backup(self, p: Path) -> str:
        d = config.DATA_DIR / "backups" / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        self._n += 1
        dst = d / f"{self._n}_{p.name}"
        shutil.copytree(p, dst) if p.is_dir() else shutil.copy2(p, dst)
        return str(dst)

    # ---------- planejamento ----------
    def plan(self, name: str, args: dict) -> Plan:
        fn = getattr(self, f"_p_{name}", None)
        if fn is None:
            raise ToolError(f"ferramenta desconhecida: {name}")
        return fn(args or {})

    def tree(self, depth=2, limit=120) -> str:
        return self._p_list_files({"path": ".", "depth": depth, "limit": limit}).apply()[0]

    # --- leitura (sem pedir permissão) ---
    def _p_list_files(self, a):
        base = self.path(A(a, "path", "dir", "directory", default="."))
        depth = int(A(a, "depth", default=2))
        limit = int(A(a, "limit", default=300))
        if not base.exists():
            raise ToolError("não existe")

        def run():
            if base.is_file():
                return self.rel(base), ""
            lines = []

            def walk(d, lvl):
                try:
                    items = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
                except OSError:
                    return
                for it in items:
                    if it.name in IGNORE:
                        continue
                    if len(lines) >= limit:
                        return
                    lines.append("  " * lvl + it.name + ("/" if it.is_dir() else ""))
                    if it.is_dir() and lvl + 1 < depth:
                        walk(it, lvl + 1)
            walk(base, 0)
            out = "\n".join(lines) or "(pasta vazia)"
            if len(lines) >= limit:
                out += "\n... (lista cortada; abra uma subpasta com path)"
            return out, ""
        return Plan("list", f"Listar {self.rel(base)}", apply=run)

    def _p_read_file(self, a):
        p = self.path(A(a, "path", "file_path", "filename", "file"))
        if not p.is_file():
            raise ToolError("arquivo não encontrado")
        s, e = int(A(a, "start_line", "start", default=1) or 1), A(a, "end_line", "end")

        def run():
            text, _ = _read(p)
            lines = text.split("\n")
            end = min(len(lines), int(e)) if e else len(lines)
            body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(max(1, s), end + 1))
            if len(body) > MAX_READ:
                body = body[:MAX_READ] + f"\n... (cortado; use start_line/end_line; o arquivo tem {len(lines)} linhas)"
            return f"{self.rel(p)} ({len(lines)} linhas; os números à esquerda NÃO fazem parte do arquivo)\n{body}", ""
        return Plan("read", f"Ler {self.rel(p)}", apply=run)

    def _p_search_files(self, a):
        q = A(a, "query", "pattern", "text", "q")
        if not q:
            raise ToolError("informe query")
        base = self.path(A(a, "path", default="."))
        rx = re.compile(re.escape(str(q)), re.I)

        def run():
            hits = []
            for dp, dn, fn in os.walk(base):
                dn[:] = [d for d in dn if d not in IGNORE]
                for f in fn:
                    fp = Path(dp) / f
                    try:
                        if fp.stat().st_size > 1_000_000:
                            continue
                        for i, line in enumerate(fp.read_text(encoding="utf-8").splitlines(), 1):
                            if rx.search(line):
                                hits.append(f"{self.rel(fp)}:{i}: {line.strip()[:160]}")
                                if len(hits) >= 60:
                                    return "\n".join(hits) + "\n... (mais resultados cortados)", ""
                    except (OSError, UnicodeDecodeError):
                        continue
            return "\n".join(hits) or "nenhum resultado", ""
        return Plan("search", f"Buscar “{q}”", apply=run)

    # --- escrita (pede permissão) ---
    def _p_write_file(self, a):
        p = self.path(A(a, "path", "file_path", "filename", "file"))
        content = A(a, "content", "contents", "text", "code")
        if content is None:
            raise ToolError("informe content (o conteúdo COMPLETO do arquivo)")
        self._guard(p)
        if p.is_dir():
            raise ToolError("é uma pasta")
        existed = p.exists()
        old, crlf = _read(p) if existed else ("", False)
        content = str(content)
        preview = _diff(old, content, self.rel(p)) if existed else "\n".join(
            "+ " + l for l in content.splitlines()[:60]) + ("\n..." if content.count("\n") > 60 else "")

        def run():
            bk = self._backup(p) if existed else None
            _write(p, content, crlf)
            self.journal.append({"action": "modify" if existed else "create", "path": self.rel(p), "backup": bk})
            return f"OK: {'sobrescrito' if existed else 'criado'} {self.rel(p)} ({len(content.splitlines())} linhas)", preview
        return Plan("write", f"{'Sobrescrever' if existed else 'Criar'} {self.rel(p)}", preview, True, run)

    def _p_edit_file(self, a):
        p = self.path(A(a, "path", "file_path", "filename", "file"))
        old = A(a, "old", "old_str", "old_string", "search", "find")
        new = A(a, "new", "new_str", "new_string", "replace", "replacement", default="")
        if old in (None, ""):
            raise ToolError("informe old (o trecho exato a trocar)")
        self._guard(p)
        if not p.is_file():
            raise ToolError("arquivo não encontrado")
        text, crlf = _read(p)
        old, new = str(old).replace("\r\n", "\n"), str(new).replace("\r\n", "\n")
        n = text.count(old)
        everywhere = bool(A(a, "replace_all", default=False))
        if n == 0:
            raise ToolError("o trecho old não foi encontrado; leia o arquivo de novo e copie o trecho exato")
        if n > 1 and not everywhere:
            raise ToolError(f"o trecho aparece {n} vezes; inclua mais linhas ao redor ou use replace_all")
        result = text.replace(old, new) if everywhere else text.replace(old, new, 1)
        preview = _diff(text, result, self.rel(p))

        def run():
            bk = self._backup(p)
            _write(p, result, crlf)
            self.journal.append({"action": "modify", "path": self.rel(p), "backup": bk})
            return f"OK: editado {self.rel(p)} ({n if everywhere else 1} troca)", preview
        return Plan("edit", f"Editar {self.rel(p)}", preview, True, run)

    def _p_delete_path(self, a):
        p = self.path(A(a, "path", "file_path", "filename", "file"))
        self._guard(p)
        if not p.exists():
            raise ToolError("não existe")
        count = 1
        if p.is_dir():
            if not A(a, "recursive", default=False):
                raise ToolError("é uma pasta; use recursive=true para apagar com o conteúdo")
            files = [f for f in p.rglob("*") if f.is_file()]
            count = len(files)
            if count > 2000 or sum(f.stat().st_size for f in files) > 200_000_000:
                raise ToolError("pasta grande demais para apagar por aqui; apague manualmente")

        def run():
            bk = self._backup(p)
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            self.journal.append({"action": "delete", "path": self.rel(p), "backup": bk})
            return f"OK: apagado {self.rel(p)}", ""
        what = f"pasta com {count} arquivo(s)" if p.is_dir() else "arquivo"
        return Plan("delete", f"Apagar {self.rel(p)}", f"Será apagado: {what}. (Dá para desfazer depois.)", True, run)

    def _p_move_path(self, a):
        src = self.path(A(a, "src", "source", "from", "path"))
        dst = self.path(A(a, "dst", "destination", "to", "new_path"))
        self._guard(src)
        self._guard(dst)
        if not src.exists():
            raise ToolError("origem não existe")
        if dst.exists():
            raise ToolError("o destino já existe")

        def run():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            self.journal.append({"action": "move", "path": self.rel(src), "dst": self.rel(dst), "backup": None})
            return f"OK: movido {self.rel(src)} -> {self.rel(dst)}", ""
        return Plan("move", f"Mover {self.rel(src)} → {self.rel(dst)}", mutating=True, apply=run)

    def _p_make_dir(self, a):
        p = self.path(A(a, "path", "dir", "directory"))
        self._guard(p)

        def run():
            existed = p.exists()
            p.mkdir(parents=True, exist_ok=True)
            if not existed:
                self.journal.append({"action": "create", "path": self.rel(p), "backup": None})
            return f"OK: pasta {self.rel(p)}", ""
        return Plan("mkdir", f"Criar pasta {self.rel(p)}", mutating=True, apply=run)

    def _p_run_command(self, a):
        cmd = A(a, "command", "cmd")
        if not cmd:
            raise ToolError("informe command")
        if DANGEROUS.search(str(cmd)):
            raise ToolError("comando bloqueado por segurança")
        timeout = min(int(A(a, "timeout", default=60) or 60), 180)

        def run():
            try:
                r = subprocess.run(str(cmd), shell=True, cwd=self.root, capture_output=True, text=True,
                                   timeout=timeout, encoding="utf-8", errors="replace",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                code = r.returncode
            except subprocess.TimeoutExpired:
                out, code = f"(tempo esgotado após {timeout}s)", -1
            if len(out) > MAX_OUT:
                out = out[:MAX_OUT // 2] + "\n... (cortado) ...\n" + out[-MAX_OUT // 2:]
            return f"código de saída {code}\n{out}", out
        return Plan("run", f"Executar: {cmd}", f"$ {cmd}\n(na pasta {self.root.name})", True, run)


def undo(root: str, items: list) -> int:
    """Desfaz as alterações de uma execução, da mais recente para a mais antiga."""
    root = Path(root)
    n = 0
    for it in reversed(items):
        p = root / it["path"]
        try:
            act = it["action"]
            if act == "create":
                shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
            elif act in ("modify", "delete") and it.get("backup"):
                bk = Path(it["backup"])
                if p.exists():
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(bk, p) if bk.is_dir() else shutil.copy2(bk, p)
            elif act == "move":
                dst = root / it["dst"]
                if dst.exists() and not p.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(p))
            n += 1
        except OSError:
            continue
    return n