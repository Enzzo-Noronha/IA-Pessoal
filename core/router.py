import json
import re
import socket

import requests

NOTICE = "\n\n[resposta cortada pelo limite de tamanho. Clique em Continuar]"

CODE_RE = re.compile(
    r"\b(c[óo]digo|code|fun[çc][ãa]o|bug|erro|python|javascript|js|html|css|sql|node|express|"
    r"api|script|classe|regex|debug|react|json)\b", re.I)
BIG_RE = re.compile(
    r"\b(landing ?page|p[áa]gina|site|sistema|aplicativo|app|card[áa]pio|dashboard)\b", re.I)

LIMITS: dict[str, int] = {}  # contexto máximo de cada modelo local (preenchido pelo app)
CAPS: dict[str, list] = {}   # o que cada modelo sabe fazer: completion, vision, tools (vem do Ollama)


def is_online(timeout: float = 1.5) -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout).close()
        return True
    except OSError:
        return False


def ollama_models(cfg: dict) -> list[str]:
    """Lista os modelos e aproveita as capacidades já enviadas por /api/tags.

    Versões recentes do Ollama incluem ``capabilities`` nesta resposta. Usá-las
    aqui evita uma janela em que a interface já conhece os nomes, mas ainda
    conclui incorretamente que nenhum deles aceita ferramentas.
    """
    try:
        r = requests.get(f"{cfg['ollama_url']}/api/tags", timeout=3)
        r.raise_for_status()
        models = r.json().get("models", [])
        for item in models:
            name = item.get("name")
            caps = item.get("capabilities")
            if name and isinstance(caps, list):
                CAPS[name] = caps
        return [m["name"] for m in models if "embed" not in m.get("name", "")]
    except Exception:
        return []


def is_gemini(model: str) -> bool:
    return model.startswith("gemini")


def is_cloud(model: str) -> bool:
    """Modelos de nuvem do Ollama (ex.: gemma4:31b-cloud) e do Gemini."""
    return is_gemini(model) or model.split(":")[-1].endswith("cloud")


def model_max_ctx(cfg: dict, model: str):
    """Pergunta ao Ollama o contexto máximo do modelo (None se não souber) e guarda o que ele sabe fazer."""
    try:
        j = requests.post(f"{cfg['ollama_url']}/api/show", json={"model": model}, timeout=5).json()
        if isinstance(j.get("capabilities"), list):
            CAPS[model] = j["capabilities"]
        for k, v in j.get("model_info", {}).items():
            if k.endswith(".context_length"):
                return int(v)
    except Exception:
        pass
    return None


_VISION_HINT = ("vision", "llava", "gemma3:4b", "gemma3:12b", "gemma3:27b", "gemma4", "qwen2.5vl", "qwen3-vl",
                "minicpm-v", "moondream", "gemini")
_TOOLS_HINT = ("qwen2.5", "qwen3", "llama3.1", "llama3.2", "llama3.3", "gemma4", "deepseek", "glm", "mistral",
               "gpt-oss", "command-r", "dev-coder")


def supports(model: str, cap: str) -> bool:
    """O modelo aceita imagens ('vision') ou chamadas de ferramenta ('tools')?"""
    caps = CAPS.get(model)
    if caps is not None:
        return cap in caps
    m = model.lower()  # o Ollama não informou: decide pelo nome
    hints = _VISION_HINT if cap == "vision" else _TOOLS_HINT
    return any(h in m for h in hints) and not (cap == "tools" and m.startswith("gemini"))


def pick_vision_model(cfg: dict, installed: list[str], online: bool):
    if online and cfg["gemini_key"]:
        return cfg["gemini_models"][0]
    cands = [m for m in installed if supports(m, "vision") and (online or not is_cloud(m))]
    cands.sort(key=lambda m: is_cloud(m))  # local primeiro
    return cands[0] if cands else None


def model_opts(cfg: dict, model: str, ctx_override=None):
    """Retorna (num_ctx, num_predict) do modelo: ajuste do modelo > padrão, limitado ao máximo dele."""
    m = cfg.get("models", {}).get(model, {})
    ctx = int(ctx_override or m.get("num_ctx") or cfg["num_ctx"])
    if LIMITS.get(model):
        ctx = min(ctx, LIMITS[model])
    pred = int(m.get("num_predict") or cfg["num_predict"])
    return ctx, min(pred, ctx)


def _available(cfg, model, installed, online) -> bool:
    if is_gemini(model):
        return online and bool(cfg["gemini_key"])
    if is_cloud(model):
        return online and model in installed
    return model in installed or f"{model}:latest" in installed


def pick_model(cfg: dict, text: str, installed: list[str], online: bool, code: bool = False) -> str:
    """code=True força a rota de código (use quando a conversa já tem código)."""
    is_code = code or CODE_RE.search(text) or BIG_RE.search(text)
    kind = "code" if is_code else "chat"
    for mode in (("online", "offline") if online else ("offline",)):
        m = cfg["routes"][mode][kind]
        if _available(cfg, m, installed, online):
            return m
    local = [m for m in installed if not is_cloud(m)]
    return (local or installed or [cfg["routes"]["offline"]["chat"]])[0]


def _stream_ollama(cfg, model, messages, stop, ctx_override=None):
    ctx, pred = model_opts(cfg, model, ctx_override)
    opts = {"temperature": 0.4, "repeat_penalty": 1.1, "repeat_last_n": 256, "num_predict": pred}
    if not is_cloud(model):  # nos modelos de nuvem o limite é do servidor
        opts["num_ctx"] = ctx
    body = {"model": model, "messages": messages, "stream": True, "options": opts}
    with requests.post(f"{cfg['ollama_url']}/api/chat", json=body, stream=True, timeout=(5, 600)) as r:
        if r.status_code != 200:
            raise RuntimeError(f"Ollama {r.status_code}: {r.text[:200]}")
        for line in r.iter_lines():
            if stop.is_set():
                return
            if not line:
                continue
            data = json.loads(line)
            if "error" in data:
                raise RuntimeError(data["error"])
            chunk = data.get("message", {}).get("content", "")
            if chunk:
                yield chunk
            if data.get("done"):
                if data.get("done_reason") == "length":
                    yield NOTICE
                return


def _stream_gemini(cfg, model, messages, stop):
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = []
    for m in messages:
        if m["role"] == "system":
            continue
        parts = [{"text": m["content"]}]
        for img in m.get("images", []):  # imagens anexadas (base64)
            parts.append({"inline_data": {"mime_type": "image/png" if img.startswith("iVBOR") else "image/jpeg",
                                          "data": img}})
        contents.append({"role": "user" if m["role"] == "user" else "model", "parts": parts})
    body = {"contents": contents}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
    with requests.post(url, json=body, stream=True, timeout=(5, 120),
                       headers={"x-goog-api-key": cfg["gemini_key"]}) as r:
        if r.status_code != 200:
            raise RuntimeError(f"Gemini {r.status_code}: {r.text[:200]}")
        for line in r.iter_lines(decode_unicode=True):
            if stop.is_set():
                return
            if line and line.startswith("data:"):
                data = json.loads(line[5:])
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if part.get("text"):
                            yield part["text"]


def stream_with_fallback(cfg, model, history, stop, installed, ctx_override=None):
    """Gera (modelo_usado, trecho). Se a nuvem falhar antes de responder, cai para o modelo local."""
    messages = [{"role": "system", "content": cfg["system_prompt"]}] + history
    cloud = is_cloud(model)
    got = False
    try:
        if is_gemini(model):
            gen = _stream_gemini(cfg, model, messages, stop)
        else:
            gen = _stream_ollama(cfg, model, messages, stop, ctx_override)
        for chunk in gen:
            got = True
            yield model, chunk
    except Exception as e:
        if got or not cloud:
            raise
        routes = cfg["routes"]
        local = routes["offline"]["code"] if model == routes["online"]["code"] else routes["offline"]["chat"]
        yield local, f"[{model} falhou: {str(e)[:150]}. Usando {local}]\n\n"
        for chunk in _stream_ollama(cfg, local, messages, stop, ctx_override):
            yield local, chunk
