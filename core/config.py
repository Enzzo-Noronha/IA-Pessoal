import json
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv():
    """Carrega variáveis simples de .env sem exigir bibliotecas adicionais."""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()
APP_NAME = os.getenv("AI_NAME", "Local AI")
DATA_DIR = Path(os.getenv("AI_DATA_DIR", Path(os.getenv("APPDATA", Path.home())) / "LocalAI"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE = DATA_DIR / "local_ai.db"

# o que o usuário pode alterar pela tela de Configurações (é só isso que vai para o config.json)
USER_KEYS = ("gemini_key", "num_ctx", "num_predict", "models", "watch_dir", "projects", "agent_approval")

DEFAULTS = {
    "ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
    "gemini_key": os.getenv("GEMINI_API_KEY", ""),
    "watch_dir": str(DATA_DIR / "Arquivos"),   # pasta vigiada: tudo que cair aqui é indexado
    "embed_model": "nomic-embed-text",
    "num_ctx": 32768,        # contexto padrão (conversa inteira), em tokens
    "num_predict": 12288,    # tamanho máximo da resposta, em tokens
    "projects": [],          # pastas de projeto: [{"name": "meu-site", "path": "C:/..."}]
    "agent_approval": "ask",  # "ask" = pergunta antes de alterar arquivos/rodar comandos; "auto" = não pergunta
    "models": {},            # ajustes por modelo: {"nome": {"num_ctx": 8192, "num_predict": 2000}}
    "routes": {
        "offline": {"chat": "qwen2.5:3b", "code": "qwen2.5-coder:7b", "fast": "qwen2.5:1.5b"},
        "online": {"chat": "gemini-3.8-flash", "code": "gemma4:31b-cloud", "fast": "gemini-3.5-flash-lite"},
    },
    "gemini_models": ["gemini-3.8-flash", "gemini-3.5-flash-lite"],
    "system_prompt": (
        f"Você é {APP_NAME}, uma assistente pessoal. Responda em português do Brasil, "
        "de forma direta e objetiva. Em código, entregue soluções completas e funcionais."
    ),
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (OSError, ValueError):
            pass
    return cfg


def save(cfg: dict) -> None:
    data = {k: cfg[k] for k in USER_KEYS}
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
