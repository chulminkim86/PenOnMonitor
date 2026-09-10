# -*- coding: utf-8 -*-
"""
ScreenPen - 화면 위에 바로 쓰는 필기 도구

세 가지 상태로 동작한다.
  통과 모드     : 필기 내용만 화면 위에 떠 있고 마우스는 아래 프로그램으로 통과된다.
  그리기 모드   : 화면을 순간 캡처해 배경으로 깔고 그 위에 자유롭게 필기한다.
  화이트보드    : 화면 전체를 흰 판으로 덮고 그 위에 필기한다. 확대/축소가 된다.

전역 단축키 (초점이 어디에 있든 동작)
  F8          그리기 모드 켜기/끄기
  Ctrl+0      화이트보드 켜기/끄기
  Esc         전부 해제 - 화이트보드에서 나오고, 그리기를 끄고, 화면 필기를 지운다
  F9          전체 지우기
  Ctrl+Alt+S  PNG로 저장
  Ctrl+Alt+Q  종료

오버레이가 입력을 받는 동안만 동작하는 키
  Ctrl+1~4    펜 / 지우개 / 사각 / 형광
  Alt+1~3     검정 / 빨강 / 파랑
  Ctrl+휠     굵기 1~50pt
  휠          화이트보드 확대/축소
  H           화이트보드 손 도구 (끌어서 이동)
  Ctrl+Z      되돌리기      Ctrl+Y  다시 실행
"""

import ctypes
import datetime
import math
import os
import queue
import sys
import threading
import time
import traceback
from ctypes import wintypes

import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageGrab, ImageTk
from memo import MemoWindow

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "screenpen.log")

# ---------------------------------------------------------------- Win32 설정

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
gdi32 = ctypes.windll.gdi32
SRCCOPY = 0x00CC0020

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_TOPMOST = 0x8
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP_ESC = 0x8001            # 전용 스레드에 Esc 등록/해제를 요청할 때 쓴다
ESC_ID = 6
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
ERROR_ALREADY_EXISTS = 183
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

VK_F8, VK_F9, VK_S, VK_Q, VK_0, VK_ESC = 0x77, 0x78, 0x53, 0x51, 0x30, 0x1B

MUTEX_NAME = "Local\\ScreenPen_SingleInstance_v1"

user32.GetMessageW.restype = ctypes.c_int


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def grab_rect(x, y, w, h):
    """화면의 일부만 가져온다.

    PIL 의 ImageGrab 은 영역을 줘도 화면 전체를 먼저 뜬 뒤 잘라내서,
    7680x2160 환경에서는 한 번에 150ms 가까이 걸린다. 유리 배경은 툴바
    크기(작은 띠)만 있으면 되므로 GDI 로 그 영역만 직접 복사한다.
    """
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, SRCCOPY)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # 음수 = 위에서 아래로
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0      # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, hdc)


def log(msg):
    """pythonw 로 띄우면 오류가 어디에도 안 보이므로 파일에 남긴다."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _set_dpi_aware():
    """모니터별 DPI를 인지해야 캡처 좌표와 캔버스 좌표가 일치한다."""
    for fn in (
        lambda: user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        lambda: user32.SetProcessDPIAware(),
    ):
        try:
            fn()
            return
        except Exception:
            continue


_set_dpi_aware()

# 가상 화면(모든 모니터를 합친 영역)
VX = user32.GetSystemMetrics(76)
VY = user32.GetSystemMetrics(77)
VW = user32.GetSystemMetrics(78)
VH = user32.GetSystemMetrics(79)

# 투명 처리에 쓸 색. 실제 화면에 거의 등장하지 않는 값을 고른다.
MAGIC = "#FF00FE"

# ---------------------------------------------------------------- 팔레트

INK_BLACK = "#0D0D0D"
INK_BLUE = "#2569A7"
INK_RED = "#A20000"
MARKER_YELLOW = "#FFE24D"      # 형광펜은 색상 선택과 무관하게 항상 이 색

# 메뉴 배치 순서 = Alt+1 / Alt+2 / Alt+3
COLORS = [INK_BLACK, INK_RED, INK_BLUE]
COLOR_NAMES = ["검정", "빨강", "파랑"]

# --- 글래스모피즘 ---------------------------------------------------------
# 흐린 배경 위에 반투명 판을 얹고, 가장자리를 밝은 선으로 딴다.
GLASS_BLUR = 16                # backdrop-filter: blur(16px)
GLASS_TINT = (255, 255, 255)   # 밝은 유리
GLASS_TINT_A = 0.58            # 어떤 바탕에서도 검은 글자가 읽히도록 진하게 잡았다
GLASS_RADIUS = 16
GLASS_RIM_TOP = (255, 255, 255, 210)    # 위쪽 테두리가 더 밝다
GLASS_RIM = (255, 255, 255, 110)
GLASS_EDGE = (13, 13, 13, 38)           # 밝은 바탕에서 판이 묻히지 않게
GLASS_SEP = (13, 13, 13, 32)
PILL_SEL = (37, 105, 167, 44)           # 선택된 항목
PILL_HOVER = (13, 13, 13, 18)
SWATCH_EDGE = (13, 13, 13, 60)

FG = "#0D0D0D"
FG_DIM = "#6E6E76"
SWITCH_OFF = "#B9B9C2"
SWITCH_KNOB = "#FFFFFF"

# ---------------------------------------------------------------- 도구 정의

TOOLS = [
    ("pen", "펜", "Ctrl+1"),
    ("erase", "지우개", "Ctrl+2"),
    ("rect", "사각", "Ctrl+3"),
    ("marker", "형광", "Ctrl+4"),
]

ACTIONS = [
    ("board", "화이트보드", "화이트보드  ·  Ctrl+0\n흰 판으로 화면을 덮는다"),
    ("clear", "전체지우기", "전체 지우기  ·  F9"),
    ("memo", "메모", "메모장 열기/닫기" + chr(10) +
     "크기 조절 자유 · 항상 띄워놓기 지원"),
    ("save", "저장", "PNG로 저장  ·  Ctrl+Alt+S\n~/Pictures/ScreenPen"),
]

WIDTH_MIN, WIDTH_MAX = 1, 50
DEFAULT_WIDTHS = {"pen": 5, "erase": 20, "rect": 5, "marker": 24}

ZOOM_MIN, ZOOM_MAX = 0.25, 8.0
ZOOM_NOTCH = 1.18              # 휠 한 칸이 목표 배율을 이만큼 바꾼다
ZOOM_EASE = 0.34               # 목표까지 한 프레임에 좁히는 비율(지수 보간)
ZOOM_FRAME_MS = 12

# 스위치: 손잡이가 트랙 위를 미끄러진다. 끝에서 살짝 늦춰지도록 감속을 준다.
SWITCH_MS = 190
SWITCH_FRAME_MS = 12

GLASS_REFRESH_MS = 400         # 통과 모드에서 뒷배경이 바뀌는 것을 따라간다

UI_FONT = ("맑은 고딕", 10)
UI_FONT_B = ("맑은 고딕", 10, "bold")
TIP_FONT = ("맑은 고딕", 9)


def rounded_rect(draw, box, radius, fill=None, outline=None, width=1):
    """PIL 로 둥근 사각형. 안티에일리어싱이 되어 가장자리가 깨끗하다."""
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline,
                           width=width)


class Surface:
    """필기 한 벌. 화면용과 화이트보드용을 따로 둔다."""

    def __init__(self, zoomable=False):
        self.strokes = []
        self.hist = [[]]
        self.hidx = 0
        self.zoomable = zoomable
        self.scale = 1.0
        self.ox = 0.0
        self.oy = 0.0


class HotkeyThread(threading.Thread):
    """전역 단축키를 소유하는 전용 스레드.

    RegisterHotKey 로 등록한 WM_HOTKEY 는 등록한 '스레드'의 메시지 큐로 들어온다.
    이걸 Tk 메인 스레드에서 받으려 하면 Tk 의 메인루프가 먼저 큐를 비워 버려서
    단축키가 대부분 유실된다. 그래서 자기 메시지 루프를 가진 스레드를 따로 두고,
    받은 단축키 번호만 큐에 넣어 메인 스레드가 꺼내 쓰게 한다.
    """

    def __init__(self, specs):
        super().__init__(daemon=True)
        self.specs = specs                 # {id: (mods, vk, name)}
        self.events = queue.Queue()
        self.failed = []
        self.ready = threading.Event()
        self.tid = 0

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # 스레드 메시지 큐를 먼저 만들어 두어야 PostThreadMessage 가 유실되지 않는다
        user32.PeekMessageW(ctypes.byref(msg), None, 0x0400, 0x0400, 0)

        for hid, (mods, vk, name) in self.specs.items():
            done = False
            for _try in range(3):          # 직전 인스턴스가 정리되는 중일 수 있다
                if user32.RegisterHotKey(None, hid, mods, vk):
                    done = True
                    break
                time.sleep(0.15)
            if not done:
                self.failed.append(name)
                log("RegisterHotKey 실패: %s (err=%d)"
                    % (name, kernel32.GetLastError()))
        self.ready.set()

        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got <= 0:                   # WM_QUIT 또는 오류
                break
            if msg.message == WM_HOTKEY:
                self.events.put(int(msg.wParam))
            elif msg.message == WM_APP_ESC:
                try:
                    if msg.wParam:
                        user32.RegisterHotKey(None, ESC_ID, MOD_NOREPEAT, VK_ESC)
                    else:
                        user32.UnregisterHotKey(None, ESC_ID)
                except Exception:
                    log("esc hotkey " + traceback.format_exc())

        for hid in list(self.specs) + [ESC_ID]:
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass

    def set_esc(self, on):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_APP_ESC, 1 if on else 0, 0)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


class ScreenPen:
    def __init__(self):
        self.tool = "pen"
        self.color = INK_BLACK
        self.widths = dict(DEFAULT_WIDTHS)     # 도구마다 굵기를 따로 기억한다
        self.drawing_mode = False
        self.board_mode = False
        self.hand_mode = False

        self.screen_surf = Surface()
        self.board_surf = Surface(zoomable=True)
        self.surf = self.screen_surf

        self.cur = None            # 그리는 중인 필기
        self.bgimg = None          # 그리기 모드 배경(화면 캡처, Tk 이미지)
        self.bg_pil = None         # 같은 배경의 PIL 원본 (유리 뒷배경에 쓴다)
        self._pan = None
        self._zoom_target = 1.0
        self._zoom_anchor = (VW / 2.0, VH / 2.0)
        self._zoom_job = None
        self._hint_job = None

        self._tip = None
        self._tip_job = None
        self._tip_zone = None
        self._hover = None
        self._bar_drag = None
        self._glass_job = None
        self._ready = False        # 초기화 중에는 모드 자동 전환을 막는다
        self._alive = True
        self._esc_on = False
        self.capture_excluded = False

        self._build_overlay()
        self._build_bar()

        # 창을 화면 캡처 대상에서 빼면, 캡처할 때 창을 숨길 필요가 없다.
        # 유리 뒷배경을 찍을 때 툴바 자신이 찍히지 않는 것도 이 덕분이다.
        self.capture_excluded = self._probe_capture_exclusion()
        log("capture exclusion: %s" % self.capture_excluded)
        if self.capture_excluded:
            self._exclude(self._hwnd())
            self._exclude(self._hwnd(self.bar))

        # 메모 창이 자기 자리를 잡을 때 화면 크기를 참고한다
        self.VX, self.VY, self.VW, self.VH = VX, VY, VW, VH
        self.memo = MemoWindow(self, BASE_DIR)

        self._register_hotkeys()
        self._set_click_through(True)
        self._ready = True
        self._render_bar()
        self.root.after(30, self._pump_hotkeys)
        self.root.after(1200, self._keep_bar_visible)
        self.root.after(GLASS_REFRESH_MS, self._glass_tick)
        if self.hotkey_failed:
            self.root.after(600, lambda: self._toast(
                "전역 단축키 등록 실패: %s  (다른 프로그램이 선점)"
                % ", ".join(self.hotkey_failed)))

    # ------------------------------------------------------------ 굵기

    @property
    def width(self):
        return self.widths.get(self.tool, 5)

    def set_width(self, w):
        self.widths[self.tool] = max(WIDTH_MIN, min(WIDTH_MAX, int(round(w))))

    def bump_width(self, step):
        """Ctrl+휠 - 1pt 부터 50pt 까지 연속으로 굵기를 고른다."""
        if not self.drawing_mode:
            return
        self.set_width(self.width + step)
        self._show_width_hint()

    # ------------------------------------------------------------ 오류 처리

    def _log_state(self, tag):
        log("%s draw=%s board=%s screen=%d board_strokes=%d"
            % (tag, self.drawing_mode, self.board_mode,
               len(self.screen_surf.strokes), len(self.board_surf.strokes)))

    def _log_exc(self, where):
        log("%s\n%s" % (where, traceback.format_exc()))

    def _tk_exc(self, exc, val, tb):
        log("tk callback\n%s" % "".join(traceback.format_exception(exc, val, tb)))

    # ------------------------------------------------------------ 창 구성

    def _build_overlay(self):
        self.root = tk.Tk()
        self.root.title("ScreenPen")
        self.root.report_callback_exception = self._tk_exc
        self.root.overrideredirect(True)
        self.root.geometry("%dx%d+%d+%d" % (VW, VH, VX, VY))
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", MAGIC)
        self.root.config(bg=MAGIC)

        self.canvas = tk.Canvas(self.root, width=VW, height=VH,
                                bg=MAGIC, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", lambda e: self.reset_all())

        # 가운데 버튼 드래그로도 화이트보드를 옮길 수 있다
        self.canvas.bind("<ButtonPress-2>", self._pan_press)
        self.canvas.bind("<B2-Motion>", self._pan_move)
        self.canvas.bind("<ButtonRelease-2>", lambda e: setattr(self, "_pan", None))

        r = self.root
        r.bind("<Control-z>", lambda e: self.undo())      # 메뉴에는 두지 않는다
        r.bind("<Control-y>", lambda e: self.redo())
        r.bind("<Control-Key-0>", lambda e: self.toggle_board())
        r.bind("<Escape>", lambda e: self.reset_all())
        for i, (tid, _, _) in enumerate(TOOLS, start=1):
            r.bind("<Control-Key-%d>" % i, lambda e, t=tid: self.set_tool(t))
        for i, c in enumerate(COLORS, start=1):
            r.bind("<Alt-Key-%d>" % i, lambda e, cc=c: self.set_color(cc))

        # 휠: 그냥 돌리면 화이트보드 확대/축소, Ctrl 을 누르고 돌리면 굵기
        for w in (r, self.canvas):
            w.bind("<MouseWheel>", self._on_wheel)
            w.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        r.bind("<Prior>", lambda e: self.zoom(ZOOM_NOTCH))
        r.bind("<Next>", lambda e: self.zoom(1 / ZOOM_NOTCH))
        r.bind("<Control-Prior>", lambda e: self.bump_width(1))
        r.bind("<Control-Next>", lambda e: self.bump_width(-1))

        # H: 손 도구 (화이트보드에서 끌어서 이동)
        for seq in ("<Key-h>", "<Key-H>"):
            r.bind(seq, lambda e: self.toggle_hand())

    def _hwnd(self, win=None):
        win = win or self.root
        wid = win.winfo_id()
        return user32.GetParent(wid) or wid

    def _exclude(self, hwnd, on=True):
        """창을 화면 캡처 결과에서 제외한다(화면에는 그대로 보인다)."""
        try:
            return bool(user32.SetWindowDisplayAffinity(
                hwnd, WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
        except Exception:
            return False

    def _round_corners(self, hwnd):
        """Windows 11 이 창 모서리를 둥글게(안티에일리어싱 포함) 깎아 준다."""
        try:
            v = ctypes.c_int(DWMWCP_ROUND)
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                         ctypes.byref(v), ctypes.sizeof(v))
            return True
        except Exception:
            return False

    def _probe_capture_exclusion(self):
        """작은 시험용 창으로 캡처 제외가 실제로 먹는지 확인한다."""
        try:
            p = tk.Toplevel(self.root)
            p.overrideredirect(True)
            p.attributes("-topmost", True)
            p.geometry("8x8+%d+%d" % (VX + 2, VY + 2))
            p.config(bg=MAGIC)
            p.update()
            self._exclude(self._hwnd(p))
            p.update()
            time.sleep(0.06)
            img = ImageGrab.grab(bbox=(VX + 2, VY + 2, VX + 10, VY + 10),
                                 all_screens=True)
            px = img.getpixel((4, 4))
            p.destroy()
            return px[:3] != (255, 0, 254)
        except Exception:
            self._log_exc("probe")
            return False

    def _set_click_through(self, on):
        """켜면 창 전체가 마우스를 무시한다(아래 프로그램으로 클릭 통과)."""
        try:
            hwnd = self._hwnd()
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_TRANSPARENT) if on else (style & ~WS_EX_TRANSPARENT)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            self._log_exc("click_through")

    # ------------------------------------------------------------ 툴바 배치

    def _build_bar(self):
        """툴바는 캔버스 하나로 그린다.

        유리 느낌을 내려면 반투명 판이 필요한데 Tk 위젯에는 알파가 없다.
        그래서 판·알약·구분선·색 스와치는 PIL 로 그린 그림 한 장으로 만들고,
        글자와 스위치만 캔버스 항목으로 그 위에 얹는다.
        """
        self.bar = tk.Toplevel(self.root)
        self.bar.overrideredirect(True)
        self.bar.attributes("-topmost", True)

        self.f_reg = tkfont.Font(family="맑은 고딕", size=10)
        self.f_bold = tkfont.Font(family="맑은 고딕", size=10, weight="bold")

        self.logo_pil = self._load_logo()
        self._layout()

        self.bar.geometry("%dx%d+%d+%d"
                          % (self.BW, self.BH,
                             VX + (VW - self.BW) // 2, VY + 14))
        self.bar_canvas = tk.Canvas(self.bar, width=self.BW, height=self.BH,
                                    highlightthickness=0, bd=0, bg="#FFFFFF")
        self.bar_canvas.pack()
        self.bar.update_idletasks()
        self._round_corners(self._hwnd(self.bar))

        self._glass_cache = None
        self._bar_photo = None
        self._bar_img = self.bar_canvas.create_image(0, 0, anchor="nw")

        # 글자는 캔버스 항목으로 얹어야 또렷하다(그림에 그리면 흐려진다)
        self.text_items = {}
        for z in self.zones:
            if z.get("label"):
                cx = (z["rect"][0] + z["rect"][2]) / 2.0
                cy = (z["rect"][1] + z["rect"][3]) / 2.0
                self.text_items[z["id"]] = self.bar_canvas.create_text(
                    cx, cy, text=z["label"], font=self.f_reg, fill=FG)

        self._build_switch()

        c = self.bar_canvas
        c.bind("<Motion>", self._bar_motion)
        c.bind("<Leave>", self._bar_leave)
        c.bind("<ButtonPress-1>", self._bar_press)
        c.bind("<B1-Motion>", self._bar_move)
        c.bind("<ButtonRelease-1>", self._bar_release)

    def _layout(self):
        """왼쪽부터 차례로 놓으며 각 항목의 자리를 계산한다."""
        PAD_X, GAP, SEP_GAP = 14, 4, 11
        H = 52
        self.BH = H
        y0, y1 = 9, H - 9          # 알약 높이
        x = PAD_X
        self.zones = []

        def add(kind, zid, w, label=None, tip=None, pad=GAP):
            self.zones.append({"kind": kind, "id": zid, "label": label,
                               "tip": tip, "rect": (x, y0, x + w, y1)})
            return x + w + pad

        # 로고 (드래그 손잡이 겸용).
        # 판 왼쪽 끝까지의 여백과 구분선까지의 여백을 같게 맞춰야 가운데로 보인다.
        lw = self.logo_pil.width if self.logo_pil is not None else 22
        lh = self.logo_pil.height if self.logo_pil is not None else 22
        LOGO_GAP = 19
        self.logo_pos = (LOGO_GAP, (H - lh) // 2)
        sep_x = LOGO_GAP + lw + LOGO_GAP
        self.seps = [sep_x]
        x = LOGO_GAP
        tip_logo = ("끌어서 툴바 이동" + chr(10) +
                    "되돌리기 Ctrl+Z · 다시 실행 Ctrl+Y" + chr(10) +
                    "전부 해제 Esc")
        add("logo", "logo", lw, tip=tip_logo, pad=0)
        x = sep_x + SEP_GAP

        for tid, label, key in TOOLS:
            tip = "%s  ·  %s\n굵기는 Ctrl+휠" % (label, key)
            if tid == "marker":
                tip += "\n형광은 항상 노란색"
            x = add("tool", tid, self.f_bold.measure(label) + 22, label, tip)

        x += SEP_GAP - GAP
        self.seps.append(x)
        x += SEP_GAP

        for i, (c, cname) in enumerate(zip(COLORS, COLOR_NAMES), start=1):
            x = add("color", c, 30, None, "%s  ·  Alt+%d\n%s" % (cname, i, c))

        x += SEP_GAP - GAP
        self.seps.append(x)
        x += SEP_GAP

        for aid, label, tip in ACTIONS:
            x = add("action", aid, self.f_bold.measure(label) + 22, label, tip)

        x += SEP_GAP - GAP
        self.seps.append(x)
        x += SEP_GAP

        x = add("switch", "switch", self.f_bold.measure("그리기") + 12, "그리기",
                "그리기 모드  ·  F8\n끄면 클릭이 아래 프로그램으로 통과된다", pad=8)
        self.switch_rect = (x, (H - 24) // 2, x + 44, (H - 24) // 2 + 24)
        self.zones.append({"kind": "switch", "id": "switch", "label": None,
                           "tip": "그리기 모드  ·  F8",
                           "rect": (x, y0, x + 44, y1)})
        x += 44 + SEP_GAP

        x = add("close", "close", 30, "✕", "종료  ·  Ctrl+Alt+Q", pad=0)
        self.BW = x + PAD_X

    def _build_switch(self):
        """트랙 위를 손잡이가 미끄러진다."""
        x0, y0, x1, y1 = self.switch_rect
        h = y1 - y0
        c = self.bar_canvas
        self.sw_track = [
            c.create_oval(x0, y0, x0 + h, y1, width=0, fill=SWITCH_OFF),
            c.create_oval(x1 - h, y0, x1, y1, width=0, fill=SWITCH_OFF),
            c.create_rectangle(x0 + h / 2, y0, x1 - h / 2, y1, width=0,
                               fill=SWITCH_OFF),
        ]
        pad = 3
        self.sw_d = h - pad * 2
        self.sw_x_off = x0 + pad
        self.sw_x_on = x1 - pad - self.sw_d
        self.sw_knob = c.create_oval(self.sw_x_off, y0 + pad,
                                     self.sw_x_off + self.sw_d, y1 - pad,
                                     fill=SWITCH_KNOB, outline="#A9A9B4")
        self.sw_on = False
        self.sw_job = None
        self.sw_t0 = 0.0
        self.sw_from = self.sw_x_off

    def _switch_set(self, on):
        if on == self.sw_on:
            return
        self.sw_on = on
        for item in self.sw_track:
            self.bar_canvas.itemconfig(item, fill=INK_BLUE if on else SWITCH_OFF)
        self.sw_from = self.bar_canvas.coords(self.sw_knob)[0]
        self.sw_t0 = time.perf_counter()
        if self.sw_job is None:
            self._switch_step()

    def _switch_step(self):
        """감속(ease-out)으로 미끄러지게 한다. 등속이면 뚝 끊겨 보인다."""
        target = self.sw_x_on if self.sw_on else self.sw_x_off
        t = (time.perf_counter() - self.sw_t0) * 1000.0 / SWITCH_MS
        if t >= 1.0:
            self.bar_canvas.move(
                self.sw_knob, target - self.bar_canvas.coords(self.sw_knob)[0], 0)
            self.sw_job = None
            return
        e = 1 - (1 - t) ** 3                       # cubic ease-out
        x = self.sw_from + (target - self.sw_from) * e
        self.bar_canvas.move(
            self.sw_knob, x - self.bar_canvas.coords(self.sw_knob)[0], 0)
        self.sw_job = self.bar.after(SWITCH_FRAME_MS, self._switch_step)

    # ------------------------------------------------------------ 툴바 그리기

    def _load_logo(self, height=22):
        """폴더에 놓인 logo.png 등을 불러온다. 여백을 잘라 중앙을 맞춘다."""
        for name in ("logo.png", "logo.gif", "logo.jpg", "logo.jpeg", "logo.webp"):
            path = os.path.join(BASE_DIR, name)
            if not os.path.exists(path):
                continue
            try:
                im = Image.open(path).convert("RGBA")
                plate = Image.new("RGBA", im.size, (255, 255, 255, 255))
                comp = Image.alpha_composite(plate, im).convert("RGB")
                diff = ImageChops.difference(comp, Image.new("RGB", im.size,
                                                             (255, 255, 255)))
                box = diff.convert("L").point(lambda v: 255 if v > 12 else 0).getbbox()
                if box:
                    im = im.crop(box)
                ratio = height / float(im.height)
                return im.resize((max(1, int(round(im.width * ratio))), height),
                                 Image.LANCZOS)
            except Exception:
                self._log_exc("logo")
                return None
        return None

    def _backdrop(self):
        """유리 뒤에 비칠 그림. 지금 상태에 따라 출처가 다르다."""
        W, H = self.BW, self.BH
        try:
            if self.board_mode:
                return Image.new("RGB", (W, H), "#FFFFFF")
            x = self.bar.winfo_rootx()
            y = self.bar.winfo_rooty()
            if self.bg_pil is not None:            # 그리기 모드: 고정된 배경
                bx, by = x - VX, y - VY
                return self.bg_pil.crop((bx, by, bx + W, by + H)).convert("RGB")
            # 통과 모드: 실제 바탕. 우리 창들은 캡처에서 빠지므로 뒤가 그대로 찍힌다
            return grab_rect(x, y, W, H)
        except Exception:
            return Image.new("RGB", (W, H), "#F0F0F2")

    def _glass(self):
        """흐린 배경 + 반투명 흰 틴트 = 유리."""
        W, H = self.BW, self.BH
        src = self._backdrop()
        if src.size != (W, H):
            src = src.resize((W, H))
        # 1/4 로 줄여 흐린 뒤 되돌리면 같은 결과를 훨씬 싸게 얻는다
        sw, sh = max(1, W // 4), max(1, H // 4)
        small = src.resize((sw, sh), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(GLASS_BLUR / 4.0))
        blurred = small.resize((W, H), Image.BILINEAR)
        return Image.blend(blurred, Image.new("RGB", (W, H), GLASS_TINT),
                           GLASS_TINT_A).convert("RGBA")

    def _render_bar(self, reblur=True):
        """툴바 그림 한 장을 만든다. reblur=False 면 흐린 배경을 재사용한다."""
        if not self._ready:
            return
        try:
            if reblur or self._glass_cache is None:
                self._glass_cache = self._glass()
            img = self._glass_cache.copy()
            ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)

            # 선택/호버 알약
            for z in self.zones:
                fill = None
                if self._is_selected(z):
                    fill = PILL_SEL
                elif self._hover == z["id"] and z["kind"] != "logo":
                    fill = PILL_HOVER
                if fill:
                    x0, y0, x1, y1 = z["rect"]
                    rounded_rect(d, (x0, y0, x1 - 1, y1 - 1), 9, fill=fill)

            # 구분선
            for sx in self.seps:
                d.line([(sx, 15), (sx, self.BH - 15)], fill=GLASS_SEP, width=1)

            # 색 스와치. 고른 색은 알약 위에 흰 테를 둘러 한눈에 띄게 한다
            for z in self.zones:
                if z["kind"] != "color":
                    continue
                x0, y0, x1, y1 = z["rect"]
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                sel = self.color == z["id"]
                r = 9.5 if sel else 8
                if sel:
                    d.ellipse((cx - r - 2.5, cy - r - 2.5, cx + r + 2.5, cy + r + 2.5),
                              fill=(255, 255, 255, 235))
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=z["id"],
                          outline=SWATCH_EDGE, width=1)

            img = Image.alpha_composite(img, ov)

            if self.logo_pil is not None:
                img.alpha_composite(self.logo_pil, self.logo_pos)

            # 유리 테두리: 위쪽이 더 밝고, 바깥쪽에 옅은 선을 둘러 판을 세운다
            d2 = ImageDraw.Draw(img)
            rounded_rect(d2, (0, 0, self.BW - 1, self.BH - 1), GLASS_RADIUS,
                         outline=GLASS_EDGE, width=1)
            rounded_rect(d2, (1, 1, self.BW - 2, self.BH - 2), GLASS_RADIUS - 1,
                         outline=GLASS_RIM, width=1)
            d2.line([(GLASS_RADIUS, 1), (self.BW - GLASS_RADIUS, 1)],
                    fill=GLASS_RIM_TOP, width=1)

            self._bar_photo = ImageTk.PhotoImage(img.convert("RGB"))
            self.bar_canvas.itemconfig(self._bar_img, image=self._bar_photo)
            self.bar_canvas.tag_lower(self._bar_img)
            self._restyle_text()
        except Exception:
            self._log_exc("render_bar")

    def _is_selected(self, z):
        if z["kind"] == "tool":
            return z["id"] == self.tool and not self.hand_mode
        if z["kind"] == "action":
            if z["id"] == "board":
                return self.board_mode
            if z["id"] == "memo":
                return self.memo.shown
            return False
        if z["kind"] == "color":
            return z["id"] == self.color
        return False

    def _restyle_text(self):
        for z in self.zones:
            item = self.text_items.get(z["id"])
            if item is None:
                continue
            sel = self._is_selected(z)
            if z["id"] == "close":
                fill = INK_RED if self._hover == "close" else FG_DIM
            elif z["kind"] == "switch":
                fill = INK_BLUE if self.drawing_mode else FG_DIM
            else:
                fill = INK_BLUE if sel else FG
            self.bar_canvas.itemconfig(item, fill=fill,
                                       font=self.f_bold if sel else self.f_reg)

    def _glass_tick(self):
        """통과 모드에서는 뒷배경이 계속 바뀌므로 주기적으로 다시 흐린다."""
        if not self._alive:
            return
        try:
            if (self._ready and not self.board_mode and self.bg_pil is None
                    and self._bar_drag is None):
                self._render_bar(reblur=True)
        except Exception:
            self._log_exc("glass_tick")
        self.root.after(GLASS_REFRESH_MS, self._glass_tick)

    # ------------------------------------------------------------ 툴바 입력

    def _zone_at(self, x, y):
        for z in self.zones:
            x0, y0, x1, y1 = z["rect"]
            if x0 <= x <= x1 and y0 <= y <= y1:
                return z
        return None

    def _bar_motion(self, e):
        z = self._zone_at(e.x, e.y)
        zid = z["id"] if z else None
        if zid != self._hover:
            self._hover = zid
            self._render_bar(reblur=False)
            self._tip_hide()
            if z and z.get("tip"):
                self._tip_zone = z
                self._tip_job = self.bar.after(
                    450, lambda t=z["tip"]: self._tip_show(t))

    def _bar_leave(self, e):
        if self._hover is not None:
            self._hover = None
            self._render_bar(reblur=False)
        self._tip_hide()

    def _bar_press(self, e):
        self._tip_hide()
        z = self._zone_at(e.x, e.y)
        if z is None:
            return
        if z["kind"] == "logo":
            self._bar_drag = (e.x_root - self.bar.winfo_x(),
                              e.y_root - self.bar.winfo_y())
            return
        if z["kind"] == "tool":
            self.set_tool(z["id"])
        elif z["kind"] == "color":
            self.set_color(z["id"])
        elif z["kind"] == "switch":
            self.set_draw_mode(not self.drawing_mode)
        elif z["kind"] == "close":
            self.quit()
        elif z["kind"] == "action":
            {"board": self.toggle_board, "clear": self.clear_all,
             "memo": self.memo.toggle, "save": self.save_png}[z["id"]]()

    def _bar_move(self, e):
        if self._bar_drag is None:
            return
        self.bar.geometry("+%d+%d" % (e.x_root - self._bar_drag[0],
                                      e.y_root - self._bar_drag[1]))

    def _bar_release(self, e):
        if self._bar_drag is not None:
            self._bar_drag = None
            self._render_bar(reblur=True)      # 옮긴 자리의 배경으로 다시 흐린다

    def _keep_bar_visible(self):
        """툴바는 어떤 상태에서도 항상 보이고 맨 앞에 있어야 한다.

        예전에는 주기적으로 lift() 를 불렀는데, 메모 창도 같은 일을 하고 있어서
        둘이 서로를 밀어내며 Z순서가 계속 뒤집혔다. 그때마다 창이 다시 그려져
        깜빡였다. 그래서 이미 맨 위면 아무것도 하지 않는다.
        """
        if not self._alive:
            return
        try:
            if not self.bar.winfo_viewable():
                self.bar.deiconify()
            hwnd = self._hwnd(self.bar)
            if not (user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST):
                self.bar.attributes("-topmost", True)
                self.bar.lift()
        except Exception:
            self._log_exc("keep_bar")
        self.root.after(1200, self._keep_bar_visible)

    # ------------------------------------------------------------ 툴팁

    def _tip_show(self, text):
        self._tip_job = None
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.config(bg="#D7D7DC")
        tk.Label(t, text=text, bg="#FFFFFF", fg=FG, font=TIP_FONT,
                 padx=10, pady=6, justify="left").pack(padx=1, pady=1)
        t.update_idletasks()
        x = self.root.winfo_pointerx() - t.winfo_reqwidth() // 2
        y = self.bar.winfo_rooty() + self.BH + 10
        t.geometry("+%d+%d" % (x, y))
        self._round_corners(self._hwnd(t))
        if self.capture_excluded:
            t.update()
            self._exclude(self._hwnd(t))
        self._tip = t

    def _tip_hide(self):
        if self._tip_job:
            try:
                self.bar.after_cancel(self._tip_job)
            except Exception:
                pass
            self._tip_job = None
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    # ------------------------------------------------------------ 상태 변경

    def set_tool(self, tid):
        self.tool = tid
        self.set_hand(False)
        self._render_bar(reblur=False)
        if self._ready and not self.drawing_mode:
            self.set_draw_mode(True)   # 도구를 고르면 바로 그릴 수 있게 한다

    def set_color(self, c):
        self.color = c
        self._render_bar(reblur=False)

    def toggle_hand(self):
        self.set_hand(not self.hand_mode)

    def set_hand(self, on):
        """H - 화이트보드를 끌어서 옮기는 손 도구."""
        on = bool(on and self.board_mode)
        if on == self.hand_mode:
            return
        self.hand_mode = on
        self.canvas.config(cursor="fleur" if on else "crosshair")
        if not on:
            self._pan = None
        self._render_bar(reblur=False)

    def _enter_input(self):
        """오버레이가 마우스를 받도록 만든다."""
        self._set_click_through(False)
        self.drawing_mode = True
        self._switch_set(True)
        self.root.lift()
        self.root.focus_force()
        self.bar.lift()

    def _leave_input(self):
        """오버레이를 통과 상태로 되돌린다."""
        self.canvas.delete("bg")
        self.canvas.delete("ui")
        self.canvas.delete("hint")
        self.canvas.delete("ann")   # 필기는 남기고 화면에서만 감춘다
        self.bgimg = None
        self.bg_pil = None
        self._set_click_through(True)
        self.drawing_mode = False
        self._switch_set(False)

    def set_draw_mode(self, on):
        if on and self.board_mode:
            return
        if not on and self.board_mode:
            self.set_board(False)
            return
        if on == self.drawing_mode:
            return
        if on:
            # 스위치를 먼저 움직여 두면 캡처를 기다리는 동안에도 반응이 보인다
            self._switch_set(True)
            img = self._grab()
            self.bg_pil = img
            self.bgimg = ImageTk.PhotoImage(img)
            self.canvas.delete("bg")
            self.canvas.create_image(0, 0, image=self.bgimg, anchor="nw", tags="bg")
            self.canvas.tag_lower("bg")
            self._enter_input()
            self._show_ui()
            self._redraw()          # 지난 필기를 되살린다
        else:
            self._leave_input()
        self._sync_esc()
        self._render_bar(reblur=True)
        self._log_state("draw_mode")
        self.bar.lift()

    def toggle_board(self):
        self.set_board(not self.board_mode)

    def set_board(self, on):
        """화이트보드: 화면 전체를 흰 판으로 덮는다. 필기는 따로 보관된다."""
        if on == self.board_mode:
            return
        self._stop_zoom()
        if on:
            self.board_mode = True
            self.surf = self.board_surf
            self._zoom_target = self.board_surf.scale
            self.canvas.delete("bg")
            self.bgimg = None
            self.bg_pil = None
            self.canvas.create_rectangle(0, 0, VW, VH, fill="#FFFFFF",
                                         outline="", tags="bg")
            self.canvas.tag_lower("bg")
            self._enter_input()
            self._show_ui()
        else:
            self.board_mode = False
            self.set_hand(False)
            self.surf = self.screen_surf
            self._leave_input()
        self._redraw()
        self._sync_esc()
        self._render_bar(reblur=True)
        self._log_state("board")
        self.bar.lift()

    def reset_all(self):
        """Esc: 화이트보드에서 나오고, 그리기를 끄고, 화면 필기를 지운다.
        화이트보드 판서는 남긴다."""
        if self.board_mode:
            self.set_board(False)
        elif self.drawing_mode:
            self.set_draw_mode(False)
        self.surf = self.screen_surf
        if self.screen_surf.strokes:
            self.screen_surf.strokes = []
            self._push()               # Ctrl+Z 로 되살릴 수 있게 남긴다
        self._redraw()
        self._sync_esc()
        self._log_state("reset")

    # ------------------------------------------------------------ 화면 표시

    def _show_ui(self):
        self.canvas.delete("ui")
        if not self.drawing_mode:
            return
        self.canvas.create_rectangle(1, 1, VW - 2, VH - 2,
                                     outline=INK_BLUE, width=2, tags="ui")

    def _show_width_hint(self):
        """굵기를 바꾸는 동안 커서 옆에 실제 크기를 보여준다."""
        self.canvas.delete("hint")
        if not self.drawing_mode:
            return
        x = self.root.winfo_pointerx() - VX
        y = self.root.winfo_pointery() - VY
        color = MARKER_YELLOW if self.tool == "marker" else self.color
        d = self._sw(self.width)
        r = max(1, d / 2.0)
        x = min(max(x + 40 + r, r + 4), VW - r - 4)
        y = min(max(y, r + 4), VH - r - 30)
        if self.tool == "erase":
            self.canvas.create_oval(x - r, y - r, x + r, y + r, outline=FG_DIM,
                                    dash=(4, 3), width=1, tags="hint")
        else:
            self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=color,
                                    outline="", tags="hint")
        self.canvas.create_text(x, y + r + 12, text="%d pt" % self.width,
                                fill=FG, font=("맑은 고딕", 10, "bold"),
                                tags="hint")
        if self._hint_job:
            self.root.after_cancel(self._hint_job)
        self._hint_job = self.root.after(900, lambda: self.canvas.delete("hint"))

    # ------------------------------------------------------------ 확대 / 이동

    def _on_wheel(self, e):
        """그냥 휠 - 화이트보드 확대/축소."""
        if self.board_mode:
            self.zoom(ZOOM_NOTCH if e.delta > 0 else 1 / ZOOM_NOTCH,
                      e.x_root - VX, e.y_root - VY)

    def _on_ctrl_wheel(self, e):
        """Ctrl+휠 - 굵기. 그리기·화이트보드 모두 같다."""
        self.bump_width(1 if e.delta > 0 else -1)

    def zoom(self, factor, cx=None, cy=None):
        """목표 배율만 바꾸고, 실제 이동은 애니메이션이 이어서 처리한다."""
        s = self.surf
        if not (s.zoomable and self.board_mode):
            return
        if cx is None:
            cx, cy = VW / 2.0, VH / 2.0
        self._zoom_anchor = (cx, cy)
        self._zoom_target = min(ZOOM_MAX, max(ZOOM_MIN, self._zoom_target * factor))
        if self._zoom_job is None:
            self._zoom_step()

    def _zoom_step(self):
        s = self.surf
        cx, cy = self._zoom_anchor
        ratio = self._zoom_target / s.scale
        if abs(math.log(ratio)) < 0.004:          # 거의 도착: 정확히 맞추고 끝낸다
            self._zoom_job = None
            if abs(ratio - 1.0) > 1e-9:
                self._apply_zoom(ratio, cx, cy)
            self._redraw()                        # 굵기까지 정확한 최종 렌더
            return
        self._apply_zoom(ratio ** ZOOM_EASE, cx, cy)
        self._zoom_job = self.root.after(ZOOM_FRAME_MS, self._zoom_step)

    def _apply_zoom(self, f, cx, cy):
        """캔버스 아이템을 통째로 변환한다.

        매번 지웠다 다시 그리면 필기가 많을수록 눈에 띄게 끊긴다.
        Tk 의 canvas.scale 은 이미 있는 아이템의 좌표만 바꾸므로 훨씬 싸다.
        선 굵기는 애니메이션이 끝날 때 _redraw 로 한 번만 정확히 맞춘다.
        """
        s = self.surf
        s.ox = cx - (cx - s.ox) * f      # 커서 아래 지점을 고정한 채 확대한다
        s.oy = cy - (cy - s.oy) * f
        s.scale *= f
        self.canvas.scale("ann", cx, cy, f, f)

    def _stop_zoom(self):
        if self._zoom_job:
            try:
                self.root.after_cancel(self._zoom_job)
            except Exception:
                pass
            self._zoom_job = None
        self._zoom_target = self.surf.scale

    def _pan_press(self, e):
        if self.board_mode and self.drawing_mode:
            self._pan = (e.x, e.y, self.surf.ox, self.surf.oy)

    def _pan_move(self, e):
        if not self._pan:
            return
        x0, y0, ox, oy = self._pan
        nx, ny = ox + (e.x - x0), oy + (e.y - y0)
        # 아이템을 옮기기만 하면 되므로 다시 그리지 않는다
        self.canvas.move("ann", nx - self.surf.ox, ny - self.surf.oy)
        self.surf.ox, self.surf.oy = nx, ny

    def _to_surface(self, x, y):
        s = self.surf
        return (x - s.ox) / s.scale, (y - s.oy) / s.scale

    def _to_screen(self, x, y):
        s = self.surf
        return x * s.scale + s.ox, y * s.scale + s.oy

    # ------------------------------------------------------------ 그리기

    def _on_press(self, e):
        if not self.drawing_mode:
            return
        if self.hand_mode:
            self._pan_press(e)
            return
        if self.tool == "erase":
            self._erase_at(e.x, e.y)
            return
        x, y = self._to_surface(e.x, e.y)
        color = MARKER_YELLOW if self.tool == "marker" else self.color
        self.cur = {"type": self.tool, "color": color, "width": self.width,
                    "stipple": "gray50" if self.tool == "marker" else "",
                    "points": [x, y], "start": (x, y), "end": (x, y)}

    def _on_move(self, e):
        if not self.drawing_mode:
            return
        if self.hand_mode:
            self._pan_move(e)
            return
        if self.tool == "erase":
            self._erase_at(e.x, e.y)
            return
        if not self.cur:
            return
        x, y = self._to_surface(e.x, e.y)
        if self.tool in ("pen", "marker"):
            pts = self.cur["points"]
            sx, sy = self._to_screen(pts[-2], pts[-1])
            pts.extend([x, y])
            self.canvas.create_line(sx, sy, e.x, e.y, fill=self.cur["color"],
                                    width=self._sw(self.cur["width"]),
                                    capstyle="round", joinstyle="round",
                                    stipple=self.cur["stipple"], tags="live")
        else:
            self.cur["end"] = (x, y)
            self.canvas.delete("live")
            self._draw_stroke(self.cur, None, tag="live")

    def _on_release(self, e):
        if not self.drawing_mode:
            return
        if self.hand_mode:
            self._pan = None
            return
        if self.tool == "erase":
            self._push()
            self._sync_esc()
            return
        if not self.cur:
            return
        self.cur["end"] = self._to_surface(e.x, e.y)
        if self.tool in ("pen", "marker") and len(self.cur["points"]) == 2:
            self.cur["points"] = self.cur["points"] * 2   # 점 하나만 찍은 경우
        self.surf.strokes.append(self.cur)
        self.cur = None
        self.canvas.delete("live")
        self._push()
        self._redraw()
        self._sync_esc()

    def _sw(self, w):
        """필기 굵기도 확대율을 따른다."""
        return max(1, int(round(w * self.surf.scale)))

    def _draw_stroke(self, s, idx, tag=None):
        tags = (tag,) if tag else ("ann", "st%d" % idx)
        t, c, w = s["type"], s["color"], self._sw(s["width"])
        if t in ("pen", "marker"):
            pts = []
            for i in range(0, len(s["points"]), 2):
                pts.extend(self._to_screen(s["points"][i], s["points"][i + 1]))
            self.canvas.create_line(*pts, fill=c, width=w, capstyle="round",
                                    joinstyle="round", smooth=True,
                                    stipple=s["stipple"], tags=tags)
            return
        x0, y0 = self._to_screen(*s["start"])
        x1, y1 = self._to_screen(*s["end"])
        if t == "rect":
            self.canvas.create_rectangle(x0, y0, x1, y1, outline=c, width=w, tags=tags)

    def _redraw(self):
        self.canvas.delete("ann")
        # 그리기를 끄면 필기를 감춘다. 지우는 것이 아니라 감추는 것이라,
        # 다시 켜면 그대로 되살아난다.
        if not self.drawing_mode:
            return
        for i, s in enumerate(self.surf.strokes):
            self._draw_stroke(s, i)

    def _erase_at(self, x, y):
        r = max(6, self.width)
        hit = self.canvas.find_overlapping(x - r, y - r, x + r, y + r)
        idxs = set()
        for item in hit:
            for tg in self.canvas.gettags(item):
                if tg.startswith("st"):
                    idxs.add(int(tg[2:]))
        if idxs:
            self.surf.strokes = [s for i, s in enumerate(self.surf.strokes)
                                 if i not in idxs]
            self._redraw()

    # ------------------------------------------------------------ 되돌리기

    def _push(self):
        s = self.surf
        s.hist = s.hist[:s.hidx + 1]
        s.hist.append([dict(x) for x in s.strokes])
        if len(s.hist) > 80:
            s.hist.pop(0)
        s.hidx = len(s.hist) - 1

    def _restore(self):
        s = self.surf
        s.strokes = [dict(x) for x in s.hist[s.hidx]]
        self._redraw()
        self._sync_esc()

    def undo(self):
        if self.surf.hidx > 0:
            self.surf.hidx -= 1
            self._restore()

    def redo(self):
        if self.surf.hidx < len(self.surf.hist) - 1:
            self.surf.hidx += 1
            self._restore()

    def clear_all(self):
        if self.surf.strokes:
            self.surf.strokes = []
            self._push()
            self._redraw()
            self._sync_esc()

    # ------------------------------------------------------------ 캡처 / 저장

    def _grab(self, include_overlay=False):
        """화면을 캡처한다.

        캡처 제외가 먹으면 창을 숨길 필요가 없어 깜빡임이 없다.
        저장할 때만 오버레이를 잠깐 캡처 대상에 도로 넣는다.
        """
        self._tip_hide()
        if self.capture_excluded:
            if include_overlay:
                self._exclude(self._hwnd(), False)
            self.root.update()
            if include_overlay:
                time.sleep(0.05)
            img = ImageGrab.grab(bbox=(VX, VY, VX + VW, VY + VH), all_screens=True)
            if include_overlay:
                self._exclude(self._hwnd(), True)
            return img

        # 폴백: 창을 잠깐 숨겼다가 찍는다(깜빡임이 생긴다)
        self.bar.withdraw()
        if not include_overlay:
            self.root.withdraw()
        self.root.update()
        time.sleep(0.18)
        img = ImageGrab.grab(bbox=(VX, VY, VX + VW, VY + VH), all_screens=True)
        if not include_overlay:
            self.root.deiconify()
        self.bar.deiconify()
        self.bar.attributes("-topmost", True)
        self.bar.lift()
        return img

    def save_png(self):
        self.canvas.delete("ui")
        self.canvas.delete("hint")
        img = self._grab(include_overlay=True)
        self._show_ui()
        d = os.path.join(os.path.expanduser("~"), "Pictures", "ScreenPen")
        os.makedirs(d, exist_ok=True)
        name = "screenpen_%s.png" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(d, name)
        img.save(path)
        self._toast("저장됨  ·  %s" % path)

    def _toast(self, msg):
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.config(bg="#D7D7DC")
        tk.Label(t, text=msg, bg="#FFFFFF", fg=FG, font=UI_FONT,
                 padx=18, pady=12).pack(padx=1, pady=1)
        t.update_idletasks()
        t.geometry("+%d+%d" % (VX + (VW - t.winfo_reqwidth()) // 2, VY + VH - 120))
        self._round_corners(self._hwnd(t))
        if self.capture_excluded:
            t.update()
            self._exclude(self._hwnd(t))
        t.after(2200, t.destroy)

    # ------------------------------------------------------------ 전역 단축키

    def _register_hotkeys(self):
        self._hotkeys = {
            1: (MOD_NOREPEAT, VK_F8, "F8",
                lambda: self.set_draw_mode(not self.drawing_mode)),
            2: (MOD_NOREPEAT, VK_F9, "F9", self.clear_all),
            3: (MOD_NOREPEAT | MOD_CONTROL | MOD_ALT, VK_S, "Ctrl+Alt+S",
                self.save_png),
            4: (MOD_NOREPEAT | MOD_CONTROL | MOD_ALT, VK_Q, "Ctrl+Alt+Q", self.quit),
            5: (MOD_NOREPEAT | MOD_CONTROL, VK_0, "Ctrl+0", self.toggle_board),
            ESC_ID: (MOD_NOREPEAT, VK_ESC, "Esc", self.reset_all),
        }
        # Esc 는 필요할 때만 잡는다
        specs = dict((hid, v[:3]) for hid, v in self._hotkeys.items() if hid != ESC_ID)
        self.hk = HotkeyThread(specs)
        self.hk.start()
        self.hk.ready.wait(timeout=3.0)
        self.hotkey_failed = list(self.hk.failed)

    def _sync_esc(self):
        """되돌릴 것이 있을 때만 Esc 를 전역으로 잡는다.
        아무 상태도 없을 때까지 Esc 를 붙들고 있으면 다른 프로그램에 방해가 된다."""
        need = bool(self.drawing_mode or self.board_mode or self.screen_surf.strokes)
        if need == self._esc_on:
            return
        self._esc_on = need
        try:
            self.hk.set_esc(need)
        except Exception:
            self._log_exc("sync_esc")

    def _pump_hotkeys(self):
        """전용 스레드가 넣어 준 단축키를 메인 스레드에서 처리한다.
        이 루프가 한 번이라도 예외로 끊기면 단축키가 통째로 죽으므로,
        무슨 일이 있어도 다음 회차를 반드시 예약한다."""
        try:
            while self._alive:
                try:
                    hid = self.hk.events.get_nowait()
                except queue.Empty:
                    break
                entry = self._hotkeys.get(hid)
                if not entry:
                    continue
                try:
                    entry[3]()
                except Exception:
                    self._log_exc("hotkey %s" % entry[2])
        except Exception:
            self._log_exc("pump")
        finally:
            if self._alive:
                self.root.after(30, self._pump_hotkeys)

    # ------------------------------------------------------------ 실행 / 종료

    def quit(self):
        self._alive = False
        self._tip_hide()
        self.memo.destroy()
        try:
            self.hk.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    # 이전 인스턴스가 살아 있으면 새 인스턴스는 전역 단축키를 하나도 못 잡는다.
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        log("이미 실행 중이라 종료")
        r = tk.Tk()
        r.withdraw()
        from tkinter import messagebox
        messagebox.showinfo("ScreenPen", "ScreenPen이 이미 실행 중입니다.")
        r.destroy()
        return 1
    try:
        ScreenPen().run()
    except Exception:
        log("치명적 오류\n%s" % traceback.format_exc())
        return 2
    finally:
        kernel32.ReleaseMutex(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
