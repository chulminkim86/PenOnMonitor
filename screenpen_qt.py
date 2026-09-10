# -*- coding: utf-8 -*-
"""
ScreenPen (Qt) - 화면 위에 바로 쓰는 필기 도구.

Tk 판과 기능은 같고, 그리는 계층만 Qt + OpenGL 로 바꾼 것이다. 바뀐 점:

  - 획에 안티에일리어싱이 붙는다 (Tk 캔버스는 밝기 2단계, 여기는 6~13단계)
  - 확대가 화면 변환이라 획이 몇 개든 60fps 로 고정된다
    (Tk 는 획 800개에서 63ms/프레임, 여기는 16.7ms 고정)
  - 획을 Catmull-Rom 곡선으로 이어 각진 느낌을 없앴다
  - 형광펜이 진짜 반투명이다 (디더링 흉내가 아니다)
  - 투명 오버레이가 입력을 받을 수 있어 **화면을 얼리지 않는다**.
    재생 중인 영상이나 스크롤 위에 그대로 그릴 수 있다.
"""

import os
# Qt 가 화면 배율만큼 좌표를 늘리면 화면 캡처/Win32 좌표와 어긋난다. 1:1 로 고정한다.
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_SCALE_FACTOR"] = "1"

import datetime
import math
import queue
import sys
import time
import traceback

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QPainter,
                           QPainterPath, QPen, QPixmap, QSurfaceFormat)
from PySide6.QtWidgets import (QApplication, QGraphicsPathItem,
                               QGraphicsRectItem, QGraphicsScene, QGraphicsView,
                               QWidget)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

import winbits as wb

def _data_dir():
    """메모·설정·로그를 둘 곳.

    USB 에 담아 들고 다니며 쓰므로 실행 파일 옆에 둔다. 그래야 메모가 USB 를
    따라다닌다. 얼린 exe 에서 __file__ 은 임시 폴더를 가리키므로 쓰면 안 된다.
    """
    if getattr(sys, "frozen", False):
        d = os.path.dirname(sys.executable)
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    if not os.access(d, os.W_OK):          # 읽기 전용 매체면 사용자 폴더로
        d = os.path.join(os.path.expanduser("~"), "ScreenPen")
        os.makedirs(d, exist_ok=True)
    return d


def _res_dir():
    """로고처럼 번들에 함께 들어가는 읽기 전용 자원이 있는 곳."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _res_dir()
DATA_DIR = _data_dir()
wb.set_log_path(os.path.join(DATA_DIR, "screenpen_qt.log"))
log = wb.log

wb.set_dpi_aware()
VX, VY, VW, VH = wb.virtual_screen()

MUTEX_NAME = "Local\\ScreenPen_Qt_SingleInstance_v1"

# ---------------------------------------------------------------- 팔레트

INK_BLACK = "#0D0D0D"
INK_BLUE = "#2569A7"
INK_RED = "#A20000"
MARKER_YELLOW = "#FFE24D"
MARKER_ALPHA = 110              # 진짜 반투명. Tk 에서는 디더링으로 흉내 냈다

COLORS = [INK_BLACK, INK_RED, INK_BLUE]
COLOR_NAMES = ["검정", "빨강", "파랑"]

GLASS_BLUR = 16
GLASS_TINT = (255, 255, 255)
GLASS_TINT_A = 0.58
GLASS_RADIUS = 16
GLASS_RIM_TOP = (255, 255, 255, 210)
GLASS_RIM = (255, 255, 255, 110)
GLASS_EDGE = (13, 13, 13, 38)
GLASS_SEP = (13, 13, 13, 32)
PILL_SEL = (37, 105, 167, 44)
PILL_HOVER = (13, 13, 13, 18)
SWATCH_EDGE = (13, 13, 13, 60)

FG = "#0D0D0D"
FG_DIM = "#6E6E76"
SWITCH_OFF = "#B9B9C2"

TOOLS = [
    ("pen", "펜", "Ctrl+1"),
    ("erase", "지우개", "Ctrl+2"),
    ("rect", "사각", "Ctrl+3"),
    ("marker", "형광", "Ctrl+4"),
]
ACTIONS = [
    ("board", "화이트보드"),
    ("clear", "전체지우기"),
    ("memo", "메모"),
    ("save", "저장"),
]

WIDTH_MIN, WIDTH_MAX = 1, 50
DEFAULT_WIDTHS = {"pen": 5, "erase": 20, "rect": 5, "marker": 24}

ZOOM_MIN, ZOOM_MAX = 0.25, 8.0
ZOOM_NOTCH = 1.18
ZOOM_EASE = 0.34
ZOOM_FRAME_MS = 8
GL_SAMPLES = 4          # 멀티샘플 안티에일리어싱

SWITCH_MS = 190
GLASS_REFRESH_MS = 400
SEP_PAD = 7

UI_FONT_NAME = "맑은 고딕"
UI_FONT_SIZE = 10


def qcolor(name, alpha=255):
    c = QColor(name)
    c.setAlpha(alpha)
    return c


def catmull_rom(points):
    """찍힌 점들을 곡선으로 이어 붙인다.

    직선으로 이으면 방향이 꺾일 때마다 각이 져서 '거친' 느낌이 난다.
    각 구간을 3차 베지에로 바꾸면 손으로 쓴 것처럼 이어진다.
    """
    path = QPainterPath()
    if not points:
        return path
    p = [QPointF(x, y) for x, y in points]
    if len(p) == 1:
        path.moveTo(p[0])
        path.lineTo(p[0].x() + 0.01, p[0].y())
        return path
    if len(p) < 4:
        path.moveTo(p[0])
        for q in p[1:]:
            path.lineTo(q)
        return path
    p = [p[0]] + p + [p[-1]]
    path.moveTo(p[1])
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        path.cubicTo(QPointF(p1.x() + (p2.x() - p0.x()) / 6.0,
                             p1.y() + (p2.y() - p0.y()) / 6.0),
                     QPointF(p2.x() - (p3.x() - p1.x()) / 6.0,
                             p2.y() - (p3.y() - p1.y()) / 6.0),
                     p2)
    return path


def smooth_points(points, window=3):
    """손떨림을 눌러 준다. 시작과 끝점은 건드리지 않는다."""
    if len(points) < window + 2:
        return list(points)
    out = [points[0]]
    half = window // 2
    for i in range(1, len(points) - 1):
        lo, hi = max(0, i - half), min(len(points), i + half + 1)
        seg = points[lo:hi]
        out.append((sum(p[0] for p in seg) / len(seg),
                    sum(p[1] for p in seg) / len(seg)))
    out.append(points[-1])
    return out


class Surface:
    """필기 한 벌. 화면용과 화이트보드용을 따로 둔다."""

    def __init__(self, zoomable=False):
        self.strokes = []
        self.hist = [[]]
        self.hidx = 0
        self.zoomable = zoomable
        self.scale = 1.0
        self.dx = 0.0
        self.dy = 0.0
        self.view_tf = None            # 이 판에서 보던 배율
        self.view_center = None        # 이 판에서 보던 한가운데

    def push(self):
        self.hist = self.hist[:self.hidx + 1]
        self.hist.append([dict(s) for s in self.strokes])
        if len(self.hist) > 80:
            self.hist.pop(0)
        self.hidx = len(self.hist) - 1

    def undo(self):
        if self.hidx > 0:
            self.hidx -= 1
            self.strokes = [dict(s) for s in self.hist[self.hidx]]
            return True
        return False

    def redo(self):
        if self.hidx < len(self.hist) - 1:
            self.hidx += 1
            self.strokes = [dict(s) for s in self.hist[self.hidx]]
            return True
        return False


class Overlay(QGraphicsView):
    """화면 전체를 덮는 투명 오버레이.

    통과 모드에서는 창 전체가 마우스를 무시하고, 그리기/화이트보드에서는 입력을
    받는다. Tk 판과 달리 화면을 캡처해 얼릴 필요가 없다. 투명한 채로 입력을
    받을 수 있어서, 살아 있는 화면 위에 그대로 그린다.
    """

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setViewport(QOpenGLWidget())      # 확대가 60fps 로 고정되는 핵심
        # OpenGL 뷰포트에서 기본값(부분 갱신)을 쓰면 바뀐 자리만 다시 그리는데,
        # 지워야 할 예전 그림이 그대로 남는다. 드래그 중인 사각형이 겹쳐 쌓이고,
        # 반투명 형광이 덧칠돼 불투명해지고, 지운 획이 화면에 남았다.
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

        self.screen_scene = QGraphicsScene(-VW, -VH, VW * 3, VH * 3)
        self.screen_scene.setBackgroundBrush(QBrush(Qt.transparent))
        self.board_scene = QGraphicsScene(-6000, -4000, 18000, 12000)
        self.board_scene.setBackgroundBrush(QBrush(QColor("#FFFFFF")))
        self.setScene(self.screen_scene)
        self.setGeometry(VX, VY, VW, VH)
        self._aligned = False

        self.live_item = None
        self.cur = None
        self._pan = None
        self._zoom_target = 1.0
        self._zoom_at = None
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setInterval(ZOOM_FRAME_MS)
        self._zoom_timer.timeout.connect(self._zoom_step)

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._aligned:
            # 창 크기가 실제로 잡힌 뒤라야 시야를 맞출 수 있다
            self._aligned = True
            self.align_view()

    def align_view(self, surf=None):
        """장면 좌표 (0,0) 이 화면 좌상단에 오도록 시야를 맞춘다.

        장면을 화면보다 크게 잡아야 손 도구로 밖으로 끌 수 있는데, 그러면
        시야가 장면 좌상단에 붙어 화면 좌표와 어긋난다. 그래서 명시적으로 맞춘다.
        판을 오갈 때는 그 판에서 보던 자리와 배율을 그대로 되살린다.
        """
        if surf is not None and getattr(surf, "view_tf", None) is not None:
            self.setTransform(surf.view_tf)
            self.centerOn(surf.view_center)
        else:
            self.resetTransform()
            self.centerOn(QPointF(VW / 2.0, VH / 2.0))
            for _ in range(3):         # 남은 한두 픽셀까지 맞춘다
                d = self.mapToScene(0, 0)
                if abs(d.x()) < 0.5 and abs(d.y()) < 0.5:
                    break
                self.translate(d.x(), d.y())

    def remember_view(self, surf):
        surf.view_tf = self.transform()
        surf.view_center = self.mapToScene(self.viewport().rect().center())

    def drawBackground(self, painter, rect):
        """매 프레임 화면을 실제로 비운다.

        투명 배경(=칠할 것이 없음)은 지우개 역할을 못 해서, 이전 프레임 픽셀이
        그대로 남았다. 드래그 중인 사각형이 겹쳐 쌓이고, 반투명 형광이 덧칠돼
        불투명해지고, 지운 획이 화면에 남은 것이 전부 이 때문이었다.
        덮어쓰기(Source) 모드로 채우면 알파까지 그대로 교체된다.
        """
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(rect, QColor("#FFFFFF") if self.app.board_mode
                         else Qt.transparent)
        painter.restore()

    # ------------------------------------------------------------ 항목 만들기

    def _pen_for(self, stroke):
        color = QColor(stroke["color"])
        color.setAlpha(stroke.get("alpha", 255))
        pen = QPen(color, float(stroke["width"]))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def _add_item(self, stroke):
        if stroke["type"] == "rect":
            x0, y0 = stroke["start"]
            x1, y1 = stroke["end"]
            item = QGraphicsRectItem(QRectF(min(x0, x1), min(y0, y1),
                                            abs(x1 - x0), abs(y1 - y0)))
            item.setBrush(QBrush(Qt.NoBrush))
        else:
            item = QGraphicsPathItem(catmull_rom(stroke["points"]))
        item.setPen(self._pen_for(stroke))
        # 형광은 밑에 깔려야 글씨를 덮지 않는다
        item.setZValue(-1 if stroke["type"] == "marker" else 0)
        self.scene().addItem(item)
        return item

    def rebuild(self):
        surf = self.app.surf
        for it in list(self.scene().items()):
            self.scene().removeItem(it)
        surf.items = [self._add_item(s) for s in surf.strokes]
        self.live_item = None
        self.cur = None

    # ------------------------------------------------------------ 마우스

    def _scene_pos(self, ev):
        p = self.mapToScene(ev.position().toPoint())
        return (p.x(), p.y())

    def mousePressEvent(self, ev):
        app = self.app
        if not app.input_mode:
            return
        ctrl = bool(ev.modifiers() & Qt.ControlModifier)
        if (ev.button() == Qt.MiddleButton or ctrl
                or (app.hand_mode and app.board_mode)):
            self._pan = ev.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if ev.button() == Qt.RightButton:
            app.reset_all()
            return
        if ev.button() != Qt.LeftButton:
            return
        x, y = self._scene_pos(ev)
        if app.tool == "erase":
            self._erase_at(x, y)
            return
        if app.tool == "marker":
            color, alpha = MARKER_YELLOW, MARKER_ALPHA
        else:
            color, alpha = app.color, 255
        self.cur = {"type": app.tool, "color": color, "alpha": alpha,
                    "width": app.width, "points": [(x, y)],
                    "start": (x, y), "end": (x, y)}
        self.live_item = self._add_item(self.cur)

    def mouseMoveEvent(self, ev):
        app = self.app
        if self._pan is not None:
            d = ev.position() - self._pan
            self._pan = ev.position()
            sc = self.transform().m11()
            self.translate(d.x() / sc, d.y() / sc)
            return
        if not app.input_mode:
            return
        x, y = self._scene_pos(ev)
        if app.tool == "erase" and (ev.buttons() & Qt.LeftButton):
            self._erase_at(x, y)
            return
        if self.cur is None:
            return
        if self.cur["type"] == "rect":
            self.cur["end"] = (x, y)
            x0, y0 = self.cur["start"]
            self.live_item.setRect(QRectF(min(x0, x), min(y0, y),
                                          abs(x - x0), abs(y - y0)))
        else:
            self.cur["points"].append((x, y))
            self.live_item.setPath(catmull_rom(self.cur["points"]))

    def mouseReleaseEvent(self, ev):
        app = self.app
        if self._pan is not None:
            self._pan = None
            self.setCursor(Qt.OpenHandCursor if app.hand_mode
                           else Qt.CrossCursor)
            return
        if not app.input_mode:
            return
        if app.tool == "erase":
            app.surf.push()
            app.sync_esc()
            return
        if self.cur is None:
            return
        if self.cur["type"] != "rect":
            # 손떨림을 눌러 준 뒤 확정한다
            self.cur["points"] = smooth_points(self.cur["points"])
            self.live_item.setPath(catmull_rom(self.cur["points"]))
        app.surf.strokes.append(self.cur)
        app.surf.items.append(self.live_item)
        self.cur = None
        self.live_item = None
        app.surf.push()
        app.sync_esc()

    def _erase_at(self, x, y):
        app = self.app
        r = max(6, app.width)
        hit = set(self.scene().items(QRectF(x - r, y - r, r * 2, r * 2)))
        if not hit:
            return
        keep_s, keep_i, removed = [], [], False
        for s, it in zip(app.surf.strokes, app.surf.items):
            if it in hit:
                self.scene().removeItem(it)
                removed = True
            else:
                keep_s.append(s)
                keep_i.append(it)
        if removed:
            app.surf.strokes, app.surf.items = keep_s, keep_i

    # ------------------------------------------------------------ 확대 / 이동

    def wheelEvent(self, ev):
        app = self.app
        if not app.input_mode:
            return
        up = ev.angleDelta().y() > 0
        if ev.modifiers() & Qt.ControlModifier:
            app.bump_width(1 if up else -1)
            return
        if app.board_mode:
            self.zoom(ZOOM_NOTCH if up else 1 / ZOOM_NOTCH, ev.position())

    def zoom(self, factor, at=None):
        if not self.app.board_mode:
            return
        cur = self.transform().m11()
        self._zoom_at = at or QPointF(self.width() / 2.0, self.height() / 2.0)
        base = self._zoom_target if self._zoom_timer.isActive() else cur
        self._zoom_target = min(ZOOM_MAX, max(ZOOM_MIN, base * factor))
        if not self._zoom_timer.isActive():
            self._zoom_timer.start()

    def _zoom_step(self):
        cur = self.transform().m11()
        ratio = self._zoom_target / cur
        if abs(math.log(ratio)) < 0.004:
            self._apply_zoom(ratio)
            self._zoom_timer.stop()
            return
        self._apply_zoom(ratio ** ZOOM_EASE)

    def _apply_zoom(self, f):
        if abs(f - 1.0) < 1e-9:
            return
        before = self.mapToScene(self._zoom_at.toPoint())
        self.scale(f, f)
        after = self.mapToScene(self._zoom_at.toPoint())
        d = after - before                 # 커서 아래 지점을 고정한다
        self.translate(d.x(), d.y())


def pil_to_qpixmap(im):
    im = im.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    qimg = QImage(data, im.width, im.height, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


def load_logo(height=22):
    """폴더에 놓인 logo.png 를 읽고 여백을 잘라 낸다."""
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
            log("logo " + traceback.format_exc())
            return None
    return None


class GlassBar(QWidget):
    """유리 툴바.

    Tk 판과 같은 방식이다. Qt 에도 backdrop-filter 는 없으므로, 툴바 뒤 영역을
    GDI 로 떠서 흐리게 만든 뒤 흰 틴트를 얹어 배경으로 깐다. 알약·글자·스위치는
    QPainter 로 그 위에 그린다(알파와 안티에일리어싱이 되므로 Tk 때처럼
    PIL 로 미리 합성할 필요가 없다).
    """

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool)
        self.setMouseTracking(True)

        self.font_reg = QFont(UI_FONT_NAME, UI_FONT_SIZE)
        self.font_bold = QFont(UI_FONT_NAME, UI_FONT_SIZE, QFont.Bold)
        self.fm = QFontMetrics(self.font_bold)
        self.logo = load_logo()
        self.logo_pix = pil_to_qpixmap(self.logo) if self.logo else None

        self._layout()
        self.setFixedSize(self.BW, self.BH)
        self.move(VX + (VW - self.BW) // 2, VY + 14)

        self.glass = None
        self.hover = None
        self._drag = None
        self.sw_on = False
        self.sw_pos = 0.0                  # 0 = 왼쪽, 1 = 오른쪽
        self._sw_from = 0.0
        self._sw_t0 = 0.0
        self._sw_timer = QTimer(self)
        self._sw_timer.setInterval(8)
        self._sw_timer.timeout.connect(self._sw_step)

    # ------------------------------------------------------------ 배치

    def _layout(self):
        GAP, H = 4, 52
        self.BH = H
        y0, y1 = 9, H - 9
        self.zones = []
        lw = self.logo.width if self.logo else 22
        lh = self.logo.height if self.logo else 22
        LOGO_GAP = 19
        self.logo_pos = (LOGO_GAP, (H - lh) // 2)
        sep_x = LOGO_GAP + lw + LOGO_GAP
        self.seps = [sep_x]

        def add(kind, zid, x, w, label=None, tip=None):
            self.zones.append({"kind": kind, "id": zid, "label": label,
                               "tip": tip, "rect": (x, y0, x + w, y1)})
            return x + w + GAP

        add("logo", "logo", LOGO_GAP, lw)
        x = sep_x + SEP_PAD + SEP_PAD

        for tid, label, key in TOOLS:
            tip = "%s  ·  %s" % (label, key)
            x = add("tool", tid, x, self.fm.horizontalAdvance(label) + 22,
                    label, tip)
        x += SEP_PAD - GAP
        self.seps.append(x)
        x += SEP_PAD
        for i, (c, cname) in enumerate(zip(COLORS, COLOR_NAMES), start=1):
            x = add("color", c, x, 30, None, "%s  ·  Alt+%d" % (cname, i))
        x += SEP_PAD - GAP
        self.seps.append(x)
        x += SEP_PAD
        tips = {"board": "화이트보드  ·  Ctrl+0", "clear": "전체 지우기  ·  F9",
                "memo": "메모장 열기/닫기", "save": "PNG 저장  ·  Ctrl+Alt+S"}
        for aid, label in ACTIONS:
            x = add("action", aid, x, self.fm.horizontalAdvance(label) + 22,
                    label, tips[aid])
        x += SEP_PAD - GAP
        self.seps.append(x)
        x += SEP_PAD
        x = add("switch", "switch", x,
                self.fm.horizontalAdvance("그리기") + 12, "그리기",
                "그리기 모드  ·  F8")
        self.switch_rect = (x, (H - 24) // 2, 44, 24)
        self.zones.append({"kind": "switch", "id": "switch", "label": None,
                           "tip": "그리기 모드  ·  F8",
                           "rect": (x, y0, x + 44, y1)})
        x += 44 + SEP_PAD
        x = add("close", "close", x, 30, "✕", "종료  ·  Ctrl+Alt+Q")
        self.BW = x - GAP + 14

    # ------------------------------------------------------------ 유리 배경

    def refresh_glass(self):
        W, H = self.BW, self.BH
        try:
            if self.app.board_mode:
                src = Image.new("RGB", (W, H), "#FFFFFF")
            else:
                src = wb.grab_rect(self.x(), self.y(), W, H)
            small = src.resize((max(1, W // 4), max(1, H // 4)), Image.BILINEAR)
            small = small.filter(ImageFilter.GaussianBlur(GLASS_BLUR / 4.0))
            blurred = small.resize((W, H), Image.BILINEAR)
            glass = Image.blend(blurred, Image.new("RGB", (W, H), GLASS_TINT),
                                GLASS_TINT_A)
            self.glass = pil_to_qpixmap(glass)
        except Exception:
            log("glass " + traceback.format_exc())
        self.update()

    # ------------------------------------------------------------ 그리기

    def _selected(self, z):
        app = self.app
        if z["kind"] == "tool":
            return z["id"] == app.tool and not app.hand_mode
        if z["kind"] == "color":
            return z["id"] == app.color
        if z["kind"] == "action":
            if z["id"] == "board":
                return app.board_mode
            if z["id"] == "memo":
                return app.memo is not None and app.memo.isVisible()
        return False

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self.glass is not None:
            p.drawPixmap(0, 0, self.glass)
        else:
            p.fillRect(self.rect(), QColor("#F2F2F4"))

        for z in self.zones:
            x0, y0, x1, y1 = z["rect"]
            live = self.app.enabled(z)
            if self._selected(z) and live:
                p.setBrush(QBrush(QColor(*PILL_SEL)))
            elif self.hover == z["id"] and z["kind"] != "logo" and live:
                p.setBrush(QBrush(QColor(*PILL_HOVER)))
            else:
                continue
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(x0, y0, x1 - x0, y1 - y0), 9, 9)

        p.setPen(QPen(QColor(*GLASS_SEP), 1))
        for sx in self.seps:
            p.drawLine(sx, 15, sx, self.BH - 15)

        for z in self.zones:
            if z["kind"] != "color":
                continue
            x0, y0, x1, y1 = z["rect"]
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            sel = self.app.color == z["id"]
            r = 9.5 if sel else 8
            if sel:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor(255, 255, 255, 235)))
                p.drawEllipse(QPointF(cx, cy), r + 2.5, r + 2.5)
            col = QColor(z["id"])
            if not self.app.enabled(z):
                col.setAlpha(70)
            p.setPen(QPen(QColor(*SWATCH_EDGE), 1))
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(cx, cy), r, r)

        if self.logo_pix is not None:
            p.drawPixmap(self.logo_pos[0], self.logo_pos[1], self.logo_pix)

        for z in self.zones:
            if not z.get("label"):
                continue
            sel = self._selected(z)
            if z["id"] == "close":
                col = QColor(INK_RED) if self.hover == "close" else QColor(FG_DIM)
            elif z["kind"] == "switch":
                col = QColor(INK_BLUE) if self.app.input_mode else QColor(FG_DIM)
            else:
                col = QColor(INK_BLUE) if sel else QColor(FG)
            if not self.app.enabled(z):
                col = QColor("#B6B6BE")     # 그리기 off 에서는 눌러도 안 되는 것들
            p.setPen(QPen(col))
            p.setFont(self.font_bold if sel or z["kind"] == "switch"
                      else self.font_reg)
            x0, y0, x1, y1 = z["rect"]
            p.drawText(QRectF(x0, y0, x1 - x0, y1 - y0),
                       Qt.AlignCenter, z["label"])

        # 스위치
        sx, sy, sw, sh = self.switch_rect
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(INK_BLUE) if self.sw_on
                          else QColor(SWITCH_OFF)))
        p.drawRoundedRect(QRectF(sx, sy, sw, sh), sh / 2.0, sh / 2.0)
        pad, d = 3, sh - 6
        kx = sx + pad + (sw - pad * 2 - d) * self.sw_pos
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.setPen(QPen(QColor("#A9A9B4"), 1))
        p.drawEllipse(QRectF(kx, sy + pad, d, d))

        # 유리 테두리
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(*GLASS_EDGE), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.BW - 1, self.BH - 1),
                          GLASS_RADIUS, GLASS_RADIUS)
        p.setPen(QPen(QColor(*GLASS_RIM), 1))
        p.drawRoundedRect(QRectF(1.5, 1.5, self.BW - 3, self.BH - 3),
                          GLASS_RADIUS - 1, GLASS_RADIUS - 1)
        p.setPen(QPen(QColor(*GLASS_RIM_TOP), 1))
        p.drawLine(GLASS_RADIUS, 1, self.BW - GLASS_RADIUS, 1)
        p.end()

    # ------------------------------------------------------------ 스위치 애니메이션

    def switch_set(self, on):
        if on == self.sw_on:
            return
        self.sw_on = on
        self._sw_from = self.sw_pos
        self._sw_t0 = time.perf_counter()
        if not self._sw_timer.isActive():
            self._sw_timer.start()
        self.update()

    def _sw_step(self):
        target = 1.0 if self.sw_on else 0.0
        t = (time.perf_counter() - self._sw_t0) * 1000.0 / SWITCH_MS
        if t >= 1.0:
            self.sw_pos = target
            self._sw_timer.stop()
        else:
            e = 1 - (1 - t) ** 3           # 감속. 등속이면 뚝 끊겨 보인다
            self.sw_pos = self._sw_from + (target - self._sw_from) * e
        self.update()

    # ------------------------------------------------------------ 입력

    def _zone_at(self, x, y):
        for z in self.zones:
            x0, y0, x1, y1 = z["rect"]
            if x0 <= x <= x1 and y0 <= y <= y1:
                return z
        return None

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            g = ev.globalPosition().toPoint()
            self.move(g.x() - self._drag[0], g.y() - self._drag[1])
            return
        z = self._zone_at(ev.position().x(), ev.position().y())
        zid = z["id"] if z else None
        if zid != self.hover:
            self.hover = zid
            self.setToolTip(z["tip"] if z and z.get("tip") else "")
            self.update()

    def leaveEvent(self, _ev):
        self.hover = None
        self.update()

    def mousePressEvent(self, ev):
        z = self._zone_at(ev.position().x(), ev.position().y())
        if z is None:
            return
        if z["kind"] == "logo":
            g = ev.globalPosition().toPoint()
            self._drag = (g.x() - self.x(), g.y() - self.y())
            return
        self.app.on_bar_click(z)

    def mouseReleaseEvent(self, _ev):
        if self._drag is not None:
            self._drag = None
            self.refresh_glass()           # 옮긴 자리의 배경으로 다시 흐린다


class Memo(QWidget):
    """메모장. Tk 판과 같은 규칙으로 동작한다."""

    MIN_W, MIN_H = 260, 180
    FONT_MIN, FONT_MAX, FONT_DEFAULT = 8, 96, 11

    def __init__(self, app):
        super().__init__()
        from PySide6.QtWidgets import (QCheckBox, QPlainTextEdit, QVBoxLayout,
                                       QFrame)
        self.app = app
        self.path = os.path.join(DATA_DIR, "memo.txt")
        self.conf_path = os.path.join(DATA_DIR, "memo_qt.json")
        self.font_size = self.FONT_DEFAULT
        self.pinned = True
        self._load_conf()

        self.setWindowTitle("메모  ·  ScreenPen")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.pin_box = QCheckBox("항상 띄워놓기")
        self.pin_box.setChecked(self.pinned)
        self.pin_box.setStyleSheet("padding:6px 8px; background:#F4F4F6;")
        self.pin_box.toggled.connect(self._on_pin)
        lay.addWidget(self.pin_box)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#DCDCE2;")
        lay.addWidget(line)
        self.text = QPlainTextEdit()
        self.text.setFrameShape(QFrame.NoFrame)
        self.text.setStyleSheet("background:#FFFFFF; padding:8px;")
        self._apply_font()
        lay.addWidget(self.text, 1)

        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self._load()
        self.text.textChanged.connect(self._on_changed)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self.save)
        self.text.installEventFilter(self)

    def place_default(self):
        """툴바 아래, 툴바 절반 크기의 정사각형. 메모 버튼과 가운데를 맞춘다."""
        bar = self.app.bar
        w = bar.BW // 2
        y = bar.y() + bar.BH + 10
        h = min(bar.BW // 2, VY + VH - y - 60)
        cx = bar.x() + bar.BW // 2
        for z in bar.zones:
            if z["id"] == "memo":
                cx = bar.x() + (z["rect"][0] + z["rect"][2]) // 2
                break
        x = max(VX + 8, min(cx - w // 2, VX + VW - w - 8))
        # setGeometry 는 제목 표시줄을 뺀 안쪽을 잡아서 창이 위로 밀려 올라간다.
        # resize + move 를 써야 창 전체가 툴바 바로 아래에 놓인다.
        self.resize(w, max(self.MIN_H, h))
        self.move(x, y)

    # ------------------------------------------------------------ 글자 크기

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.text and ev.type() == QEvent.Wheel:
            if ev.modifiers() & Qt.ControlModifier:
                self.bump_font(1 if ev.angleDelta().y() > 0 else -1)
                return True                # 기본 스크롤을 막는다
        return False

    def bump_font(self, step):
        size = max(self.FONT_MIN, min(self.FONT_MAX, self.font_size + step))
        if size == self.font_size:
            return
        self.font_size = size
        self._apply_font()
        self._save_conf()

    def _apply_font(self):
        self.text.setFont(QFont(UI_FONT_NAME, self.font_size))

    # ------------------------------------------------------------ 항상 띄워놓기

    def _on_pin(self, on):
        self.pinned = bool(on)
        self.apply_pin()
        self._save_conf()

    def apply_pin(self):
        """도구 창으로 만들면 작업 표시줄과 '바탕화면 보기'에서 빠져
        Win+D 를 눌러도 남는다.

        Qt.Tool 이 곧 WS_EX_TOOLWINDOW 다. 직접 확장 스타일을 넣으면
        setWindowFlags 가 네이티브 창을 다시 만들면서 지워 버리므로 쓰지 않는다.
        """
        was = self.isVisible()
        flags = (Qt.Tool | Qt.WindowStaysOnTopHint) if self.pinned else Qt.Window
        self.setWindowFlags(flags)
        if was or self.pinned:
            self.show()

    # ------------------------------------------------------------ 저장

    def _load_conf(self):
        import json
        try:
            with open(self.conf_path, encoding="utf-8") as f:
                c = json.load(f)
            self.font_size = max(self.FONT_MIN,
                                 min(self.FONT_MAX,
                                     int(c.get("font_size", self.FONT_DEFAULT))))
            self.pinned = bool(c.get("pinned", True))
        except Exception:
            pass

    def _save_conf(self):
        import json
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
                    self.text.setPlainText(f.read())
        except Exception:
            pass

    def _on_changed(self):
        self._save_timer.start()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(self.text.toPlainText())
        except Exception:
            pass

    def closeEvent(self, ev):
        self.save()
        self.app.bar.update()
        super().closeEvent(ev)


class ScreenPenQt:
    """상태와 흐름을 쥔 쪽. 화면 그리기는 Overlay, 메뉴는 GlassBar 가 맡는다."""

    def __init__(self):
        self.tool = "pen"
        self.color = INK_BLACK
        self.widths = dict(DEFAULT_WIDTHS)
        self.input_mode = False            # 오버레이가 입력을 받는 상태
        self.board_mode = False
        self.hand_mode = False
        self._draw_before_board = False
        self.memo = None
        self._esc_on = False

        self.screen_surf = Surface()
        self.screen_surf.items = []
        self.board_surf = Surface(zoomable=True)
        self.board_surf.items = []
        self.surf = self.screen_surf

        self.overlay = Overlay(self)
        self.bar = GlassBar(self)
        self.bar.show()
        self.bar.refresh_glass()
        wb.round_corners(int(self.bar.winId()))
        wb.exclude_from_capture(int(self.bar.winId()))
        self.overlay.show()
        wb.exclude_from_capture(int(self.overlay.winId()))
        self.overlay.hide()                # 통과 모드에서는 아예 감춘다

        self._register_hotkeys()
        self.pump = QTimer()
        self.pump.setInterval(30)
        self.pump.timeout.connect(self._pump_hotkeys)
        self.pump.start()
        self.glass_timer = QTimer()
        self.glass_timer.setInterval(GLASS_REFRESH_MS)
        self.glass_timer.timeout.connect(self._glass_tick)
        self.glass_timer.start()
        self.hint_timer = QTimer()
        self.hint_timer.setSingleShot(True)
        self.hint_timer.setInterval(900)
        self.hint_timer.timeout.connect(self._clear_hint)
        self.hint_items = []

    # ------------------------------------------------------------ 굵기

    @property
    def width(self):
        return self.widths.get(self.tool, 5)

    def set_width(self, w):
        self.widths[self.tool] = max(WIDTH_MIN, min(WIDTH_MAX, int(round(w))))

    def bump_width(self, step):
        if not self.input_mode:
            return
        self.set_width(self.width + step)
        self._show_width_hint()

    def _clear_hint(self):
        for it in self.hint_items:
            try:
                it.scene().removeItem(it)
            except Exception:
                pass
        self.hint_items = []

    def _show_width_hint(self):
        from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsSimpleTextItem
        self._clear_hint()
        sc = self.overlay.scene()
        pos = self.overlay.mapToScene(
            self.overlay.mapFromGlobal(self.overlay.cursor().pos()))
        r = max(1.0, self.width / 2.0)
        col = QColor(MARKER_YELLOW if self.tool == "marker" else self.color)
        dot = QGraphicsEllipseItem(QRectF(pos.x() + 40, pos.y() - r, r * 2, r * 2))
        dot.setBrush(QBrush(col))
        dot.setPen(QPen(QColor(FG_DIM), 1) if self.tool == "erase" else Qt.NoPen)
        txt = QGraphicsSimpleTextItem("%d pt" % self.width)
        txt.setFont(QFont(UI_FONT_NAME, 10, QFont.Bold))
        txt.setBrush(QBrush(QColor(FG)))
        txt.setPos(pos.x() + 40, pos.y() + r + 4)
        for it in (dot, txt):
            sc.addItem(it)
        self.hint_items = [dot, txt]
        self.hint_timer.start()

    # ------------------------------------------------------------ 상태 전환

    def set_tool(self, tid):
        if not self.input_mode:
            return                  # 그리기가 꺼져 있으면 도구도 바꾸지 않는다
        self.tool = tid
        self.hand_mode = False
        self.bar.update()

    def set_color(self, c):
        if not self.input_mode:
            return
        self.color = c
        self.bar.update()

    def toggle_hand(self):
        if not self.board_mode:
            return
        self.hand_mode = not self.hand_mode
        self.overlay.setCursor(Qt.OpenHandCursor if self.hand_mode
                               else Qt.CrossCursor)
        self.bar.update()

    def set_input(self, on):
        """켜면 오버레이가 보이고 입력을 받는다. 끄면 통째로 감춘다.

        Tk 판처럼 화면을 캡처해 얼리지 않는다. 오버레이가 투명한 채로 입력을
        받을 수 있어서, 아래 화면은 계속 살아 움직인다.
        """
        if not on and self.board_mode:
            # F8/스위치로 끈 경우다. 화이트보드도 나가되 '들어가기 전 상태로
            # 복귀'는 하지 않는다. 사용자는 그리기를 끄려고 누른 것이다.
            self.set_board(False, restore=False)
            return
        if on == self.input_mode:
            return
        if on:
            self.overlay.setCursor(Qt.CrossCursor)
            self.overlay.show()
            self.overlay.raise_()
            self.overlay.activateWindow()
            self.overlay.setFocus()
            self.overlay.rebuild()
            self.keep_bar_visible()
        else:
            self._clear_hint()
            self.overlay.hide()            # 필기는 남기고 화면에서만 감춘다
        self.input_mode = on
        self.bar.switch_set(on)
        self.bar.raise_()
        self.sync_esc()
        self.bar.update()
        log("input=%s board=%s screen=%d board_strokes=%d"
            % (on, self.board_mode, len(self.screen_surf.strokes),
               len(self.board_surf.strokes)))

    def toggle_board(self):
        self.set_board(not self.board_mode)

    def set_board(self, on, restore=True):
        """restore=True 면 화이트보드에서 나올 때 들어가기 전 그리기 상태로 돌아간다.
        화이트보드 버튼과 Ctrl+0 이 이 경로다."""
        if on == self.board_mode:
            return
        self.overlay._zoom_timer.stop()
        if on:
            self._draw_before_board = self.input_mode   # 나올 때 되돌리려고 기억
            self.overlay.remember_view(self.screen_surf)
            self.board_mode = True
            self.surf = self.board_surf
            self.overlay.setScene(self.overlay.board_scene)
            self.overlay.align_view(self.board_surf)
            self.set_input(True)
            self.overlay.rebuild()
        else:
            self.overlay.remember_view(self.board_surf)
            self.board_mode = False
            self.hand_mode = False
            self.surf = self.screen_surf
            self.overlay.setScene(self.overlay.screen_scene)
            self.overlay.align_view(self.screen_surf)
            # 화이트보드에 들어가기 전이 그리기 상태였다면 그대로 돌려놓는다
            back = restore and getattr(self, "_draw_before_board", False)
            self.input_mode = True          # set_input 의 조기 반환을 피한다
            self.set_input(False)
            if back:
                self.set_input(True)
        self.bar.refresh_glass()
        self.bar.update()
        log("board=%s" % on)

    def reset_all(self):
        """Esc: 전부 해제하고 화면 필기를 지운다. 화이트보드 판서는 남긴다."""
        if self.board_mode:
            self.set_board(False, restore=False)
        else:
            self.set_input(False)
        self.surf = self.screen_surf
        if self.screen_surf.strokes:
            self.screen_surf.strokes = []
            self.screen_surf.items = []
            self.screen_surf.push()
        self.sync_esc()
        self.bar.update()
        log("reset")

    def clear_all(self):
        if self.surf.strokes:
            self.surf.strokes = []
            self.surf.items = []
            self.surf.push()
            self.overlay.rebuild()
            self.sync_esc()

    def undo(self):
        if self.surf.undo():
            self.overlay.rebuild()
            self.sync_esc()

    def redo(self):
        if self.surf.redo():
            self.overlay.rebuild()
            self.sync_esc()

    # ------------------------------------------------------------ 툴바 클릭

    # 그리기가 꺼져 있을 때 눌러도 되는 것들
    OFF_ALLOWED = {"memo", "switch", "close"}

    def enabled(self, z):
        """그리기 off 상태에서는 메모/스위치/종료만 살아 있다."""
        if self.input_mode:
            return True
        return z["id"] in self.OFF_ALLOWED

    def on_bar_click(self, z):
        if not self.enabled(z):
            return
        k, i = z["kind"], z["id"]
        if k == "tool":
            self.set_tool(i)
        elif k == "color":
            self.set_color(i)
        elif k == "switch":
            self.set_input(not self.input_mode)
        elif k == "close":
            self.quit()
        elif k == "action":
            {"board": self.toggle_board, "clear": self.clear_all,
             "memo": self.toggle_memo, "save": self.save_png}[i]()

    def toggle_memo(self):
        if self.memo is not None and self.memo.isVisible():
            self.memo.save()
            self.memo.hide()
        else:
            if self.memo is None:
                self.memo = Memo(self)
                self.memo.apply_pin()
                self.memo.place_default()
            self.memo.show()
            self.memo.raise_()
        self.bar.update()

    # ------------------------------------------------------------ 저장

    def save_png(self):
        hwnd = int(self.overlay.winId())
        wb.exclude_from_capture(hwnd, False)
        QApplication.processEvents()
        time.sleep(0.06)
        img = wb.grab_rect(VX, VY, VW, VH)
        wb.exclude_from_capture(hwnd, True)
        d = os.path.join(DATA_DIR, "captures")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(
            d, "screenpen_%s.png"
            % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        img.save(path)
        log("saved " + path)

    def _glass_tick(self):
        if not self.board_mode and not self.input_mode:
            self.bar.refresh_glass()
        self.keep_bar_visible()

    def keep_bar_visible(self):
        """툴바는 화이트보드든 아니든 항상 보여야 한다.

        오버레이도 topmost 라 WS_EX_TOPMOST 만으로는 누가 앞인지 알 수 없다.
        실제 Z순서를 확인해서, 툴바가 뒤에 있을 때만 올린다. 무조건 올리면
        서로 밀어내며 깜빡인다.
        """
        try:
            if not self.bar.isVisible():
                self.bar.show()
            bar_h = int(self.bar.winId())
            if self.overlay.isVisible():
                ov_h = int(self.overlay.winId())
                if wb.first_in_z_order((bar_h, ov_h)) != bar_h:
                    self.bar.raise_()
            elif not wb.is_topmost(bar_h):
                self.bar.raise_()
        except Exception:
            log("keep_bar " + traceback.format_exc())

    # ------------------------------------------------------------ 전역 단축키

    def _register_hotkeys(self):
        self.hotkeys = {
            1: (wb.MOD_NOREPEAT, wb.VK_F8, "F8",
                lambda: self.set_input(not self.input_mode)),
            2: (wb.MOD_NOREPEAT, wb.VK_F9, "F9", self.clear_all),
            3: (wb.MOD_NOREPEAT | wb.MOD_CONTROL | wb.MOD_ALT, wb.VK_S,
                "Ctrl+Alt+S", self.save_png),
            4: (wb.MOD_NOREPEAT | wb.MOD_CONTROL | wb.MOD_ALT, wb.VK_Q,
                "Ctrl+Alt+Q", self.quit),
            5: (wb.MOD_NOREPEAT | wb.MOD_CONTROL, wb.VK_0, "Ctrl+0",
                self.toggle_board),
            wb.ESC_ID: (wb.MOD_NOREPEAT, wb.VK_ESC, "Esc", self.reset_all),
        }
        specs = dict((h, v[:3]) for h, v in self.hotkeys.items()
                     if h != wb.ESC_ID)
        self.hk = wb.HotkeyThread(specs)
        self.hk.start()
        self.hk.ready.wait(timeout=3.0)
        self.hotkey_failed = list(self.hk.failed)
        if self.hotkey_failed:
            log("전역 단축키 등록 실패: %s" % ", ".join(self.hotkey_failed))

    def sync_esc(self):
        need = bool(self.input_mode or self.board_mode or
                    self.screen_surf.strokes)
        if need == self._esc_on:
            return
        self._esc_on = need
        self.hk.set_esc(need)

    def _pump_hotkeys(self):
        """이 루프가 끊기면 전역 단축키가 통째로 죽는다. 무슨 일이 있어도 산다."""
        try:
            while True:
                try:
                    hid = self.hk.events.get_nowait()
                except queue.Empty:
                    break
                entry = self.hotkeys.get(hid)
                if entry:
                    if not self.input_mode and hid not in (1, 4):
                        continue        # 그리기 off 에서는 F8 / 종료만
                    try:
                        entry[3]()
                    except Exception:
                        log("hotkey %s\n%s" % (entry[2], traceback.format_exc()))
        except Exception:
            log("pump\n" + traceback.format_exc())

    def quit(self):
        try:
            if self.memo is not None:
                self.memo.save()
            self.hk.stop()
        except Exception:
            pass
        QApplication.quit()


def main():
    handle = wb.claim_single_instance(MUTEX_NAME)
    if handle is None:
        log("이미 실행 중이라 종료")
        return 1
    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)          # 투명 오버레이에 필요하다
    # OpenGL 뷰포트에서는 QPainter 의 Antialiasing 힌트가 멀티샘플링으로 간다.
    # 여기서 샘플 수를 요청하지 않으면 화면에 그려지는 획이 계단 그대로 남는다.
    fmt.setSamples(GL_SAMPLES)
    QSurfaceFormat.setDefaultFormat(fmt)
    qapp = QApplication(sys.argv)
    app = ScreenPenQt()

    def keys(ev):
        m, k = ev.modifiers(), ev.key()
        if m & Qt.ControlModifier:
            for i, (tid, _, _) in enumerate(TOOLS, start=1):
                if k == getattr(Qt, "Key_%d" % i):
                    app.set_tool(tid)
                    return
            if k == Qt.Key_Z:
                app.undo()
            elif k == Qt.Key_Y:
                app.redo()
            elif k == Qt.Key_0:
                app.toggle_board()
        elif m & Qt.AltModifier:
            for i, c in enumerate(COLORS, start=1):
                if k == getattr(Qt, "Key_%d" % i):
                    app.set_color(c)
                    return
        elif k in (Qt.Key_H,):
            app.toggle_hand()
        elif k == Qt.Key_Escape:
            app.reset_all()

    app.overlay.keyPressEvent = keys
    try:
        code = qapp.exec()
    finally:
        wb.release_single_instance(handle)
    return code


if __name__ == "__main__":
    sys.exit(main())
