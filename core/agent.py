"""Modo Projeto: a IA usa ferramentas (ler, criar, editar, apagar, rodar comandos) dentro da pasta do projeto."""
import json
import re
from dataclasses import dataclass
from typing import Callable

import requests

from . import router
from .tools import ToolError, Workspace

MAX_TURNS = 25


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
TOOLS = [
    _fn("list_files", "Lista arquivos e pastas (ignora node_modules, .git etc.).",
        {"path": {"type": "string", "description": "pasta, relativa à raiz. Padrão: ."},
         "depth": {"type": "integer", "description": "níveis (padrão 2)"}}, []),
    _fn("read_file", "Lê um arquivo de texto, com números de linha.",
        {"path": S, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    _fn("search_files", "Procura um texto dentro dos arquivos do projeto.", {"query": S, "path": S}, ["query"]),
    _fn("write_file", "Cria um arquivo novo ou reescreve um existente com o conteúdo COMPLETO.",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("edit_file", "Troca um trecho EXATO de um arquivo (old deve aparecer uma vez).",
        {"path": S, "old": S, "new": S, "replace_all": {"type": "boolean"}}, ["path", "old", "new"]),
    _fn("delete_path", "Apaga um arquivo ou pasta (pasta exige recursive=true).",
        {"path": S, "recursive": {"type": "boolean"}}, ["path"]),
    _fn("move_path", "Move ou renomeia um arquivo/pasta.", {"src": S, "dst": S}, ["src", "dst"]),
    _fn("make_dir", "Cria uma pasta.", {"path": S}, ["path"]),
    _fn("run_command", "Roda um comando no terminal, na raiz do projeto (testes, npm, pip, git...).",
        {"command": S}, ["command"]),
]
NAMES = {t["function"]["name"] for t in TOOLS}


@dataclass
class UI:
    text: Callable        # (texto) -> None            texto da IA chegando
    step: Callable        # (kind, summary, detail, ok) -> None    uma ferramenta foi usada
    ask: Callable         # (kind, summary, preview) -> bool       pede permissão
    auto: Callable        # () -> bool                 aprovação automática ligada?


def system_prompt(ws: Workspace, name: str) -> str:
    return (f"Você é um agente de programação (como o Codex) trabalhando dentro do projeto “{name}”.\n"
            "Use as ferramentas para ler, criar, editar, mover e apagar arquivos e para rodar comandos. "
            "Todos os caminhos são relativos à raiz do projeto.\n"
            "Regras:\n"
            "- Leia o arquivo antes de editá-lo. Prefira edit_file (troca de um trecho exato) a reescrever tudo; "
            "use write_file para arquivos novos ou reescritas completas, sempre com o conteúdo COMPLETO.\n"
            "- Os números de linha do read_file não fazem parte do arquivo.\n"
            "- Faça só o que foi pedido e não apague nada que o usuário não pediu.\n"
            "- Depois de alterar, confira (releia o arquivo ou rode os testes, se existirem).\n"
            "- Responda em português do Brasil, curto: o que foi feito e quais arquivos mudaram.\n\n"
            f"Estrutura atual do projeto:\n{ws.tree()}")


def pick_model(cfg: dict, installed: list, online: bool):
    cands = [m for m in installed if router.supports(m, "tools") and (online or not router.is_cloud(m))]
    r = cfg["routes"]
    prefer = ([r["online"]["code"]] if online else []) + [r["offline"]["code"], r["offline"]["chat"]]
    order = [m for p in prefer for m in cands if m in (p, p + ":latest")]
    order += [m for m in cands if m not in order]
    return order[0] if order else None


def _text_calls(content: str) -> list:
    """Alguns modelos escrevem a chamada como JSON no texto em vez de usar o formato de ferramenta."""
    calls = []
    for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>|```(?:json)?\s*(\{.*?\})\s*```|^(\{.*\})$",
                         content.strip(), re.S | re.M):
        raw = next(g for g in m.groups() if g)
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("name") in NAMES:
            calls.append({"function": {"name": d["name"], "arguments": d.get("arguments") or d.get("parameters") or {}}})
    return calls


def _chat(cfg, model, messages, stop, ctx_override, on_text):
    ctx, pred = router.model_opts(cfg, model, ctx_override)
    opts = {"temperature": 0.2, "num_predict": pred}
    if not router.is_cloud(model):
        opts["num_ctx"] = ctx
    body = {"model": model, "messages": messages, "tools": TOOLS, "stream": True, "options": opts}
    content, calls = [], []
    with requests.post(f"{cfg['ollama_url']}/api/chat", json=body, stream=True, timeout=(5, 600)) as r:
        if r.status_code != 200:
            msg = r.text[:200]
            if "tools" in msg.lower():
                msg = f"o modelo {model} não suporta ferramentas. Escolha outro (ex.: qwen2.5-coder:7b)."
            raise RuntimeError(msg)
        for line in r.iter_lines():
            if stop.is_set():
                break
            if not line:
                continue
            d = json.loads(line)
            if "error" in d:
                raise RuntimeError(d["error"])
            msg = d.get("message", {})
            if msg.get("content"):
                content.append(msg["content"])
                on_text(msg["content"])
            calls += msg.get("tool_calls") or []
            if d.get("done"):
                break
    text = "".join(content)
    return text, (calls or _text_calls(text))


def run(cfg: dict, model: str, history: list, ws: Workspace, project_name: str, stop, ctx_override, ui: UI):
    messages = [{"role": "system", "content": system_prompt(ws, project_name)}] + history
    seen = {}
    for _ in range(MAX_TURNS):
        text, calls = _chat(cfg, model, messages, stop, ctx_override, ui.text)
        if stop.is_set() or not calls:
            return
        messages.append({"role": "assistant", "content": text, "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            name, args = fn.get("name", ""), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            key = name + json.dumps(args, sort_keys=True, default=str)
            seen[key] = seen.get(key, 0) + 1
            if stop.is_set():
                return
            if seen[key] > 3:
                result = "ERRO: você repetiu esta mesma chamada várias vezes. Pare e responda ao usuário."
            else:
                try:
                    plan = ws.plan(name, args)
                    if plan.mutating and not ui.auto() and not ui.ask(plan.kind, plan.summary, plan.preview):
                        result = "O usuário NEGOU esta ação. Não repita; pergunte o que ele prefere."
                        ui.step(plan.kind, plan.summary + " (negado)", plan.preview, False)
                    else:
                        result, detail = plan.apply()
                        ui.step(plan.kind, plan.summary, detail, True)
                except ToolError as e:
                    result = f"ERRO: {e}"
                    ui.step("error", f"{name}: {e}", "", False)
                except Exception as e:  # erro inesperado de ferramenta não derruba a conversa
                    result = f"ERRO: {e}"
                    ui.step("error", f"{name}: {e}", "", False)
            messages.append({"role": "tool", "tool_name": name, "content": str(result)[:12000]})
    ui.text("\n\n(Parei: limite de passos atingido.)")