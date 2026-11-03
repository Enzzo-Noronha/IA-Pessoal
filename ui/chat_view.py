"""Área de conversa: balões, cartões, blocos de código com cores e animações."""
import json
import re
import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk
from PIL import Image

from core import config, router

BG2, CARD, BORDER = "#0f0f15", "#15151d", "#262631"
TEXT, MUTED, ACCENT, GREEN, RED = "#e6e6ec", "#8b8b99", "#7c5cff", "#3ddc97", "#ff5c6c"
USER_BG, CODE_BG = "#3b3199", "#0d0d13"

FENCE = re.compile(r"```([\w+#.-]*)[ \t]*\n(.*?)```", re.S)
INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`\n]+)`")

SUGGESTIONS = [
    ("🌐", "Criar uma landing page", "Crie uma landing page de barbearia com tema escuro, em HTML, CSS e JavaScript."),
    ("📄", "Resumir meus arquivos", "Resuma os principais pontos dos meus arquivos."),
    ("🧩", "Explicar um código", "Explique este código passo a passo:\n\n"),
    ("📸", "Legendas para Instagram", "Escreva 5 legendas curtas para posts de perfumes importados no Instagram."),
]

PROJECT_SUGGESTIONS = [
    ("🧭", "Explicar este projeto", "Explore o projeto e me explique como ele está organizado e o que cada parte faz."),
    ("🐞", "Procurar bugs", "Leia o código principal e procure bugs ou problemas. Liste o que encontrar antes de mudar algo."),
    ("✨", "Adicionar uma função", "Quero adicionar uma nova função neste projeto: "),
    ("🧪", "Criar testes", "Crie testes automatizados para as partes mais importantes deste projeto e rode-os."),
]
STEP_COLORS = {"read": "#6b7a99", "list": "#6b7a99", "search": "#6b7a99", "write": GREEN, "edit": GREEN,
               "mkdir": GREEN, "delete": RED, "move": "#4cc9f0", "run": "#ffb454", "error": RED}

LANG_COLORS = {"html": "#e44d26", "css": "#2f7bf5", "javascript": "#f7df1e", "js": "#f7df1e", "python": "#4b8bbe",
               "py": "#4b8bbe", "json": "#8bc34a", "sql": "#e38c00", "bash": "#4eaa25", "sh": "#4eaa25"}

# ---------- realce de sintaxe ----------
KW = {
    "py": set("def class return if elif else for while import from as with try except finally raise lambda None True "
              "False in is not and or pass break continue yield async await self global del assert".split()),
    "js": set("const let var function return if else for while do switch case break continue class new this import "
              "export from default async await try catch finally throw null undefined true false typeof instanceof "
              "in of void delete extends super static".split()),
    "sql": set("select from where insert into values update set delete create table alter drop join left right inner "
               "outer on group by order having limit as and or not null primary key foreign references default "
               "distinct union count sum avg min max like in is between exists case when then else end".split()),
    "sh": set("if then else elif fi for do done while case esac function echo cd ls cat grep export source return "
              "exit in".split()),
}
KW["gen"] = KW["py"] | KW["js"]
LANG = {"python": "py", "py": "py", "javascript": "js", "js": "js", "jsx": "js", "ts": "js", "typescript": "js",
        "node": "js", "json": "json", "html": "html", "xml": "html", "css": "css", "sql": "sql", "bash": "sh",
        "sh": "sh", "shell": "sh", "bat": "sh", "powershell": "sh"}
COMMENT = {"py": r"#[^\n]*", "sh": r"#[^\n]*", "js": r"//[^\n]*|/\*.*?\*/", "css": r"/\*.*?\*/",
           "html": r"<!--.*?-->", "sql": r"--[^\n]*|/\*.*?\*/", "json": r"(?!x)x"}
SYN = {"kw": "#c678dd", "string": "#98c379", "comment": "#5c6370", "number": "#d19a66", "tag": "#e06c75",
       "attr": "#d19a66", "func": "#61afef"}
_PAT = {}


def _pattern(kind):
    parts = [f"(?P<comment>{COMMENT.get(kind, r'//[^' + chr(10) + r']*|#[^' + chr(10) + r']*')})",
             r"""(?P<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`[^`]*`)"""]
    if kind == "html":
        parts += [r"(?P<tag></?[A-Za-z][\w-]*|/?>)", r"(?P<attr>[A-Za-z_:@-][\w:.-]*(?==))"]
    elif kind == "css":
        parts += [r"(?P<number>#[0-9a-fA-F]{3,8}\b|-?\d*\.?\d+(?:px|rem|em|%|vh|vw|s|ms|deg)?)",
                  r"(?P<attr>[A-Za-z-]+(?=\s*:))", r"(?P<func>[A-Za-z-]+(?=\())"]
    else:
        parts += [r"(?P<number>\b\d+(?:\.\d+)?\b)", r"(?P<func>[A-Za-z_]\w*(?=\())", r"(?P<word>[A-Za-z_]\w*)"]
    return re.compile("|".join(parts), re.S | (re.I if kind == "sql" else 0))


def highlight(t, code, lang):
    if len(code) > 20000:
        return
    kind = LANG.get((lang or "").lower(), "gen")
    pat = _PAT.get(kind) or _PAT.setdefault(kind, _pattern(kind))
    kws = KW.get(kind, KW["gen"])
    for m in pat.finditer(code):
        g, w = m.lastgroup, m.group()
        if g == "word":
            if (w.lower() if kind == "sql" else w) not in kws:
                continue
            g = "kw"
        elif g == "func" and w in kws:
            g = "kw"
        elif g == "string" and kind == "json" and code[m.end():].lstrip()[:1] == ":":
            g = "attr"
        t.tag_add(g, f"1.0+{m.start()}c", f"1.0+{m.end()}c")


def split_blocks(text):
    """Divide o texto em trechos normais e blocos de código (```lang ... ```)."""
    out, pos = [], 0
    for m in FENCE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()], ""))
        out.append(("code", m.group(2), m.group(1)))
        pos = m.end()
    tail = text[pos:]
    i = tail.find("```")
    if i != -1:  # bloco aberto e nunca fechado (resposta cortada)
        if i:
            out.append(("text", tail[:i], ""))
        lang, _, code = tail[i + 3:].partition("\n")
        out.append(("code", code, lang.strip()))
    elif tail:
        out.append(("text", tail, ""))
    return out


def chip_colors(model):
    m = model.lower()
    if "cloud" in m or m.startswith("gemini"):
        return "#4cc9f0", "#112d38"
    if "coder" in m or m.startswith("dev-"):
        return "#ffb454", "#38280f"
    return "#3ddc97", "#10332a"


class ChatView(ctk.CTkScrollableFrame):
    def __init__(self, master, host):
        super().__init__(master, fg_color=BG2, corner_radius=14, border_width=1, border_color=BORDER,
                         scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
        self.host = host
        self._typing, self._job, self._fonts, self._rj = None, None, {}, None

    # ---------- utilidades ----------
    def _sc(self):
        try:
            return ctk.ScalingTracker.get_window_scaling(self.winfo_toplevel())
        except Exception:
            return 1.0

    def _font(self, size, weight="normal", family="Segoe UI"):
        key = (family, size, weight)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(family=family, size=-max(1, int(size * self._sc())), weight=weight)
        return self._fonts[key]

    def _text(self, parent, bg, fg=TEXT, size=15, width=1):
        t = tk.Text(parent, wrap="word", bg=bg, fg=fg, relief="flat", bd=0, highlightthickness=0, padx=0, pady=0,
                    width=width, height=1, font=self._font(size), cursor="arrow", takefocus=0, insertwidth=0,
                    selectbackground="#3d3d66", selectforeground="#ffffff", spacing3=3)
        t.tag_config("b", font=self._font(size, "bold"))
        t.tag_config("code", font=self._font(size - 1, family="Consolas"), background="#242432",
                     foreground="#ffb454")
        for i, extra in enumerate((6, 4, 2), 1):
            t.tag_config(f"h{i}", font=self._font(size + extra, "bold"), spacing1=6)
        t._base = self._font(size)
        t.bind("<Configure>", lambda e, t=t: self._fit(t))
        return t

    def _fit(self, t):
        """Ajusta a altura do texto ao número de linhas visíveis (acompanha o redimensionamento)."""
        if t.winfo_width() < 30:
            return
        try:
            n = t.count("1.0", "end", "update", "displaylines")  # até "end": conta todas as linhas visuais
        except tk.TclError:
            return
        if isinstance(n, tuple):
            n = n[0] if n else 1
        n = max(1, int(n or 1))
        if getattr(t, "_big", False):  # títulos usam fonte maior: mede em pixels para não cortar o fim
            try:
                px = t.count("1.0", "end", "update", "ypixels")
                px = px[0] if isinstance(px, tuple) else px
                base = t._base.metrics("linespace") + 3
                n = max(n, -(-int(px or 0) // base))
            except (tk.TclError, TypeError, AttributeError):
                pass
        if int(t.cget("height")) != n:
            t.configure(height=n)
            self._queue_region()

    def _mini(self, parent, text, cmd=None):
        return ctk.CTkButton(parent, text=text, width=70, height=24, fg_color="transparent", hover_color=BORDER,
                             text_color=MUTED, font=("Segoe UI", 11), corner_radius=8, command=cmd)

    def _md(self, t, text):
        """Markdown básico: títulos, listas, **negrito** e `código`."""
        t.configure(state="normal")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            tag = None
            h = re.match(r"^(#{1,3})\s+(.*)", line)
            if h:
                line, tag = h.group(2), f"h{len(h.group(1))}"
                t._big = True
            else:
                line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
            pos = 0
            for m in INLINE.finditer(line):
                t.insert("end", line[pos:m.start()], tag)
                if m.group(1) is not None:
                    t.insert("end", m.group(1), ("b", tag) if tag else "b")
                else:
                    t.insert("end", m.group(2), "code")
                pos = m.end()
            t.insert("end", line[pos:], tag)
            if i < len(lines) - 1:
                t.insert("end", "\n")
        t.configure(state="disabled")

    # ---------- controle ----------
    def clear(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self._typing = None
        for w in self.winfo_children():
            w.destroy()
        self._queue_region()

    # ---------- área de rolagem ----------
    def _sync_region(self):
        """A área de rolagem do CTkScrollableFrame não encolhe sozinha quando o conteúdo diminui
        (acontece quando a resposta em texto puro vira blocos de código). Aqui ela é refeita à mão."""
        try:
            self.update_idletasks()
            w = max(self._parent_canvas.winfo_width(), 1)
            h = max(self.winfo_reqheight(), self._parent_canvas.winfo_height())
            self._parent_canvas.configure(scrollregion=(0, 0, w, h))
        except tk.TclError:
            pass

    def _queue_region(self):
        if self._rj is None:
            self._rj = self.after(60, self._region_job)

    def _region_job(self):
        self._rj = None
        self._sync_region()

    def scroll_end(self):
        def go():
            self._sync_region()
            self._parent_canvas.yview_moveto(1.0)
        self.after(30, go)

    def _at_bottom(self):
        return self._parent_canvas.yview()[1] >= 0.96

    def render(self, msgs, atts=None, project=None):
        self.clear()
        if not msgs:
            self.show_welcome(project)
            return
        atts = atts or {}
        last = max((i for i, m in enumerate(msgs) if m["role"] in ("user", "assistant")), default=-1)
        for i, m in enumerate(msgs):
            if m["role"] == "user":
                self.add_user(m["content"], atts.get(m["id"], []))
            elif m["role"] == "tool":
                self._tool_row(m)
            else:
                self.add_assistant(m["content"], m["model"], m["id"], i == last)
        self.scroll_end()
        self.after(250, self.scroll_end)
        self.after(700, self.scroll_end)  # os textos terminam de se ajustar um pouco depois

    # ---------- telas ----------
    def show_welcome(self, project=None):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(expand=True, pady=(70, 0))
        ctk.CTkLabel(wrap, text=config.APP_NAME, font=("Segoe UI", 34, "bold"), text_color=TEXT).pack()
        sub = f"Projeto: {project}. O que vamos fazer nele?" if project else "Como posso ajudar hoje?"
        ctk.CTkLabel(wrap, text=sub, font=("Segoe UI", 15), text_color=MUTED).pack(pady=(4, 26))
        grid = ctk.CTkFrame(wrap, fg_color="transparent")
        grid.pack()
        for i, (icon, title, prompt) in enumerate(PROJECT_SUGGESTIONS if project else SUGGESTIONS):
            ctk.CTkButton(grid, text=f"{icon}   {title}", anchor="w", width=250, height=54, corner_radius=12,
                          fg_color=CARD, hover_color="#1f1f2b", border_width=1, border_color=BORDER,
                          text_color=TEXT, font=("Segoe UI", 13), command=lambda p=prompt: self.host.pick_prompt(p)
                          ).grid(row=i // 2, column=i % 2, padx=6, pady=6)

    def add_note(self, text, error=False):
        box = ctk.CTkFrame(self, fg_color="#2a1218" if error else CARD, corner_radius=10, border_width=1,
                           border_color="#5a2230" if error else BORDER)
        box.pack(fill="x", padx=14, pady=6)
        ctk.CTkLabel(box, text=text, text_color=RED if error else MUTED, font=("Segoe UI", 12), anchor="w",
                     justify="left", wraplength=760).pack(fill="x", padx=12, pady=8)
        self.scroll_end()

    def add_user(self, text, atts=()):
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(12, 4))
        bubble = ctk.CTkFrame(row, fg_color=USER_BG, corner_radius=16)
        bubble.pack(side="right")
        t = self._text(bubble, USER_BG, "#f4f2ff", 15)
        t.pack(padx=14, pady=(10, 10 if not atts else 4), anchor="w")
        t.insert("1.0", text)
        t.configure(state="disabled")
        for a in atts:
            if a["kind"] == "image":
                try:
                    im = Image.open(a["path"])
                    w, h = im.size
                    k = min(240 / w, 170 / h, 1)
                    img = ctk.CTkImage(light_image=im, size=(max(1, int(w * k)), max(1, int(h * k))))
                    lbl = ctk.CTkLabel(bubble, text="", image=img, corner_radius=8)
                    lbl.image = img
                    lbl.pack(padx=14, pady=(2, 8), anchor="w")
                    continue
                except Exception:
                    pass
            ctk.CTkLabel(bubble, text=f"  📄 {a['name']}  ", fg_color="#4a3fb5", corner_radius=8,
                         text_color="#f4f2ff", font=("Segoe UI", 12), height=24).pack(padx=14, pady=(2, 8), anchor="w")

        def size(e=None):  # a bolha encolhe ao texto, até o limite da largura
            f = self._font(15)
            longest = max((f.measure(line) for line in text.split("\n")), default=10)
            avail = max(220, row.winfo_width() - 100)
            chars = int(min(longest + 10, avail) / max(1, f.measure("0"))) + 1
            if int(t.cget("width")) != chars:
                t.configure(width=chars)
        row.bind("<Configure>", size)
        size()

    # ---------- passos do agente (modo Projeto) ----------
    def _mono_box(self, parent, text, max_lines=14):
        """Caixa só de leitura para diffs e saídas de comando (+ verde, - vermelho)."""
        lines = text.split("\n")
        if len(lines) > 220:
            lines = lines[:220] + ["..."]
        box = ctk.CTkFrame(parent, fg_color=CODE_BG, border_width=1, border_color=BORDER, corner_radius=8)
        t = tk.Text(box, wrap="none", bg=CODE_BG, fg="#abb2bf", relief="flat", bd=0, highlightthickness=0, padx=10,
                    pady=6, width=1, height=min(len(lines), max_lines), font=self._font(12, family="Consolas"),
                    cursor="arrow", takefocus=0, insertwidth=0, selectbackground="#3d3d66")
        t.tag_config("add", foreground="#98c379")
        t.tag_config("del", foreground="#e06c75")
        t.tag_config("hunk", foreground="#56b6c2")
        for ln in lines:
            tag = "add" if ln.startswith("+") and not ln.startswith("+++") else \
                  "del" if ln.startswith("-") and not ln.startswith("---") else "hunk" if ln.startswith("@@") else None
            t.insert("end", ln + "\n", tag)
        t.configure(state="disabled")
        t.pack(side="left", fill="both", expand=True)
        if len(lines) > max_lines:
            vs = ctk.CTkScrollbar(box, command=t.yview, button_color=BORDER, button_hover_color=MUTED)
            vs.pack(side="right", fill="y")
            t.configure(yscrollcommand=vs.set)
            t.bind("<MouseWheel>", lambda e: (t.yview_scroll(int(-e.delta / 120) * 2, "units"), "break")[1])
        return box

    def _stop_dots(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None

    def add_step(self, kind, summary, detail="", ok=True):
        """Uma ferramenta foi usada. Ao vivo, o cartão de 'pensando' dá lugar ao passo e um novo é aberto."""
        live = self._typing is not None
        if live:
            s = self._typing
            self._stop_dots()
            if s["text"] is None:
                s["card"].destroy()
            self._typing = None
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=22, pady=(3, 3))
        head = ctk.CTkFrame(row, fg_color="transparent")
        head.pack(fill="x")
        color = STEP_COLORS.get(kind, MUTED) if ok else RED
        ctk.CTkLabel(head, text="●", text_color=color, font=("Segoe UI", 11), width=14).pack(side="left")
        ctk.CTkLabel(head, text=summary, text_color=TEXT if ok else RED, font=("Consolas", 12), anchor="w"
                     ).pack(side="left", padx=4)
        if detail.strip():
            body = self._mono_box(row, detail)
            state = {"open": False}

            def toggle(b=None):
                state["open"] = not state["open"]
                if state["open"]:
                    body.pack(fill="x", padx=(18, 0), pady=(2, 4))
                else:
                    body.pack_forget()
                btn.configure(text="ocultar" if state["open"] else "ver")
            btn = self._mini(head, "ver", toggle)
            btn.configure(width=50, height=20)
            btn.pack(side="left")
            if kind in ("write", "edit") and live:  # alterações de arquivo já aparecem abertas ao vivo
                toggle()
        if live:
            self.begin_typing()
        else:
            return
        self.scroll_end()

    def _tool_row(self, m):
        try:
            d = json.loads(m["content"])
        except ValueError:
            return
        if d.get("kind") == "changes":
            self.add_changes(m["id"], d)
        else:
            self.add_step(d.get("kind", ""), d.get("summary", ""), d.get("detail", ""), d.get("ok", True))

    def add_approval(self, kind, summary, preview, on_decide):
        box = ctk.CTkFrame(self, fg_color="#1b1710", border_width=1, border_color="#5a4a1a", corner_radius=12)
        box.pack(fill="x", padx=22, pady=8)
        ctk.CTkLabel(box, text=f"A IA quer: {summary}", text_color="#ffd479", font=("Segoe UI", 13, "bold"),
                     anchor="w", justify="left", wraplength=700).pack(fill="x", padx=14, pady=(10, 4))
        if preview.strip():
            self._mono_box(box, preview, 12).pack(fill="x", padx=14, pady=(0, 6))
        bar = ctk.CTkFrame(box, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(2, 10))

        def decide(choice):
            for w in bar.winfo_children():
                w.destroy()
            txt = {"yes": "✔ Permitido", "all": "✔ Permitido (tudo nesta sessão)", "no": "✖ Negado"}[choice]
            ctk.CTkLabel(bar, text=txt, text_color=GREEN if choice != "no" else RED,
                         font=("Segoe UI", 12)).pack(side="left", padx=4)
            on_decide(choice)
        for label, choice, fg in (("Permitir", "yes", ACCENT), ("Negar", "no", BORDER),
                                  ("Permitir tudo nesta sessão", "all", BORDER)):
            ctk.CTkButton(bar, text=label, width=90, height=30, corner_radius=8, fg_color=fg,
                          hover_color="#6a4be0" if fg == ACCENT else "#33333f", font=("Segoe UI", 12),
                          command=lambda c=choice: decide(c)).pack(side="left", padx=4)
        self.scroll_end()

    def add_changes(self, msg_id, data):
        items = data.get("detail", {}).get("items", []) if isinstance(data.get("detail"), dict) else []
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=22, pady=(4, 6))
        undone = data.get("undone")
        ctk.CTkLabel(row, text=f"{len(items)} alteração(ões) feitas no projeto" + ("  ·  desfeitas" if undone else ""),
                     text_color=MUTED, font=("Segoe UI", 12)).pack(side="left")
        if not undone and items:
            ctk.CTkButton(row, text="Desfazer tudo", width=100, height=26, corner_radius=8, fg_color=CARD,
                          hover_color=BORDER, border_width=1, border_color=BORDER, text_color=TEXT,
                          font=("Segoe UI", 12), command=lambda: self.host.undo_changes(msg_id)).pack(side="left", padx=10)

    # ---------- resposta do assistente ----------
    def _chip(self, parent, model):
        fg, bg = chip_colors(model)
        ctk.CTkLabel(parent, text=f" {model} ", text_color=fg, fg_color=bg, corner_radius=8,
                     font=("Segoe UI", 11), height=20).pack(side="left", padx=2)

    def _card(self, model=None):
        card = ctk.CTkFrame(self, fg_color=CARD, border_width=1, border_color=BORDER, corner_radius=14)
        card.pack(fill="x", padx=14, pady=(8, 6))
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(head, text="N", width=24, height=24, corner_radius=12, fg_color=ACCENT, text_color="#ffffff",
                     font=("Segoe UI", 12, "bold")).pack(side="left")
        ctk.CTkLabel(head, text=config.APP_NAME, text_color=TEXT, font=("Segoe UI", 13, "bold")
                     ).pack(side="left", padx=8)
        if model:
            self._chip(head, model)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=(0, 6))
        return card, head, body

    def add_assistant(self, content, model, msg_id, last):
        cut = router.NOTICE in content
        content = content.replace(router.NOTICE, "")
        content, _, src = content.partition("\n\nFontes: ")
        card, head, body = self._card(model)
        for kind, part, lang in split_blocks(content):
            if kind == "text":
                part = part.strip("\n")
                if part.strip():
                    t = self._text(body, CARD)
                    t.pack(fill="x", pady=(0, 4))
                    self._md(t, part)
            else:
                self._code(body, part, lang)
        if cut:
            ctk.CTkLabel(body, text="Resposta cortada pelo limite de tamanho. Clique em Continuar.",
                         text_color=MUTED, font=("Segoe UI", 11)).pack(anchor="w", pady=(2, 4))
        if src.strip():
            chips = ctk.CTkFrame(body, fg_color="transparent")
            chips.pack(fill="x", pady=(4, 4))
            ctk.CTkLabel(chips, text="Fontes", text_color=MUTED, font=("Segoe UI", 11)).pack(side="left", padx=(0, 6))
            for name in [s.strip() for s in src.split(",") if s.strip()]:
                ctk.CTkLabel(chips, text=f" {name} ", text_color=MUTED, fg_color="#1d1d27", corner_radius=8,
                             font=("Segoe UI", 11), height=20).pack(side="left", padx=2)
        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.pack(fill="x", padx=8, pady=(0, 8))
        cb = self._mini(bar, "Copiar")
        cb.configure(command=lambda: self.host.copy_text(content, cb))
        cb.pack(side="left")
        if last:
            self._mini(bar, "Continuar", self.host.continue_reply).pack(side="left")
            self._mini(bar, "Regenerar", lambda: self.host.regenerate(msg_id)).pack(side="left")
        self._mini(bar, "Apagar", lambda: self.host.remove_msg(msg_id)).pack(side="left")

    def _code(self, parent, code, lang):
        code = code.rstrip("\n")
        lines = code.split("\n")
        box = ctk.CTkFrame(parent, fg_color=CODE_BG, border_width=1, border_color=BORDER, corner_radius=10)
        box.pack(fill="x", pady=(2, 8))
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(6, 0))
        ctk.CTkLabel(head, text="●", text_color=LANG_COLORS.get(lang.lower(), MUTED), font=("Segoe UI", 12)
                     ).pack(side="left")
        ctk.CTkLabel(head, text=lang or "código", text_color=MUTED, font=("Segoe UI", 11)
                     ).pack(side="left", padx=(4, 0))
        cb = self._mini(head, "Copiar")
        cb.configure(command=lambda: self.host.copy_text(code, cb))
        buttons = [cb, self._mini(head, "Salvar", lambda: self.host.save_code(code, lang))]
        if lang.lower() == "html":
            buttons.append(self._mini(head, "Abrir", lambda: self.host.open_html(code)))
        for b in buttons:
            b.pack(side="right")
        ctk.CTkFrame(box, height=1, fg_color=BORDER).pack(fill="x", pady=(6, 0))

        wrap = ctk.CTkFrame(box, fg_color=CODE_BG, corner_radius=0)
        wrap.pack(fill="x", padx=2, pady=(0, 6))
        wrap.grid_columnconfigure(0, weight=1)
        tall, wide = len(lines) > 22, max(len(x) for x in lines) > 110
        t = tk.Text(wrap, wrap="none", bg=CODE_BG, fg="#abb2bf", relief="flat", bd=0, highlightthickness=0,
                    padx=12, pady=8, width=1, height=min(len(lines), 22), font=self._font(13, family="Consolas"),
                    cursor="arrow", takefocus=0, insertwidth=0, selectbackground="#3d3d66",
                    selectforeground="#ffffff")
        for tag, color in SYN.items():
            t.tag_config(tag, foreground=color)
        t.insert("1.0", code)
        highlight(t, code, lang)
        t.configure(state="disabled")
        t.grid(row=0, column=0, sticky="nsew")
        if tall:
            vs = ctk.CTkScrollbar(wrap, command=t.yview, button_color=BORDER, button_hover_color=MUTED)
            vs.grid(row=0, column=1, sticky="ns")
            t.configure(yscrollcommand=vs.set)
            t.bind("<MouseWheel>", lambda e: (t.yview_scroll(int(-e.delta / 120) * 2, "units"), "break")[1])
        if wide:
            hs = ctk.CTkScrollbar(wrap, orientation="horizontal", command=t.xview, button_color=BORDER,
                                  button_hover_color=MUTED)
            hs.grid(row=1, column=0, sticky="ew")
            t.configure(xscrollcommand=hs.set)

    # ---------- resposta em andamento ----------
    def begin_typing(self):
        card, head, body = self._card()
        dots = ctk.CTkLabel(body, text="●  ○  ○", text_color=ACCENT, font=("Segoe UI", 14))
        dots.pack(anchor="w", pady=(0, 10))
        self._typing = {"card": card, "head": head, "body": body, "dots": dots, "text": None}
        self._tick_dots(0)
        self.scroll_end()

    def _tick_dots(self, i):
        s = self._typing
        if not s or not s["dots"] or not s["dots"].winfo_exists():
            return
        s["dots"].configure(text=("●  ○  ○", "○  ●  ○", "○  ○  ●", "○  ●  ○")[i % 4])
        self._job = self.after(280, lambda: self._tick_dots(i + 1))

    def stream_append(self, model, text):
        s = self._typing
        if s is None:
            return
        stick = self._at_bottom()
        if s["text"] is None:
            if self._job:
                self.after_cancel(self._job)
                self._job = None
            s["dots"].destroy()
            s["dots"] = None
            self._chip(s["head"], model)
            t = self._text(s["body"], CARD)
            t.pack(fill="x", pady=(0, 10))
            s["text"] = t
        t = s["text"]
        t.configure(state="normal")
        t.insert("end", text)
        t.configure(state="disabled")
        self._fit(t)
        if stick:
            self.scroll_end()
