"""QPainter 绘制：统一服务卡片骨架、同心双环、额度条、余额 hero 与静态层缓存。

三服务共用同一卡片骨架（chrome/标题/主显示/主说明/状态行/两行明细），
只替换各区域内容。静态层（chrome/标题/说明/明细行/余额大字）按数据版本
缓存为 QPixmap；主环、环心数字与倒计时文字逐帧动态绘制，不进缓存。
"""

import json

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap

from .layout import (
    CARD_TIMER_RING_WIDTH,
    DETAIL1_Y,
    DETAIL2_Y,
    HERO_CY,
    HERO_LABEL_Y,
    SERVICE_KEYS,
    STATUS_Y,
    TITLE_Y,
    panel_card_rects,
    panel_size,
)

COLOR_BG = QColor(20, 22, 27, 215)
COLOR_CARD = QColor(255, 255, 255, 10)
COLOR_TRACK = QColor(255, 255, 255, 32)
COLOR_TEXT = QColor(235, 236, 240)
COLOR_SUBTLE = QColor(158, 164, 174)
COLOR_GREEN = QColor(74, 200, 126)
COLOR_YELLOW = QColor(240, 181, 60)
COLOR_RED = QColor(237, 90, 82)
COLOR_GRAY = QColor(148, 153, 162)
COLOR_TIMER = QColor(200, 205, 215, 170)  # 倒计时外环中性色，不占绿黄红语义

CURRENCY_SYMBOLS = {"CNY": "¥", "USD": "$", "EUR": "€"}

CARD_FONT_CACHE = {}
CENTER_FONT_CACHE = {}


def currency_symbol(code):
    return CURRENCY_SYMBOLS.get((code or "").upper(), (code or "").upper())


def color_for_remaining(remaining):
    if remaining is None:
        return COLOR_GRAY
    if remaining > 30:
        return COLOR_GREEN
    if remaining >= 10:
        return COLOR_YELLOW
    return COLOR_RED


def _card_font(pt, bold=False):
    """卡片字体（按 字号+粗细 缓存，避免每帧重建 QFontMetrics）。"""
    key = (round(float(pt), 1), bold)
    f = CARD_FONT_CACHE.get(key)
    if f is None:
        f = QFont()
        f.setPointSizeF(float(pt))
        f.setBold(bold)
        CARD_FONT_CACHE[key] = f
    return f


def center_font(text, hole, base_pt=10.0):
    """环心数字自适应字号（按 文本+孔径档位+基准字号 缓存）：
    宽度超过孔径就按 0.5pt 步进缩小（下限 6pt），避免宽文本压到同色弧上。"""
    key = (text, int(hole), round(base_pt, 1))
    f = CENTER_FONT_CACHE.get(key)
    if f is not None:
        return f
    font = QFont()
    font.setPointSizeF(base_pt)
    font.setBold(True)
    fm = QFontMetricsF(font)
    size = base_pt
    while size > 6.0 and fm.horizontalAdvance(text) > hole:
        size = max(6.0, size - 0.5)
        font.setPointSizeF(size)
        fm = QFontMetricsF(font)
    CENTER_FONT_CACHE[key] = font
    return font


def draw_card_chrome(p, rect):
    """卡片背景与边框（三服务共用）。"""
    p.setPen(QPen(QColor(255, 255, 255, 14)))
    p.setBrush(COLOR_CARD)
    p.drawRoundedRect(QRectF(rect.x() + 0.5, rect.y() + 0.5,
                             rect.width() - 1.0, rect.height() - 1.0), 10, 10)


def draw_title(p, rect, title):
    """标题行：服务名（居中，三卡统一）。"""
    p.setPen(QPen(COLOR_TEXT))
    p.setFont(_card_font(9, bold=True))
    p.drawText(QRectF(rect.x(), rect.y() + TITLE_Y, rect.width(), 14),
               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, title)


def draw_double_ring(p, cx, cy, r_inner, r_outer, w_inner, remaining,
                     timer_ratio, color, outer_opacity):
    """同心双环：内环=剩余额度（弧长恒定，阈值色），外环=倒计时（中性色，
    透明度=outer_opacity 淡入）。timer_ratio 为距重置的剩余比例
    （None=只画轨道，不伪装成有效倒计时）。"""
    rect_i = QRectF(cx - r_inner, cy - r_inner, 2 * r_inner, 2 * r_inner)
    rect_o = QRectF(cx - r_outer, cy - r_outer, 2 * r_outer, 2 * r_outer)

    if outer_opacity > 0.004:
        p.save()
        p.setOpacity(p.opacity() * outer_opacity)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(COLOR_TRACK, CARD_TIMER_RING_WIDTH,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawEllipse(rect_o)
        if timer_ratio is not None:
            span = int(360 * 16 * max(0.0, min(1.0, timer_ratio)))
            if span > 0:
                p.setPen(QPen(COLOR_TIMER, CARD_TIMER_RING_WIDTH,
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawArc(rect_o, 90 * 16, -span)
        p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(COLOR_TRACK, w_inner, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
    p.drawEllipse(rect_i)
    if remaining is not None:
        arc = int(360 * 16 * max(0.0, min(100.0, remaining)) / 100.0)
        if arc > 0:
            p.setPen(QPen(color, w_inner, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(rect_i, 90 * 16, -arc)

    text = f"{remaining:.0f}%" if remaining is not None else "—"
    hole = 2 * (r_inner - w_inner / 2.0) - 4
    p.setFont(center_font(text, hole, 9.0))
    p.setPen(QPen(color if remaining is not None else COLOR_GRAY))
    p.drawText(rect_i, Qt.AlignmentFlag.AlignCenter, text)


def draw_balance_hero(p, cx, cy, w, amount):
    """DeepSeek 余额大字：按区域宽度自适应字号。"""
    font = QFont()
    font.setPointSizeF(16.0)
    font.setBold(True)
    fm = QFontMetricsF(font)
    max_w = w - 20
    while fm.horizontalAdvance(amount) > max_w and font.pointSizeF() > 10:
        font.setPointSizeF(font.pointSizeF() - 0.5)
        fm = QFontMetricsF(font)
    p.setFont(font)
    p.setPen(QPen(COLOR_TEXT))
    p.drawText(QRectF(cx - w / 2, cy - 16, w, 32),
               Qt.AlignmentFlag.AlignCenter, amount)


def draw_quota_row(p, x, y, w, label, value, bar_remaining):
    """明细行：label（左）+ value（右）+ 可选进度条。"""
    p.setPen(QPen(COLOR_SUBTLE))
    p.setFont(_card_font(8))
    p.drawText(QRectF(x, y, w * 0.5, 12),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
    if value:
        p.drawText(QRectF(x, y, w, 12),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, value)
    if bar_remaining is not None:
        track_y = y + 13
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(COLOR_TRACK)
        p.drawRoundedRect(QRectF(x, track_y, w, 5), 2.5, 2.5)
        fill_w = w * max(0.0, min(100.0, bar_remaining)) / 100.0
        if fill_w > 0:
            p.setBrush(color_for_remaining(bar_remaining))
            p.drawRoundedRect(QRectF(x, track_y, fill_w, 5), 2.5, 2.5)


def draw_service_card(p, rect, spec):
    """统一服务卡片骨架（静态层）：chrome + 标题/状态点 + 主显示 + 主说明
    + 主状态行（仅余额类静态）+ 两行明细。

    spec 字段由 widget._card_specs 构建（纯文本/数值，见该方法的文档）。
    主环与环心数字、倒计时文字为动态层，不在此绘制。
    """
    draw_card_chrome(p, rect)
    draw_title(p, rect, spec.get("title") or "")

    hero_cx = rect.x() + rect.width() / 2
    hero_cy = rect.y() + HERO_CY
    hero_label = spec.get("hero_label") or ""
    if spec.get("hero_kind") == "balance":
        draw_balance_hero(p, hero_cx, hero_cy, rect.width(),
                          spec.get("hero_amount") or "—")

    p.setPen(QPen(COLOR_SUBTLE))
    p.setFont(_card_font(8))
    p.drawText(QRectF(rect.x() + 10, rect.y() + HERO_LABEL_Y, rect.width() - 20, 12),
               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, hero_label)

    # 主状态行：余额类仅在不可用时给红色提示（可用时不加装饰）；
    # 环形类该位置是 5h 倒计时（动态层）
    if (spec.get("hero_kind") == "balance"
            and spec.get("status_text") == "不可用"):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(spec.get("status_dot_color") or "#94a1a2"))
        p.drawEllipse(QRectF(hero_cx - 26, rect.y() + STATUS_Y + 2, 7, 7))
        p.setPen(QPen(QColor(spec.get("status_text_color") or "#94a1a2")))
        p.setFont(_card_font(9))
        p.drawText(QRectF(hero_cx - 14, rect.y() + STATUS_Y, 80, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   spec.get("status_text"))

    for y, (label, value, bar) in zip((DETAIL1_Y, DETAIL2_Y),
                                      spec.get("details") or ()):
        draw_quota_row(p, rect.x() + 14, rect.y() + y, rect.width() - 28,
                       label, value, bar)


class RenderCache:
    """静态层缓存：按（方向组合、DPR、数据签名）缓存面板静态内容 Pixmap。

    缓存内容：卡片 chrome、标题与状态点、主说明、余额大字、明细行与进度条。
    不缓存：主环与环心数字、倒计时文字（逐帧/逐秒变化，由 widget 动态绘制）。
    """

    def __init__(self):
        self._pixmaps = {}
        self._signature = None

    def ensure(self, orientations, dpr, cards_data):
        sig = (tuple(orientations), round(dpr, 3),
               json.dumps(cards_data, sort_keys=True, default=str))
        if self._signature == sig:
            return
        self._pixmaps = {}
        for orientation in orientations:
            w, h = panel_size(orientation)
            pm = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            for rect, name in zip(panel_card_rects(orientation), SERVICE_KEYS):
                draw_service_card(p, rect, cards_data.get(name) or {})
            p.end()
            self._pixmaps[orientation] = pm
        self._signature = sig

    def pixmap(self, orientation):
        return self._pixmaps.get(orientation)

    def invalidate(self):
        self._signature = None
