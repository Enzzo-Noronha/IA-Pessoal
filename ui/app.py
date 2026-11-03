import base64
import copy
import json
import os
import queue
import shutil
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageGrab

from core import agent, config, db, folder, rag, router, tools
from ui.chat_view import ChatView

BG, PANEL, CARD, BORDER = "#0b0b0f", "#111117", "#17171f", "#26262f"
TEXT, MUTED, ACCENT, GREEN, RED = "#e6e6ec", "#8b8b99", "#7c5cff", "#3ddc97", "#ff5c6c"
AUTO = "Auto"
NO_PROJECT, ADD_PROJECT = "Sem projeto", "+ Adicionar pasta..."
APPR_LABELS = {"Perguntar antes de alterar": "ask", "Aprovar tudo automaticamente": "auto"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
ATT_DIR = config.DATA_DIR / "anexos"
MAX_ATT_CHARS = 60000
CTX_CHOICES = {"Contexto padrão": None, "Contexto 4k": 4096, "Contexto 8k": 8192, "Contexto 16k": 16384,
               "Contexto 32k": 32768, "Contexto 64k": 65536, "Contexto 128k": 131072}
EXT = {"html": ".html", "css": ".css", "javascript": ".js", "js": ".js", "python": ".py", "py": ".py",
       "json": ".json", "sql": ".sql", "bash": ".sh", "sh": ".sh", "bat": ".bat", "md": ".md"}


def fmt(n):
    return f"{int(n):,}".replace(",", ".")


def trim_history(history, budget=28000):
    """Mantém as mensagens mais recentes até o limite de caracteres (evita estourar o contexto)."""
    out, total = [], 0
    for m in reversed(history):
        total += len(m["content"])
        if out and total > budget:
            break
        out.append(m)
    out.reverse()
    if out and out[0]["role"] == "assistant":  # a conversa precisa começar com o usuário
        out.insert(0, {"role": "user", "content": "(continuação da conversa)"})
    return out


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(config.APP_NAME)
        self.geometry("1100x760")
        self.minsize(820, 560)
        self.configure(fg_color=BG)

        self.cfg = config.load()
        self.conv_id = None
        self.installed, self.online = [], False
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.reply, self.reply_model, self.sources, self.err = [], None, [], None
        self._n = 0
        self.limits, self._win = {}, None
        self.failed, self._syncing = {}, False
        self.stats, self._doc_count = None, 0
        self.project, self.pending, self.session_auto, self._approvals = None, [], False, {}

        self._build()
        self._refresh_project_menu()
        self._redraw()  # mostra a tela de boas-vindas
        self._load_convs()
        self._load_docs()
        self.refresh_status()
        self.after(40, self._poll)
        self.after(350, self._tick)
        self.after(3000, self._auto_sync)

    # ---------- UI ----------
    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        side = ctk.CTkFrame(self, width=270, fg_color=PANEL, corner_radius=0)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(side, text=config.APP_NAME, font=("Segoe UI", 20, "bold"), text_color=TEXT
                     ).grid(row=0, column=0, padx=18, pady=(18, 10), sticky="w")
        ctk.CTkButton(side, text="+  Nova conversa  (Ctrl+N)", fg_color=ACCENT, hover_color="#6a4be0",
                      command=self.new_chat).grid(row=1, column=0, padx=14, pady=6, sticky="ew")
        self.search_entry = ctk.CTkEntry(side, fg_color=CARD, border_color=BORDER,
                                         placeholder_text="Buscar conversas  (Ctrl+K)")
        self.search_entry.bind("<KeyRelease>", lambda e: self._load_convs())
        self.search_entry.grid(row=2, column=0, padx=14, pady=(2, 4), sticky="ew")
        self.convs = ctk.CTkScrollableFrame(side, fg_color="transparent", scrollbar_button_color=BORDER,
                                           scrollbar_button_hover_color=MUTED)
        self.convs.grid(row=3, column=0, padx=6, pady=6, sticky="nsew")

        head = ctk.CTkFrame(side, fg_color="transparent")
        head.grid(row=4, column=0, padx=14, pady=(8, 0), sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        self.docs_title = ctk.CTkLabel(head, text="Meus arquivos", text_color=MUTED, font=("Segoe UI", 12, "bold"))
        self.docs_title.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(head, text="+ Adicionar", width=90, height=26, fg_color=CARD, hover_color=BORDER,
                      border_width=1, border_color=BORDER, command=self.add_files).grid(row=0, column=1)
        tools = ctk.CTkFrame(side, fg_color="transparent")
        tools.grid(row=5, column=0, padx=14, pady=(6, 0), sticky="ew")
        tools.grid_columnconfigure((0, 1), weight=1)
        for i, (t, c) in enumerate((("Abrir pasta", self.open_folder), ("Sincronizar", lambda: self.sync_now(True)))):
            ctk.CTkButton(tools, text=t, height=26, fg_color=CARD, hover_color=BORDER, border_width=1,
                          border_color=BORDER, text_color=MUTED, command=c
                          ).grid(row=0, column=i, padx=(0, 4) if i == 0 else (4, 0), sticky="ew")
        self.docs = ctk.CTkScrollableFrame(side, height=150, fg_color=CARD, border_width=1, border_color=BORDER,
                                           scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
        self.docs.grid(row=6, column=0, padx=14, pady=6, sticky="ew")
        self.docs_info = ctk.CTkLabel(side, text="", text_color=MUTED, font=("Segoe UI", 11),
                                      wraplength=240, justify="left")
        self.docs_info.grid(row=7, column=0, padx=14, sticky="w")

        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.grid(row=8, column=0, padx=14, pady=14, sticky="ew")
        foot.grid_columnconfigure((0, 1), weight=1)
        for i, (t, c) in enumerate((("Configurações", self.open_settings), ("Apagar chat", self.delete_chat))):
            ctk.CTkButton(foot, text=t, fg_color=CARD, hover_color=BORDER, border_width=1,
                          border_color=BORDER, text_color=MUTED, command=c
                          ).grid(row=0, column=i, padx=3, sticky="ew")

        main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(main, fg_color="transparent")
        top.grid(row=0, column=0, padx=20, pady=(14, 6), sticky="ew")
        self.model_var = ctk.StringVar(value=AUTO)
        self.model_menu = ctk.CTkOptionMenu(top, variable=self.model_var, values=[AUTO], width=210,
                                            fg_color=CARD, button_color=BORDER, button_hover_color=ACCENT)
        self.model_menu.pack(side="left")
        self.ctx_var = ctk.StringVar(value="Contexto padrão")
        ctk.CTkOptionMenu(top, variable=self.ctx_var, values=list(CTX_CHOICES), width=135, fg_color=CARD,
                          button_color=BORDER, button_hover_color=ACCENT, text_color=MUTED
                          ).pack(side="left", padx=(8, 0))
        self.rag_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(top, text="Usar meus arquivos", variable=self.rag_var, progress_color=ACCENT,
                      text_color=MUTED).pack(side="left", padx=12)
        self.think = ctk.CTkLabel(top, text="", text_color=ACCENT, width=76, anchor="w")
        self.think.pack(side="left")
        self.status = ctk.CTkLabel(top, text="● verificando...", text_color=MUTED)
        self.status.pack(side="right")

        self.chat = ChatView(main, self)
        self.chat.grid(row=1, column=0, padx=20, pady=6, sticky="nsew")

        bottom = ctk.CTkFrame(main, fg_color="transparent")
        bottom.grid(row=2, column=0, padx=20, pady=(6, 14), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(bottom, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        menu_style = dict(height=28, fg_color=CARD, button_color=BORDER, button_hover_color=ACCENT,
                          text_color=MUTED, dynamic_resizing=False)
        self.proj_var = ctk.StringVar(value=NO_PROJECT)
        self.proj_menu = ctk.CTkOptionMenu(bar, variable=self.proj_var, values=[NO_PROJECT, ADD_PROJECT], width=200,
                                           command=self._on_project, **menu_style)
        self.proj_menu.pack(side="left")
        self.appr_var = ctk.StringVar(value=next((k for k, v in APPR_LABELS.items()
                                                  if v == self.cfg.get("agent_approval")), "Perguntar antes de alterar"))
        self.appr_menu = ctk.CTkOptionMenu(bar, variable=self.appr_var, values=list(APPR_LABELS), width=230,
                                           command=self._on_approval, **menu_style)
        self.att_bar = ctk.CTkFrame(bar, fg_color="transparent")
        self.att_bar.pack(side="left", padx=8)
        box = ctk.CTkFrame(bottom, fg_color=CARD, border_width=1, border_color=BORDER, corner_radius=18)
        box.grid(row=1, column=0, sticky="ew")
        box.grid_columnconfigure(1, weight=1)
        self.attach_btn = ctk.CTkButton(box, text="+", width=36, height=36, corner_radius=18, fg_color=BORDER,
                                        hover_color="#33333f", font=("Segoe UI", 20), command=self.attach_dialog)
        self.attach_btn.grid(row=0, column=0, padx=(12, 0), pady=10, sticky="s")
        self.input = ctk.CTkTextbox(box, height=62, fg_color="transparent", border_width=0,
                                    font=("Segoe UI", 14), scrollbar_button_color=BORDER,
                                    scrollbar_button_hover_color=MUTED)
        self.input.grid(row=0, column=1, padx=(8, 6), pady=10, sticky="ew")
        self.input.bind("<Return>", self._on_enter)
        self.input._textbox.bind("<Control-v>", self._on_paste)
        self.input._textbox.bind("<FocusIn>", lambda e: box.configure(border_color=ACCENT))
        self.input._textbox.bind("<FocusOut>", lambda e: box.configure(border_color=BORDER))
        self.send_btn = ctk.CTkButton(box, text="↑", width=44, height=44, corner_radius=22, fg_color=ACCENT,
                                      hover_color="#6a4be0", font=("Segoe UI", 20, "bold"), command=self.on_send)
        self.send_btn.grid(row=0, column=2, padx=(0, 12), pady=10, sticky="s")
        ctk.CTkLabel(bottom, text="Enter envia  ·  Shift+Enter quebra a linha  ·  Ctrl+V cola imagem  ·  Esc interrompe",
                     text_color="#5d5d6b", font=("Segoe UI", 10)).grid(row=2, column=0, pady=(4, 0))

        self._hotkey("<Control-n>", self.new_chat)
        self._hotkey("<Control-k>", self.search_entry.focus_set)
        self._hotkey("<Escape>", lambda: self.stop.set() if self.busy else None)

    def _hotkey(self, seq, fn):
        def handler(e):
            fn()
            return "break"
        self.bind_all(seq, handler)
        self.input._textbox.bind(seq, handler)

    def _on_enter(self, e):
        if e.state & 0x1:  # Shift+Enter = quebra de linha
            return None
        self.on_send()
        return "break"

    def _tick(self):
        if self.busy:
            self._n += 1
            self.think.configure(text=("gerando" if self.reply_model else "pensando") + "." * (self._n % 4))
        elif self.think.cget("text"):
            self.think.configure(text="")
        self.after(350, self._tick)

    # ---------- ações chamadas pela área de conversa ----------
    def _redraw(self):
        msgs = db.get_msgs(self.conv_id) if self.conv_id else []
        atts = db.conv_attachments(self.conv_id) if self.conv_id else {}
        self.chat.render(msgs, atts, self.project["name"] if self.project else None)

    def pick_prompt(self, text):
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self.input.focus_set()

    def copy_text(self, text, btn):
        self.clipboard_clear()
        self.clipboard_append(text)
        btn.configure(text="Copiado")

        def reset():
            try:
                btn.configure(text="Copiar")
            except Exception:
                pass
        self.after(1400, reset)

    def save_code(self, code, lang):
        ext = EXT.get(lang.lower(), ".txt")
        path = filedialog.asksaveasfilename(defaultextension=ext, initialfile="arquivo" + ext)
        if path:
            Path(path).write_text(code, encoding="utf-8")

    def open_html(self, code):
        f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
        f.write(code)
        f.close()
        webbrowser.open(Path(f.name).as_uri())

    # ---------- conversas ----------
    def _load_convs(self):
        for w in self.convs.winfo_children():
            w.destroy()
        term = self.search_entry.get().strip().lower()
        for c in db.list_convs(self._pkey())[:100]:
            if term and term not in c["title"].lower():
                continue
            active = c["id"] == self.conv_id
            ctk.CTkButton(self.convs, text=c["title"], anchor="w",
                          fg_color=CARD if active else "transparent", hover_color=BORDER,
                          text_color=TEXT if active else MUTED,
                          command=lambda i=c["id"]: self.open_chat(i)).pack(fill="x", pady=1)

    def new_chat(self):
        if self.busy:
            return
        self.conv_id = None
        self._redraw()
        self._load_convs()

    def open_chat(self, conv_id):
        if self.busy:
            return
        self.conv_id = conv_id
        self._redraw()
        self._load_convs()

    def delete_chat(self):
        if self.conv_id and not self.busy:
            db.delete_conv(self.conv_id)
            self.new_chat()

    def remove_msg(self, msg_id):
        if self.busy:
            return
        db.delete_msg(msg_id)
        self._redraw()

    def continue_reply(self):
        if self.busy or not self.conv_id:
            return
        db.add_msg(self.conv_id, "user", "Continue exatamente de onde parou, sem repetir o que já escreveu.")
        self._redraw()
        self._start("continue")

    def regenerate(self, msg_id):
        if self.busy:
            return
        db.delete_msg(msg_id)
        msgs = db.get_msgs(self.conv_id)
        while msgs and msgs[-1]["role"] == "tool":
            db.delete_msg(msgs[-1]["id"])
            msgs = msgs[:-1]
        self._redraw()
        if msgs and msgs[-1]["role"] == "user":
            self._start(msgs[-1]["content"])

    # ---------- arquivos ----------
    def _load_docs(self):
        for w in self.docs.winfo_children():
            w.destroy()
        docs = db.list_docs()
        self._doc_count = len(docs)
        self._update_title()
        if not docs:
            ctk.CTkLabel(self.docs, text="Nenhum arquivo ainda", text_color=MUTED).pack(pady=8)
        # cada linha custa caro de desenhar: mostra os adicionados à mão primeiro e limita o resto
        docs = sorted(docs, key=lambda d: bool(d["managed"]))
        hidden = max(0, len(docs) - 40)
        for d in docs[:40]:
            row = ctk.CTkFrame(self.docs, fg_color="transparent")
            row.pack(fill="x")
            row.grid_columnconfigure(0, weight=1)
            name = d["name"] if len(d["name"]) <= 24 else d["name"][:21] + "..."
            ctk.CTkLabel(row, text=name, anchor="w", text_color=TEXT).grid(row=0, column=0, sticky="w")
            if d["managed"]:  # vem da pasta: para remover, apague o arquivo de lá
                ctk.CTkLabel(row, text="pasta", text_color=MUTED, font=("Segoe UI", 10)
                             ).grid(row=0, column=1, padx=6)
            else:
                ctk.CTkButton(row, text="×", width=24, height=22, fg_color="transparent", hover_color=BORDER,
                              text_color=MUTED, command=lambda i=d["id"]: self.remove_doc(i)
                              ).grid(row=0, column=1)
        if hidden:
            ctk.CTkLabel(self.docs, text=f"+ {hidden} arquivos da pasta (a IA lê todos)", text_color=MUTED,
                         font=("Segoe UI", 11)).pack(pady=6)

    def _update_title(self):
        s = self.stats
        if s and s["remaining"]:
            text = f"Meus arquivos ({s['indexed']}/{s['total']})"
        elif self._doc_count:
            text = f"Meus arquivos ({self._doc_count})"
        else:
            text = "Meus arquivos"
        self.docs_title.configure(text=text)

    def _apply_stats(self, out, line):
        # folder.sync() informa as alterações desta rodada (added, removed,
        # failed e pending); as chaves total/indexed eram de uma versão antiga
        # do sincronizador. Não deixar essa diferença travar o loop do Tk.
        added = int(out.get("added", 0))
        removed = int(out.get("removed", 0))
        failed_n = len(out.get("failed", []))
        remaining = int(out.get("pending", 0)) + failed_n
        indexed = max(0, self._doc_count + added - removed)
        total = indexed + remaining
        self.stats = {"total": total, "indexed": indexed, "failed": failed_n,
                      "remaining": remaining}
        extra = ""
        if remaining:
            extra = f"{indexed}/{total} indexados"
            if failed_n:
                extra += f" · {failed_n} com erro"
        text = " · ".join(x for x in (line, extra) if x)
        if text:
            self.docs_info.configure(text=text)
        if added or removed:
            self._load_docs()
        else:
            self._update_title()
        if out.get("pending") and not self.busy:  # ainda há arquivos para indexar: segue logo em seguida
            self.after(1500, self.sync_now)

    def add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(rag.SUPPORTED))
        paths = filedialog.askopenfilenames(title="Escolha os arquivos",
                                            filetypes=[("Documentos", exts), ("Todos", "*.*")])
        if paths:
            threading.Thread(target=self._ingest_worker, args=(list(paths),), daemon=True).start()

    def _ingest_worker(self, paths):
        for i, p in enumerate(paths, 1):
            name = Path(p).name
            self.q.put(("docinfo", f"Indexando {i}/{len(paths)}: {name}...", False))
            try:
                n = rag.ingest(self.cfg, p)
                self.q.put(("docinfo", f"{name}: {n} trechos indexados", True))
            except Exception as e:
                self.q.put(("docinfo", f"Falha em {name}: {e}", True))
        self.q.put(("docinfo", "Pronto.", True))

    def remove_doc(self, doc_id):
        db.delete_doc(doc_id)
        rag.invalidate()
        self._load_docs()

    # ---------- pasta vigiada ----------
    def open_folder(self):
        path = Path(self.cfg["watch_dir"])
        path.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(path)
        else:
            webbrowser.open(path.as_uri())

    def sync_now(self, manual=False):
        if self._syncing:
            return
        if manual:
            self.failed.clear()
            self.docs_info.configure(text="Verificando a pasta...")
        self._syncing = True
        threading.Thread(target=self._sync_worker, args=(manual,), daemon=True).start()

    def _sync_worker(self, manual):
        try:
            out = folder.sync(self.cfg, self.failed, lambda m: self.q.put(("docinfo", m, False)),
                              lambda: self.busy)
            msg = folder.summary(out)
            self.q.put(("stats", out, msg or ("Pasta em dia." if manual else "")))
        except Exception as e:
            self.q.put(("docinfo", f"Falha ao ler a pasta: {e}", True))
        finally:
            self._syncing = False

    def _auto_sync(self):
        if not self.busy:  # não disputa o Ollama com uma resposta em andamento
            self.sync_now()
        self.after(30000, self._auto_sync)

    # ---------- envio ----------
    def on_send(self):
        if self.busy:
            self.stop.set()
            return
        text = self.input.get("1.0", "end").strip()
        if not text and not self.pending:
            return
        text = text or "Veja o anexo."
        self.input.delete("1.0", "end")
        if self.conv_id is None:
            self.conv_id = db.new_conv(text, self._pkey())
            self._load_convs()
        mid = db.add_msg(self.conv_id, "user", text)
        for a in self.pending:
            db.add_attachment(mid, a["kind"], a["name"], a["path"], a["text"])
        self.pending = []
        self._render_pending()
        self._redraw()
        self._start(text)

    def _history(self):
        """Conversa no formato do modelo: textos dos arquivos anexados sempre; imagens só na última pergunta."""
        msgs = [m for m in db.get_msgs(self.conv_id) if m["role"] in ("user", "assistant")]
        atts = db.conv_attachments(self.conv_id)
        last_user = max((m["id"] for m in msgs if m["role"] == "user"), default=None)
        out = []
        for m in msgs:
            item = {"role": m["role"], "content": m["content"].replace(router.NOTICE, "")}
            images = []
            for a in atts.get(m["id"], []):
                if a["kind"] == "file":
                    item["content"] += f"\n\n[Arquivo anexado: {a['name']}]\n{a['text']}"
                elif m["id"] == last_user:
                    try:
                        images.append(base64.b64encode(Path(a["path"]).read_bytes()).decode())
                    except OSError:
                        item["content"] += f"\n[imagem {a['name']} não está mais disponível]"
                else:
                    item["content"] += f"\n[imagem anexada antes: {a['name']}]"
            if images:
                item["images"] = images
            out.append(item)
        return out

    def _start(self, text):
        hist = self._history()
        has_code = any("```" in h["content"] for h in hist[-4:])
        choice = self.model_var.get()
        ctx_over = CTX_CHOICES.get(self.ctx_var.get())
        wants_img = bool(hist and hist[-1].get("images"))
        if self.project:  # modo Projeto: precisa de um modelo que use ferramentas
            model = choice.split("  ·")[0] if choice != AUTO else None
            if not model or router.is_gemini(model) or not router.supports(model, "tools"):
                model = agent.pick_model(self.cfg, self.installed, self.online)
            if not model:
                self.chat.add_note("Nenhum modelo com suporte a ferramentas foi encontrado. "
                                   "Instale um, por exemplo: ollama pull qwen2.5-coder:7b", error=True)
                return
            if wants_img and not router.supports(model, "vision"):
                for h in hist:
                    h.pop("images", None)
                self.chat.add_note(f"{model} não lê imagens; a imagem foi ignorada.")
        else:
            model = (router.pick_model(self.cfg, text, self.installed, self.online, code=has_code)
                     if choice == AUTO else choice.split("  ·")[0])
            if wants_img and not router.supports(model, "vision"):
                vm = router.pick_vision_model(self.cfg, self.installed, self.online)
                if not vm:
                    self.chat.add_note("Nenhum modelo que leia imagens foi encontrado. Instale um, por exemplo: "
                                       "ollama pull gemma3:4b  (ou configure a chave do Gemini).", error=True)
                    return
                self.chat.add_note(f"{model} não lê imagens; usando {vm} nesta resposta.")
                model = vm
        if router.is_cloud(model):
            budget = 60000
        else:
            ctx, pred = router.model_opts(self.cfg, model, ctx_over)
            budget = max(4000, int((ctx - min(pred, ctx // 2)) * 2.5))  # ~2,5 caracteres por token
        history = trim_history(hist[-20:], budget)
        use_rag = self.rag_var.get() and bool(db.list_docs()) and not self.project
        self.busy, self.reply, self.reply_model, self.sources, self.err = True, [], None, [], None
        self.stop.clear()
        self.send_btn.configure(text="■", fg_color=RED, hover_color="#d94a58")
        self.chat.begin_typing()
        if self.project:
            threading.Thread(target=self._agent_worker, args=(model, history, ctx_over), daemon=True).start()
        else:
            threading.Thread(target=self._worker, args=(model, history, text, use_rag, ctx_over),
                             daemon=True).start()

    # ---------- modo Projeto ----------
    def _agent_worker(self, model, history, ctx_over):
        proj, conv = self.project, self.conv_id
        ws = tools.Workspace(proj["path"])
        state = {"text": False}

        def on_text(s):
            state["text"] = True
            self.q.put(("chunk", model, s))

        def on_step(kind, summary, detail, ok):
            state["text"] = False
            db.add_msg(conv, "tool", json.dumps({"kind": kind, "summary": summary, "ok": ok,
                                                 "detail": (detail or "")[:6000]}, ensure_ascii=False))
            self.q.put(("step", kind, summary, detail or "", ok))
        ui = agent.UI(text=on_text, step=on_step, ask=self._ask,
                      auto=lambda: self.cfg.get("agent_approval") == "auto" or self.session_auto)
        try:
            agent.run(self.cfg, model, history, ws, proj["name"], self.stop, ctx_over, ui)
            if not state["text"] and not self.stop.is_set():
                on_text("Concluído." if ws.journal else "Ok.")
            self._save_changes(conv, ws)
            self.q.put(("done",))
        except Exception as e:
            self._save_changes(conv, ws)
            self.q.put(("error", str(e)))

    def _save_changes(self, conv, ws):
        if ws.journal:
            db.add_msg(conv, "tool", json.dumps({"kind": "changes", "summary": f"{len(ws.journal)} alterações",
                                                 "detail": {"root": str(ws.root), "items": ws.journal}},
                                                ensure_ascii=False))

    def _ask(self, kind, summary, preview):
        """Chamada pela thread do agente: espera o clique em Permitir/Negar na tela."""
        token, ev, box = uuid.uuid4().hex, threading.Event(), {"ok": False}
        self._approvals[token] = (ev, box)
        self.q.put(("approve", token, kind, summary, preview))
        while not ev.wait(0.2):
            if self.stop.is_set():
                return False
        return box["ok"]

    def _decide(self, token, choice):
        ev, box = self._approvals.pop(token, (None, None))
        if choice == "all":
            self.session_auto = True
        if ev:
            box["ok"] = choice in ("yes", "all")
            ev.set()

    def undo_changes(self, msg_id):
        row = next((m for m in db.get_msgs(self.conv_id) if m["id"] == msg_id), None)
        if not row or self.busy:
            return
        d = json.loads(row["content"])
        n = tools.undo(d["detail"]["root"], d["detail"]["items"])
        d["undone"] = True
        db.update_msg_content(msg_id, json.dumps(d, ensure_ascii=False))
        self._redraw()
        self.chat.add_note(f"{n} alteração(ões) desfeitas.")

    def _pkey(self):
        return self.project["path"] if self.project else ""

    def _refresh_project_menu(self):
        self.proj_menu.configure(values=[NO_PROJECT] + [p["name"] for p in self.cfg["projects"]] + [ADD_PROJECT])
        self.proj_var.set(self.project["name"] if self.project else NO_PROJECT)
        if self.project:
            self.appr_menu.pack(side="left", padx=(8, 0), before=self.att_bar)
        else:
            self.appr_menu.pack_forget()

    def _on_project(self, choice):
        if self.busy:
            self.proj_var.set(self.project["name"] if self.project else NO_PROJECT)
            return
        if choice == ADD_PROJECT:
            self.add_project()
            return
        self._set_project(next((p for p in self.cfg["projects"] if p["name"] == choice), None))

    def _set_project(self, proj):
        if proj and not Path(proj["path"]).is_dir():
            self.chat.add_note(f"A pasta do projeto não existe mais: {proj['path']}", error=True)
            proj = None
        self.project, self.conv_id, self.session_auto = proj, None, False
        self._refresh_project_menu()
        self._redraw()
        self._load_convs()

    def _unique_project_name(self, name):
        names, n, new = {p["name"] for p in self.cfg["projects"]}, 2, name
        while new in names or new in (NO_PROJECT, ADD_PROJECT):
            new, n = f"{name} ({n})", n + 1
        return new

    def add_project(self):
        d = filedialog.askdirectory(title="Escolha a pasta do projeto")
        if not d:
            self.proj_var.set(self.project["name"] if self.project else NO_PROJECT)
            return
        proj = {"name": self._unique_project_name(Path(d).name or d), "path": str(Path(d))}
        self.cfg["projects"].append(proj)
        config.save(self.cfg)
        self._set_project(proj)

    def _on_approval(self, choice):
        self.cfg["agent_approval"] = APPR_LABELS[choice]
        self.session_auto = False
        config.save(self.cfg)

    # ---------- anexos ----------
    def attach_dialog(self):
        paths = filedialog.askopenfilenames(title="Anexar imagens ou arquivos", filetypes=[
            ("Imagens e documentos", "*.png *.jpg *.jpeg *.webp *.gif *.bmp *.pdf *.docx *.xlsx *.txt *.md *.csv "
                                      "*.json *.py *.js *.html *.css *.sql"), ("Todos", "*.*")])
        for p in paths:
            self._attach_path(p)

    def _attach_path(self, path):
        p = Path(path)
        if len(self.pending) >= 8:
            self.chat.add_note("Máximo de 8 anexos por mensagem.")
            return
        if p.suffix.lower() in IMG_EXT:
            try:
                self._attach_image(Image.open(p), p.name)
            except Exception as e:
                self.chat.add_note(f"Não foi possível abrir {p.name}: {e}", error=True)
        else:  # ler PDF/DOCX pode demorar: vai para uma thread
            threading.Thread(target=self._attach_file_worker, args=(p,), daemon=True).start()

    def _attach_image(self, img, name):
        img.load()
        alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        img = img.convert("RGBA" if alpha else "RGB")
        img.thumbnail((1568, 1568))  # tamanho suficiente para o modelo e leve para enviar
        ATT_DIR.mkdir(parents=True, exist_ok=True)
        ext = ".png" if alpha else ".jpg"
        dest = ATT_DIR / f"{uuid.uuid4().hex[:10]}{ext}"
        img.save(dest) if alpha else img.save(dest, quality=90)
        self.pending.append({"kind": "image", "name": Path(name).stem + ext, "path": str(dest), "text": ""})
        self._render_pending()

    def _attach_file_worker(self, p):
        try:
            text = rag.extract(p)
            if not text.strip():
                raise ValueError("não encontrei texto (PDF escaneado?)")
            ATT_DIR.mkdir(parents=True, exist_ok=True)
            dest = ATT_DIR / f"{uuid.uuid4().hex[:10]}_{p.name}"
            shutil.copy2(p, dest)
            if len(text) > MAX_ATT_CHARS:
                text = text[:MAX_ATT_CHARS] + "\n[... arquivo cortado: só o começo foi enviado ...]"
            self.q.put(("attached", {"kind": "file", "name": p.name, "path": str(dest), "text": text}, ""))
        except Exception as e:
            self.q.put(("attached", None, f"Não foi possível anexar {p.name}: {e}"))

    def _render_pending(self):
        for w in self.att_bar.winfo_children():
            w.destroy()
        for i, a in enumerate(self.pending):
            chip = ctk.CTkFrame(self.att_bar, fg_color=CARD, border_width=1, border_color=BORDER, corner_radius=8)
            chip.pack(side="left", padx=3)
            img = None
            if a["kind"] == "image":
                try:
                    im = Image.open(a["path"])
                    img = ctk.CTkImage(light_image=im, size=(26, int(26 * im.height / im.width) or 26))
                except Exception:
                    img = None
            name = a["name"] if len(a["name"]) <= 16 else a["name"][:13] + "..."
            lbl = ctk.CTkLabel(chip, text=name if img else "📄 " + name, image=img, compound="left",
                               font=("Segoe UI", 11), text_color=MUTED)
            lbl.image = img
            lbl.pack(side="left", padx=(6, 0))
            ctk.CTkButton(chip, text="×", width=22, height=22, fg_color="transparent", hover_color=BORDER,
                          text_color=MUTED, command=lambda i=i: self._remove_pending(i)).pack(side="left")

    def _remove_pending(self, i):
        if i < len(self.pending):
            self.pending.pop(i)
        self._render_pending()

    def _on_paste(self, e):
        """Ctrl+V: cola imagem da área de transferência (print) ou arquivos copiados no Explorer."""
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            clip = None
        if isinstance(clip, Image.Image):
            self._attach_image(clip, "captura.png")
            return "break"
        if isinstance(clip, list) and clip:
            for f in clip:
                self._attach_path(f)
            return "break"
        return None  # texto comum: deixa colar normalmente

    def _worker(self, model, history, text, use_rag, ctx_over=None):
        try:
            if use_rag:
                cat = rag.catalog_block(self.stats) if rag.catalog_question(text) else None
                hits = []
                try:
                    hits = rag.search(self.cfg, text)
                except Exception as e:
                    self.q.put(("note", f"[busca nos arquivos ignorada: {e}]"))
                if hits or cat:
                    history[-1]["content"] = rag.build_prompt(text, hits, cat)
                    if hits:
                        self.q.put(("sources", sorted({h[0] for h in hits})))
            for used, chunk in router.stream_with_fallback(self.cfg, model, history, self.stop, self.installed,
                                                      ctx_over):
                self.q.put(("chunk", used, chunk))
            self.q.put(("done",))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _finish(self):
        if self.reply:
            if self.sources:
                self.reply.append("\n\nFontes: " + ", ".join(self.sources))
            db.add_msg(self.conv_id, "assistant", "".join(self.reply), self.reply_model)
        self.busy = False
        self.send_btn.configure(text="↑", fg_color=ACCENT, hover_color="#6a4be0")
        self._redraw()
        if self.err:
            self.chat.add_note(f"Erro: {self.err}", error=True)
            self.err = None

    def _poll(self):
        pend = []  # pedaços de texto do mesmo ciclo viram uma única atualização da tela

        def flush():
            if pend:
                self.chat.stream_append(self.reply_model, "".join(pend))
                pend.clear()
        try:
            while True:
                kind, *a = self.q.get_nowait()
                if kind == "chunk":
                    model, text = a
                    if self.reply_model is None:
                        self.reply_model = model
                    self.reply.append(text)
                    pend.append(text)
                    continue
                flush()
                if kind == "sources":
                    self.sources = a[0]
                elif kind == "note":
                    self.chat.add_note(a[0])
                elif kind == "done":
                    self._finish()
                elif kind == "error":
                    self.err = a[0]
                    self._finish()
                elif kind == "status":
                    self._apply_status(*a)
                elif kind == "step":
                    self.reply.clear()  # o texto de antes do passo fica no cartão antigo; a resposta final é só a última parte
                    self.chat.add_step(*a)
                elif kind == "approve":
                    token, k, summary, preview = a
                    self.chat.add_approval(k, summary, preview, lambda c, t=token: self._decide(t, c))
                elif kind == "attached":
                    item, err = a
                    if err:
                        self.chat.add_note(err, error=True)
                    elif item:
                        self.pending.append(item)
                        self._render_pending()
                elif kind == "stats":
                    self._apply_stats(*a)
                elif kind == "docinfo":
                    self.docs_info.configure(text=a[0])
                    if a[1]:
                        self._load_docs()
        except queue.Empty:
            pass
        except Exception as e:
            # Eventos de fundo (indexação, status, modelos) nunca podem parar
            # o agendador da interface; caso contrário ela fica em "verificando".
            self.docs_info.configure(text=f"Falha ao atualizar a tela: {e}")
        finally:
            flush()
            self.after(40, self._poll)

    # ---------- status online/offline + modelos ----------
    def refresh_status(self):
        known = set(self.limits)

        def job():
            inst = router.ollama_models(self.cfg)
            lim = {}
            for m in inst:
                if m in router.CAPS and (m in known or router.is_cloud(m)):
                    continue
                ctx = router.model_max_ctx(self.cfg, m)  # também descobre se o modelo lê imagens/usa ferramentas
                if ctx and not router.is_cloud(m):
                    lim[m] = ctx
            self.q.put(("status", router.is_online(), inst, lim))
        threading.Thread(target=job, daemon=True).start()
        self.after(20000, self.refresh_status)

    def _apply_status(self, online, installed, limits):
        self.online, self.installed = online, installed
        self.limits.update(limits)
        router.LIMITS.update(limits)
        gem = self.cfg["gemini_models"] if self.cfg["gemini_key"] else []
        names = [AUTO] + [f"{m}  · nuvem" if router.is_cloud(m) else m for m in installed] + gem
        if self.model_var.get() not in names:
            self.model_var.set(AUTO)
        self.model_menu.configure(values=names)
        ollama = "Ollama ok" if installed else "Ollama off"
        self.status.configure(text=f"● {'online' if online else 'offline'} · {ollama}",
                              text_color=GREEN if online else MUTED)

    # ---------- configurações ----------
    def _slider(self, parent, title, lo, hi, step, value, on_change):
        lo = int(lo)
        hi = int(max(hi, lo + step))
        box = ctk.CTkFrame(parent, fg_color="transparent")
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=title, text_color=TEXT).pack(side="left")
        val = ctk.CTkLabel(head, text="", text_color=ACCENT)
        val.pack(side="right")
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text=fmt(lo), text_color=MUTED, font=("Segoe UI", 11), width=56).pack(side="left")
        sl = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=max(1, (hi - lo) // step), fg_color=BORDER,
                           progress_color=ACCENT, button_color=ACCENT, button_hover_color="#6a4be0")
        sl.pack(side="left", fill="x", expand=True, padx=8)
        ctk.CTkLabel(row, text=fmt(hi), text_color=MUTED, font=("Segoe UI", 11), width=64).pack(side="left")

        def set_value(v, notify=False):
            v = int(min(hi, max(lo, lo + round((v - lo) / step) * step)))
            sl.set(v)
            val.configure(text=f"{fmt(v)} tokens")
            if notify:
                on_change(v)
        sl.configure(command=lambda v: set_value(v, True))
        set_value(value)
        return box, set_value

    def _model_card(self, parent, name, draft):
        mx = self.limits.get(name) or 32768
        cur = draft["models"].get(name, {})
        card = ctk.CTkFrame(parent, fg_color=CARD, border_width=1, border_color=BORDER, corner_radius=10)
        card.pack(fill="x", pady=6)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(head, text=name, font=("Segoe UI", 13, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(head, text=f"   máximo do modelo: {fmt(mx)}", text_color=MUTED,
                     font=("Segoe UI", 11)).pack(side="left")

        def put(key):
            return lambda v: draft["models"].setdefault(name, {}).update({key: v})
        ctx_w, set_ctx = self._slider(card, "Contexto (conversa inteira)", min(2048, mx), mx, 1024,
                                      cur.get("num_ctx") or min(draft["num_ctx"], mx), put("num_ctx"))
        ctx_w.pack(fill="x", padx=14, pady=(6, 2))
        pmax = min(16384, mx)
        pred_w, set_pred = self._slider(card, "Resposta (tamanho máximo)", min(256, pmax), pmax, 256,
                                        cur.get("num_predict") or min(draft["num_predict"], pmax), put("num_predict"))
        pred_w.pack(fill="x", padx=14, pady=(2, 12))

        def reset():
            draft["models"].pop(name, None)
            set_ctx(min(draft["num_ctx"], mx))
            set_pred(min(draft["num_predict"], pmax))
        ctk.CTkButton(head, text="Padrão", width=64, height=22, fg_color="transparent", hover_color=BORDER,
                      text_color=MUTED, font=("Segoe UI", 11), command=reset).pack(side="right")

    def open_settings(self):
        if self._win is not None and self._win.winfo_exists():
            self._win.focus()
            return
        draft = copy.deepcopy({k: self.cfg[k] for k in config.USER_KEYS})
        win = self._win = ctk.CTkToplevel(self)
        win.title("Configurações")
        win.geometry("700x780")
        win.configure(fg_color=BG)
        win.after(150, win.focus)

        body = ctk.CTkScrollableFrame(win, fg_color="transparent", scrollbar_button_color=BORDER,
                                      scrollbar_button_hover_color=MUTED)
        body.pack(fill="both", expand=True, padx=16, pady=(16, 0))

        def title(text, hint=""):
            ctk.CTkLabel(body, text=text, font=("Segoe UI", 16, "bold"), text_color=TEXT, anchor="w"
                         ).pack(fill="x", pady=(14, 0))
            if hint:
                ctk.CTkLabel(body, text=hint, text_color=MUTED, font=("Segoe UI", 11), anchor="w",
                             justify="left", wraplength=620).pack(fill="x", pady=(0, 4))

        title("Gemini", "Chave da API para usar os modelos Gemini quando houver internet.")
        key = ctk.CTkEntry(body, show="•", fg_color=CARD, border_color=BORDER, placeholder_text="Cole a chave aqui")
        if draft["gemini_key"]:
            key.insert(0, draft["gemini_key"])
        key.pack(fill="x", pady=4)

        title("Pasta de arquivos", "Tudo que você colocar nesta pasta é lido e indexado sozinho (PDF, DOCX, XLSX, "
                                   "TXT, CSV e outros). Para tirar um arquivo da IA, apague-o da pasta. "
                                   "Ao trocar de pasta, os arquivos da pasta antiga saem da base.")
        path_lbl = ctk.CTkLabel(body, text=draft["watch_dir"], text_color=MUTED, anchor="w", justify="left",
                                wraplength=620)
        path_lbl.pack(fill="x", pady=(2, 4))
        prow = ctk.CTkFrame(body, fg_color="transparent")
        prow.pack(fill="x")

        def choose():
            d = filedialog.askdirectory(title="Escolha a pasta de arquivos", initialdir=draft["watch_dir"],
                                        parent=win)
            if d:
                draft["watch_dir"] = str(Path(d))
                path_lbl.configure(text=draft["watch_dir"])
        ctk.CTkButton(prow, text="Escolher outra pasta", fg_color=CARD, hover_color=BORDER, border_width=1,
                      border_color=BORDER, text_color=MUTED, command=choose).pack(side="left")
        ctk.CTkButton(prow, text="Abrir pasta", fg_color=CARD, hover_color=BORDER, border_width=1,
                      border_color=BORDER, text_color=MUTED, command=self.open_folder).pack(side="left", padx=8)

        title("Projetos", "Pastas onde a IA pode ler, criar, editar e apagar arquivos (modo Projeto). "
                          "Escolha o projeto no menu acima da caixa de mensagem.")
        plist = ctk.CTkFrame(body, fg_color="transparent")
        plist.pack(fill="x")

        def draw_projects():
            for w in plist.winfo_children():
                w.destroy()
            if not draft["projects"]:
                ctk.CTkLabel(plist, text="Nenhum projeto ainda.", text_color=MUTED).pack(anchor="w")
            for pr in list(draft["projects"]):
                row = ctk.CTkFrame(plist, fg_color=CARD, border_width=1, border_color=BORDER, corner_radius=8)
                row.pack(fill="x", pady=3)
                ctk.CTkLabel(row, text=f"{pr['name']}   {pr['path']}", text_color=TEXT, anchor="w",
                             font=("Segoe UI", 12)).pack(side="left", padx=10, pady=6)
                ctk.CTkButton(row, text="Remover", width=70, height=24, fg_color="transparent", hover_color=BORDER,
                              text_color=MUTED, command=lambda pr=pr: (draft["projects"].remove(pr), draw_projects())
                              ).pack(side="right", padx=6)

        def add_proj():
            d = filedialog.askdirectory(title="Escolha a pasta do projeto", parent=win)
            if d:
                base, n, new = Path(d).name or d, 2, Path(d).name or d
                while new in {p["name"] for p in draft["projects"]} | {NO_PROJECT, ADD_PROJECT}:
                    new, n = f"{base} ({n})", n + 1
                draft["projects"].append({"name": new, "path": str(Path(d))})
                draw_projects()
        draw_projects()
        ctk.CTkButton(body, text="+ Adicionar pasta de projeto", fg_color=CARD, hover_color=BORDER, border_width=1,
                      border_color=BORDER, text_color=MUTED, command=add_proj).pack(anchor="w", pady=6)

        title("Padrões gerais", "Valem para todos os modelos que você não ajustou abaixo. "
                                "Valores altos usam mais memória e deixam o começo da resposta mais lento.")
        g1, _ = self._slider(body, "Contexto padrão", 2048, 131072, 1024, draft["num_ctx"],
                             lambda v: draft.update(num_ctx=v))
        g1.pack(fill="x", pady=4)
        g2, _ = self._slider(body, "Resposta padrão", 256, 16384, 256, draft["num_predict"],
                             lambda v: draft.update(num_predict=v))
        g2.pack(fill="x", pady=4)

        title("Por modelo", "O máximo de cada modelo vem do próprio Ollama. Se o modelo foi instalado agora, "
                            "feche e abra esta janela para ele aparecer.")
        local = [m for m in self.installed if not router.is_cloud(m)]
        cloud = [m for m in self.installed if router.is_cloud(m)]
        if not local and not cloud:
            ctk.CTkLabel(body, text="Nenhum modelo do Ollama encontrado. Abra o Ollama e tente de novo.",
                         text_color=MUTED).pack(fill="x", pady=8)
        for m in sorted(local):
            self._model_card(body, m, draft)
        for m in sorted(cloud):
            ctk.CTkLabel(body, text=f"{m}: modelo de nuvem, o limite é definido pelo servidor (só a resposta é ajustável).",
                         text_color=MUTED, font=("Segoe UI", 11), anchor="w", justify="left", wraplength=620
                         ).pack(fill="x", pady=2)

        foot = ctk.CTkFrame(win, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=14)

        def save():
            draft["gemini_key"] = key.get().strip()
            if draft["watch_dir"] != self.cfg["watch_dir"]:
                self.failed.clear()
            for k in config.USER_KEYS:
                self.cfg[k] = draft[k]
            config.save(self.cfg)
            win.destroy()
            if self.project and self.project["path"] not in {p["path"] for p in self.cfg["projects"]}:
                self._set_project(None)  # o projeto aberto foi removido
            else:
                self._refresh_project_menu()
            self.refresh_status()
            self.sync_now()
        ctk.CTkButton(foot, text="Salvar", fg_color=ACCENT, hover_color="#6a4be0", command=save
                      ).pack(side="right")
        ctk.CTkButton(foot, text="Cancelar", fg_color=CARD, hover_color=BORDER, text_color=MUTED,
                      command=win.destroy).pack(side="right", padx=8)
