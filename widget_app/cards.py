"""Paint service cards from a common title, hero, status and detail grid."""

from functools import lru_cache
import re

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPen

from .layout import DETAIL_ROWS, HERO_CY, HERO_LABEL_Y, RING_STATUS_Y, TITLE_Y
from .quota import format_countdown_dh, format_countdown_hm

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
CARD_TIMER_RING_WIDTH = 4.0


CARD_FONT_CACHE = {}
CENTER_FONT_CACHE = {}


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


@lru_cache(maxsize=256)
def hero_font_size(value, available, base_pt):
    """缓存最近的文本主指标字号，按 0.5pt 缩小到 10pt 下限。"""
    size = base_pt
    while size > 10 and QFontMetricsF(_card_font(size, True)).horizontalAdvance(value) > available:
        size -= 0.5
    return size


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



def _text(p, rect, value, size=8, bold=False, color=COLOR_SUBTLE, alignment=Qt.AlignmentFlag.AlignHCenter):
    p.setFont(_card_font(size, bold))
    p.setPen(QPen(color))
    text = p.fontMetrics().elidedText(str(value), Qt.TextElideMode.ElideRight, max(0, int(rect.width())))
    p.drawText(rect, alignment | Qt.AlignmentFlag.AlignVCenter, text)


def _line(p, rect, y, text, **style):
    _text(p, QRectF(rect.x() + 10, rect.y() + y, rect.width() - 20, 15), text, **style)


def _chrome(p, rect):
    p.setPen(QPen(QColor(255, 255, 255, 16)))
    p.setBrush(COLOR_CARD)
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)


def metric_text(metric, now):
    if metric.remaining is not None:
        return f"{metric.remaining:.0f}%"
    if metric.reset is not None:
        return format_countdown_dh(metric.reset, now)
    return metric.value or "—"


def service_status(service):
    if service.error:
        return "数据陈旧" if service.hero else "读取失败"
    if service.unavailable:
        return "不可用"
    if service.stale:
        return "数据陈旧"
    if service.hero is None:
        return "暂无可显示数据"
    return ""


def _hero(p, rect, metric, center_y, now, ring_color, mini=False):
    if metric and metric.remaining is not None:
        ratio = None
        if metric.reset is not None and metric.window_seconds:
            ratio = (metric.reset - now).total_seconds() / metric.window_seconds
        draw_double_ring(p, rect.center().x(), rect.y() + center_y,
                         16.0 if mini else 18.0, 23.0, 5.0, metric.remaining,
                         ratio, ring_color, 1.0 if mini and ratio is not None else 0.0)
    else:
        value = metric_text(metric, now) if metric else "—"
        available = rect.width() - 16
        size = hero_font_size(value, available, 15.0 if mini else 18.0)
        _text(p, QRectF(rect.x() + 8, rect.y() + center_y - 18, available, 36),
              value, size=size, bold=True, color=COLOR_TEXT)


def _detail(p, rect, y, metric, now):
    x, width = rect.x() + 12, rect.width() - 24
    value = metric_text(metric, now)
    p.setFont(_card_font(8))
    value_width = min(width * 0.56, p.fontMetrics().horizontalAdvance(value) + 3)
    _text(p, QRectF(x, rect.y() + y, width - value_width - 6, 13), metric.label,
          alignment=Qt.AlignmentFlag.AlignLeft)
    _text(p, QRectF(x + width - value_width, rect.y() + y, value_width, 13), value,
          alignment=Qt.AlignmentFlag.AlignRight, color=COLOR_TEXT)
    if metric.remaining is not None:
        track = QRectF(x, rect.y() + y + 14, width, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(COLOR_TRACK)
        p.drawRoundedRect(track, 2, 2)
        if metric.remaining > 0:
            p.setBrush(color_for_remaining(metric.remaining))
            fill = QRectF(track)
            fill.setWidth(track.width() * metric.remaining / 100)
            p.drawRoundedRect(fill, 2, 2)
        if metric.reset is not None:
            _text(p, QRectF(x, rect.y() + y + 20, width, 13),
                  format_countdown_dh(metric.reset, now), size=7.5)


def mini_caption(hero, status, label):
    """迷你卡底部一行：环卡放到外环下方（外环底边≈82），文本主指标留在文字下。"""
    return (status or label), (82 if hero and hero.remaining is not None else 77)


def draw_service(p, rect, service, now, ring_color, mini=False, offline=False):
    _chrome(p, rect.adjusted(3, 3, -3, -3) if mini else rect)
    # 迷你卡上服务名只是定位提示，用次级色弱化；展开面板仍是主色。
    _line(p, rect, TITLE_Y, service.name, size=9, bold=True,
          color=COLOR_SUBTLE if mini else COLOR_TEXT)
    status = "数据陈旧" if offline else service_status(service)
    hero = service.hero
    five_hour_ring = bool(hero and hero.remaining is not None and (
        hero.window_seconds == 18000 or re.match(r"^\s*5\s*(?:小时|h(?:ours?|rs?)?(?![a-z]))", hero.label, re.I)
    ))
    label = hero.label if hero and not five_hour_ring else ""
    if mini:
        _hero(p, rect, hero, 57, now, ring_color, mini=True)
        caption, caption_y = mini_caption(hero, status, label)
        if caption:
            _text(p, QRectF(rect.x() + 10, rect.y() + caption_y, rect.width() - 20, 11),
                  caption, size=7)
        return
    _hero(p, rect, hero, HERO_CY, now, ring_color)
    if hero and hero.remaining is not None and hero.reset is not None and not status:
        formatter = format_countdown_hm if five_hour_ring else format_countdown_dh
        status = formatter(hero.reset, now)
    if label:
        if status:
            inner = QRectF(rect.x() + 10, rect.y() + HERO_LABEL_Y, rect.width() - 20, 15)
            half = inner.width() / 2
            _text(p, QRectF(inner.x(), inner.y(), half, inner.height()), label,
                  alignment=Qt.AlignmentFlag.AlignLeft)
            _text(p, QRectF(inner.x() + half, inner.y(), half, inner.height()), status,
                  size=7.5, alignment=Qt.AlignmentFlag.AlignRight)
            status = ""
        else:
            _line(p, rect, HERO_LABEL_Y, label)
    if status:
        _line(p, rect, RING_STATUS_Y, status)
    for y, metric in zip(DETAIL_ROWS, service.details):
        _detail(p, rect, y, metric, now)


def draw_empty(p, rect, text):
    _chrome(p, rect.adjusted(3, 3, -3, -3))
    _line(p, rect, rect.height() / 2 - 8, text, size=9, bold=True, color=COLOR_TEXT)
