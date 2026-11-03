import sqlite3
import threading

from . import config

_local = threading.local()


def _c() -> sqlite3.Connection:
    """Uma conexão por thread (modo WAL): a tela nunca espera a indexação terminar."""
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(config.DB_FILE, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c


def _init():
    c = _c()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS conversations(
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
      updated TEXT DEFAULT CURRENT_TIMESTAMP, project TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS attachments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL, kind TEXT NOT NULL,
      name TEXT NOT NULL, path TEXT, text TEXT);
    CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INTEGER NOT NULL,
      role TEXT NOT NULL, content TEXT NOT NULL, model TEXT,
      created TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS documents(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, path TEXT NOT NULL,
      added TEXT DEFAULT CURRENT_TIMESTAMP, mtime REAL DEFAULT 0, managed INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS chunks(
      id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id INTEGER NOT NULL,
      idx INTEGER NOT NULL, text TEXT NOT NULL, emb BLOB NOT NULL);
    """)
    for col in ("mtime REAL DEFAULT 0", "managed INTEGER DEFAULT 0"):  # bancos antigos ganham as colunas novas
        try:
            c.execute(f"ALTER TABLE documents ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    try:
        c.execute("ALTER TABLE conversations ADD COLUMN project TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_att_msg ON attachments(msg_id)")
    # sem estes índices, cada consulta varria a tabela inteira de trechos (era isso que travava a janela)
    c.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id)")
    c.commit()


_init()


def _run(sql, args=()):
    c = _c()
    cur = c.execute(sql, args)
    c.commit()
    return cur


def _all(sql, args=()):
    return _c().execute(sql, args).fetchall()


# conversas
def new_conv(title: str, project: str = "") -> int:
    return _run("INSERT INTO conversations(title, project) VALUES(?,?)", (title[:60], project)).lastrowid


def list_convs(project: str = ""):
    return _all("SELECT id, title FROM conversations WHERE COALESCE(project,'')=? "
                "ORDER BY updated DESC, id DESC", (project,))


def add_msg(conv_id, role, content, model=None) -> int:
    mid = _run("INSERT INTO messages(conv_id, role, content, model) VALUES(?,?,?,?)",
               (conv_id, role, content, model)).lastrowid
    _run("UPDATE conversations SET updated=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
    return mid


def get_msgs(conv_id):
    return _all("SELECT id, role, content, model FROM messages WHERE conv_id=? ORDER BY id", (conv_id,))


def update_msg_content(msg_id, content):
    _run("UPDATE messages SET content=? WHERE id=?", (content, msg_id))


def add_attachment(msg_id, kind, name, path, text=""):
    _run("INSERT INTO attachments(msg_id, kind, name, path, text) VALUES(?,?,?,?,?)",
         (msg_id, kind, name, path, text))


def conv_attachments(conv_id):
    """{id_da_mensagem: [anexos]} de uma conversa."""
    out = {}
    for r in _all("SELECT a.* FROM attachments a JOIN messages m ON m.id=a.msg_id WHERE m.conv_id=? ORDER BY a.id",
                  (conv_id,)):
        out.setdefault(r["msg_id"], []).append(r)
    return out


def delete_msg(msg_id):
    _run("DELETE FROM attachments WHERE msg_id=?", (msg_id,))
    _run("DELETE FROM messages WHERE id=?", (msg_id,))


def delete_conv(conv_id):
    _run("DELETE FROM attachments WHERE msg_id IN (SELECT id FROM messages WHERE conv_id=?)", (conv_id,))
    _run("DELETE FROM messages WHERE conv_id=?", (conv_id,))
    _run("DELETE FROM conversations WHERE id=?", (conv_id,))


# documentos (RAG)
def add_doc(name: str, path: str, mtime: float = 0.0, managed: int = 0) -> int:
    for old in _all("SELECT id FROM documents WHERE path=?", (path,)):
        delete_doc(old["id"])
    return _run("INSERT INTO documents(name, path, mtime, managed) VALUES(?,?,?,?)",
                (name, path, mtime, managed)).lastrowid


def add_chunks(doc_id: int, rows):
    c = _c()
    c.executemany("INSERT INTO chunks(doc_id, idx, text, emb) VALUES(?,?,?,?)",
                  [(doc_id, i, t, e) for i, (t, e) in enumerate(rows)])
    c.commit()


def list_docs():
    return _all("SELECT d.id, d.name, d.path, d.mtime, d.managed, COUNT(c.id) AS n "
                "FROM documents d LEFT JOIN chunks c ON c.doc_id = d.id GROUP BY d.id ORDER BY d.id DESC")


def delete_doc(doc_id: int):
    _run("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    _run("DELETE FROM documents WHERE id=?", (doc_id,))


def all_chunks():
    return _all("SELECT c.text, c.emb, d.name FROM chunks c JOIN documents d ON d.id=c.doc_id")