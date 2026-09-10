# -*- coding: utf-8 -*-
"""
ScreenPen 메모장.

윈도우 메모장 수준의 단순한 글쓰기 창이다. 크기는 자유롭게 조절되고,
'항상 띄워놓기'를 켜면 다른 어떤 창에도 가리지 않으며 바탕화면 보기(Win+D)로도
사라지지 않는다. Ctrl+휠로 글자 크기를 바꾼다.
"""

import ctypes
import json
import os
import tkinter as tk

user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080     # 작업 표시줄과 '바탕화면 보기'에서 빠진다
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOPMOST = 0x00000008

BG = "#FFFFFF"
FG = "#0D0D0D"
BAR_BG = "#F4F4F6"
LINE = "#DCDCE2"
ACCENT = "#2569A7"

FONT_FAMILY = "맑은 고딕"
FONT_MIN, FONT_MAX, FONT_DEFAULT = 8, 96, 11
UI_FONT = ("맑은 고딕", 9)


class MemoWindow:
    """메모 창 하나. 닫아도 내용은 남고, 파일로도 저장된다."""

    MIN_W, MIN_H = 260, 180
    SAVE_DELAY_MS = 700
    WATCH_MS = 700

    def __init__(self, app, base_dir):
        self.app = app
        self.path = os.path.join(base_dir, "memo.txt")
        self.conf_path = os.path.join(base_dir, "memo.json")
        self.win = None
        self.text = None
        self.pinned = True          # '항상 띄워놓기' 기본 켜짐
        self.font_size = FONT_DEFAULT
        self.shown = False
        self._save_job = None
        self._watching = False
        self._load_conf()

    # ------------------------------------------------------------ 열고 닫기

    def toggle(self):
        if self.shown:
            self.hide()
        else:
            self.show()

    def show(self):
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self.win.deiconify()
        self.win.lift()
        self.shown = True
        self._apply_pin()
        self.text.focus_set()
        self.app._render_bar(reblur=False)

    def hide(self):
        self.save()
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()
        self.shown = False
        self.app._render_bar(reblur=False)

    def default_geometry(self):
        """툴바 바로 아래에, 툴바의 '메모' 버튼과 가운데를 맞춰 놓는다."""
        app = self.app
        w = app.BW // 2
        y = app.bar.winfo_rooty() + app.BH + 10
        h = min(app.BW // 2, app.VY + app.VH - y - 60)

        cx = app.bar.winfo_rootx() + app.BW // 2
        for z in app.zones:                     # '메모' 버튼 한가운데
            if z["id"] == "memo":
                cx = app.bar.winfo_rootx() + (z["rect"][0] + z["rect"][2]) // 2
                break
        x = cx - w // 2
        x = max(app.VX + 8, min(x, app.VX + app.VW - w - 8))   # 화면 안으로
        return w, max(self.MIN_H, h), x, y

    def _build(self):
        app = self.app
        self.win = tk.Toplevel(app.root)
        self.win.title("메모  ·  ScreenPen")
        w, h, x, y = self.default_geometry()
        self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.win.minsize(self.MIN_W, self.MIN_H)
        self.win.config(bg=BAR_BG)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)

        bar = tk.Frame(self.win, bg=BAR_BG)
        bar.pack(side="top", fill="x")
        self.pin_var = tk.IntVar(value=1 if self.pinned else 0)
        tk.Checkbutton(bar, text="항상 띄워놓기", variable=self.pin_var,
                       command=self._on_pin_toggle, bg=BAR_BG, fg=FG,
                       activebackground=BAR_BG, activeforeground=ACCENT,
                       selectcolor=BG, font=UI_FONT, bd=0,
                       highlightthickness=0, cursor="hand2",
                       padx=8, pady=6).pack(side="left")
        tk.Frame(self.win, bg=LINE, height=1).pack(side="top", fill="x")

        body = tk.Frame(self.win, bg=BG)
        body.pack(side="top", fill="both", expand=True)
        self.text = tk.Text(body, wrap="word", undo=True,
                            font=(FONT_FAMILY, self.font_size),
                            bd=0, highlightthickness=0, bg=BG, fg=FG,
                            insertbackground=FG, padx=12, pady=10,
                            selectbackground="#CBE0F2", selectforeground=FG)
        sb = tk.Scrollbar(body, command=self.text.yview)
        self.text.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self._load()
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<Control-s>", lambda e: (self.save(), "break")[1])
        # Ctrl+휠 - 글자 크기. "break" 를 돌려주지 않으면 기본 스크롤까지 같이 돈다
        self.text.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        self.text.bind("<Control-Prior>", lambda e: self.bump_font(1) or "break")
        self.text.bind("<Control-Next>", lambda e: self.bump_font(-1) or "break")
        self._start_watch()

    # ------------------------------------------------------------ 글자 크기

    def _on_ctrl_wheel(self, e):
        self.bump_font(1 if e.delta > 0 else -1)
        return "break"

    def bump_font(self, step):
        size = max(FONT_MIN, min(FONT_MAX, self.font_size + step))
        if size == self.font_size:
            return
        self.font_size = size
        if self.text is not None and self.text.winfo_exists():
            self.text.config(font=(FONT_FAMILY, size))
        self._save_conf()

    # ------------------------------------------------------------ 항상 띄워놓기

    def _on_pin_toggle(self):
        self.pinned = bool(self.pin_var.get())
        self._apply_pin()
        self._save_conf()

    def _hwnd(self):
        wid = self.win.winfo_id()
        return user32.GetParent(wid) or wid

    def _apply_pin(self):
        """항상 띄워놓기.

        -topmost 만으로는 다른 창 위에 뜨지만 '바탕화면 보기'(Win+D)를 누르면
        같이 내려간다. 도구 창(WS_EX_TOOLWINDOW)으로 만들면 작업 표시줄과
        바탕화면 보기의 대상에서 빠져 그대로 남는다.
        """
        if self.win is None or not self.win.winfo_exists():
            return
        try:
            hwnd = self._hwnd()
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            want = ((ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW if self.pinned
                    else (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
            if want != ex:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, want)
                # 확장 스타일 변경은 창을 다시 보여 줘야 반영된다
                if self.shown:
                    self.win.withdraw()
                    self.win.deiconify()
            if bool(ex & WS_EX_TOPMOST) != self.pinned:
                self.win.attributes("-topmost", self.pinned)
        except Exception:
            pass

    def _start_watch(self):
        if self._watching:
            return
        self._watching = True
        self._watch()

    def _watch(self):
        """내려가 있을 때만 되돌린다.

        예전에는 주기적으로 lift() 를 불렀는데, 툴바도 같은 일을 하고 있어서
        둘이 서로를 밀어내며 6초에 10번씩 Z순서가 뒤집혔다. 그때마다 창이
        다시 그려져 상단이 깜빡였다. 그래서 '이미 맨 위면 아무것도 하지 않는다'.
        """
        if not getattr(self.app, "_alive", False):
            return
        try:
            if (self.shown and self.pinned and self.win is not None
                    and self.win.winfo_exists()
                    and not self.app.drawing_mode):
                hwnd = self._hwnd()
                if user32.IsIconic(hwnd):
                    self.win.deiconify()
                elif not (user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST):
                    self.win.attributes("-topmost", True)
        except Exception:
            pass
        self.app.root.after(self.WATCH_MS, self._watch)

    # ------------------------------------------------------------ 저장

    def _load_conf(self):
        try:
            with open(self.conf_path, encoding="utf-8") as f:
                conf = json.load(f)
            self.font_size = max(FONT_MIN, min(FONT_MAX,
                                               int(conf.get("font_size",
                                                            FONT_DEFAULT))))
            self.pinned = bool(conf.get("pinned", True))
        except Exception:
            pass

    def _save_conf(self):
        try:
            with open(self.conf_path, "w", encoding="utf-8") as f:
                json.dump({"font_size": self.font_size, "pinned": self.pinned},
                          f, ensure_ascii=False)
        except Exception:
            pass

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    self.text.insert("1.0", f.read())
        except Exception:
            pass
        self.text.edit_modified(False)

    def _on_modified(self, _e=None):
        if self.text is None or not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._save_job:
            try:
                self.win.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.win.after(self.SAVE_DELAY_MS, self.save)

    def save(self):
        """메모장과 달리 내용을 잃지 않도록 파일에 흘려 둔다."""
        self._save_job = None
        if self.text is None or not self.text.winfo_exists():
            return
        try:
            body = self.text.get("1.0", "end-1c")
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception:
            pass

    def destroy(self):
        self.save()
        self._save_conf()
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
        self.win = None
