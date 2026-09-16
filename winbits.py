# -*- coding: utf-8 -*-
"""
ScreenPen 의 Windows 연동 계층. 화면 라이브러리(Tk/Qt)와 무관한 것만 모았다.

전역 단축키, 화면 캡처, 창 속성(클릭 통과 / 캡처 제외 / 둥근 모서리),
중복 실행 방지가 들어 있다.
"""

import ctypes
import datetime
import os
import queue
import threading
import time
import traceback
from ctypes import wintypes

from PIL import Image

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi

SRCCOPY = 0x00CC0020
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP_ESC = 0x8001
ESC_ID = 6
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
ERROR_ALREADY_EXISTS = 183

VK_F7, VK_F8, VK_F9, VK_S, VK_Q, VK_0, VK_ESC = (
    0x76, 0x77, 0x78, 0x53, 0x51, 0x30, 0x1B)

user32.GetMessageW.restype = ctypes.c_int

_log_path = None


def set_log_path(path):
    global _log_path
    _log_path = path


def log(msg):
    """창만 띄우는 방식으로 실행하면 오류가 어디에도 안 보이므로 파일에 남긴다."""
    if not _log_path:
        return
    try:
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def set_dpi_aware():
    """모니터별 DPI 를 인지해야 캡처 좌표와 창 좌표가 1:1 로 맞는다."""
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


def virtual_screen():
    """모든 모니터를 합친 영역 (x, y, w, h)."""
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


# ---------------------------------------------------------------- 화면 캡처

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

    PIL 의 ImageGrab 은 영역을 줘도 화면 전체를 먼저 뜬 뒤 잘라내서
    7680x2160 환경에서 한 번에 120ms 넘게 걸린다. 여기서는 GDI 로 그 영역만
    직접 복사한다(같은 영역 기준 약 15ms).
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


# ---------------------------------------------------------------- 창 속성

def exclude_from_capture(hwnd, on=True):
    """창을 화면 캡처 결과에서 뺀다. 화면에는 그대로 보인다."""
    try:
        return bool(user32.SetWindowDisplayAffinity(
            hwnd, WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
    except Exception:
        return False


def set_click_through(hwnd, on):
    """켜면 창 전체가 마우스를 무시한다(아래 프로그램으로 클릭 통과)."""
    try:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        want = (style | WS_EX_TRANSPARENT) if on else (style & ~WS_EX_TRANSPARENT)
        if want != style:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, want)
        return True
    except Exception:
        return False


def is_topmost(hwnd):
    try:
        return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    except Exception:
        return True


def set_tool_window(hwnd, on):
    """도구 창으로 만들면 작업 표시줄과 '바탕화면 보기'(Win+D)의 대상에서 빠진다."""
    try:
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        want = ((ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW if on
                else (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
        if want != ex:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, want)
            return True          # 반영하려면 호출한 쪽에서 창을 다시 보여야 한다
        return False
    except Exception:
        return False


GW_HWNDNEXT = 2


def first_in_z_order(hwnds):
    """주어진 창들 중 화면에서 가장 앞에 있는 것을 돌려준다.

    둘 다 topmost 면 WS_EX_TOPMOST 만으로는 누가 앞인지 알 수 없다.
    실제 Z순서를 훑어야 '툴바가 오버레이에 가렸는지'를 판단할 수 있다.
    """
    h = user32.GetTopWindow(None)
    while h:
        if h in hwnds:
            return h
        h = user32.GetWindow(h, GW_HWNDNEXT)
    return None


def round_corners(hwnd):
    """Windows 11 이 모서리를 둥글게(안티에일리어싱 포함) 깎아 준다."""
    try:
        v = ctypes.c_int(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                     ctypes.byref(v), ctypes.sizeof(v))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 창 이동 제한

WM_MOVING = 0x0216


def moving_rect(msg_addr):
    """WM_MOVING 이면 창이 놓일 사각형을 돌려준다. 아니면 None.

    제목 표시줄로 끄는 창은 Qt 가 아니라 Windows 가 옮기므로 moveEvent 로는
    막을 수 없다. Windows 는 놓을 자리를 정하기 전에 WM_MOVING 을 보내 주는데,
    lParam 이 가리키는 RECT 를 그 자리에서 고치면 그대로 반영된다. 즉 창이
    화면 밖으로 나가려는 순간에 되돌리는 게 아니라, 아예 나가지 못하게 된다.

    돌려주는 것은 살아 있는 RECT 라 필드를 직접 바꿔 쓰면 된다.
    """
    try:
        msg = ctypes.cast(int(msg_addr), ctypes.POINTER(wintypes.MSG)).contents
        if msg.message != WM_MOVING:
            return None
        return ctypes.cast(msg.lParam,
                           ctypes.POINTER(wintypes.RECT)).contents
    except Exception:
        return None


# ---------------------------------------------------------------- 전역 단축키

class HotkeyThread(threading.Thread):
    """전역 단축키를 소유하는 전용 스레드.

    RegisterHotKey 로 등록한 WM_HOTKEY 는 등록한 '스레드'의 메시지 큐로 들어온다.
    이걸 UI 메인 스레드에서 받으려 하면 그쪽 메인루프가 먼저 큐를 비워 버려서
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


# ---------------------------------------------------------------- 중복 실행

def claim_single_instance(name):
    """이미 떠 있으면 None. 이전 인스턴스가 남아 있으면 새 인스턴스는
    전역 단축키를 하나도 잡지 못하므로 아예 막는다."""
    handle = kernel32.CreateMutexW(None, False, name)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def release_single_instance(handle):
    if handle:
        try:
            kernel32.ReleaseMutex(handle)
        except Exception:
            pass
