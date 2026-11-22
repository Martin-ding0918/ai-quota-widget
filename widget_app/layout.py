"""纯几何：服务卡片布局、形变状态与插值。

不依赖 QPainter、不访问数据、不做网络——只回答"东西放在哪里、
两个矩形怎么插值"。绘制见 cards.py，状态与事件见 widget.py。
"""

from dataclasses import dataclass, field

from PySide6.QtCore import QRect

# 展开面板：三卡横排（普通悬浮 / 上、下贴边）与三卡竖排（左、右贴边）
PANEL_H_W, PANEL_H_H = 512, 196
PANEL_V_W, PANEL_V_H = 176, 572
# 迷你卡：双单元（上、下贴边横排 / 左、右贴边竖排），单元 96×96
CARD_CELL_SIZE = 96
MINI_H_W, MINI_H_H = 2 * CARD_CELL_SIZE, CARD_CELL_SIZE
MINI_V_W, MINI_V_H = CARD_CELL_SIZE, 2 * CARD_CELL_SIZE

# 服务卡片内部布局（card-local，160×176，三服务共用同一骨架）
CARD_W, CARD_H = 160, 176
TITLE_Y = 5              # 标题行 y（高 13）
HERO_CY = 50.0           # 主显示区中心 y（圆环圆心 / 余额大字中心）
HERO_INNER_R = 18.0      # 面板 hero 内环（额度）半径
HERO_OUTER_R = 24.0      # 面板外环（倒计时）半径，随收起淡入
HERO_RING_W = 5.0        # 面板主环线宽
CARD_QUOTA_RING_RADIUS = 16.0       # 迷你卡内环（额度）半径
CARD_RING_RADIUS = 23.0             # 迷你卡外环（倒计时）半径
CARD_QUOTA_RING_WIDTH = 5.0         # 迷你卡内环（额度）线宽
CARD_TIMER_RING_WIDTH = 4.0         # 迷你卡外环线宽
HERO_LABEL_Y = 72        # 主说明（5小时 / 余额）
STATUS_Y = 86            # 主状态行（5h 裸倒计时 / ● 可用）
DETAIL1_Y = 100          # 明细一 label/value 行
DETAIL_BAR_DY = 12       # 进度条相对明细行的纵向偏移
COUNTDOWN1_Y = 124       # 明细一倒计时（OpenCode 每周；Kimi 此行留空）
DETAIL2_Y = 138          # 明细二 label/value 行
COUNTDOWN2_Y = 160       # 明细二倒计时（Kimi 共用月重置 / OpenCode 每月）

VERTICAL_EDGES = ("left", "right")

SERVICE_KEYS = ("kimi", "opencode", "deepseek")
SERVICE_TITLES = {"kimi": "Kimi Code", "opencode": "OpenCode", "deepseek": "DeepSeek"}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_rect(r1, r2, t):
    return QRect(
        int(round(_lerp(r1.left(), r2.left(), t))),
        int(round(_lerp(r1.top(), r2.top(), t))),
        int(round(_lerp(r1.width(), r2.width(), t))),
        int(round(_lerp(r1.height(), r2.height(), t))),
    )


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = _clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def panel_card_rects(orientation: str):
    """展开面板中三张服务卡片的矩形，顺序 (kimi, opencode, deepseek)。"""
    if orientation == "vertical":
        return tuple(QRect(8, 8 + i * (CARD_H + 8), CARD_W, CARD_H) for i in range(3))
    return tuple(QRect(16 + i * (CARD_W + 8), 10, CARD_W, CARD_H) for i in range(3))


def mini_cell_rects(orientation: str):
    """迷你卡中两个服务单元的矩形，顺序 (kimi, opencode)。"""
    s = CARD_CELL_SIZE
    if orientation == "vertical":
        return (QRect(0, 0, s, s), QRect(0, s, s, s))
    return (QRect(0, 0, s, s), QRect(s, 0, s, s))


def panel_size(orientation: str):
    return (PANEL_V_W, PANEL_V_H) if orientation == "vertical" else (PANEL_H_W, PANEL_H_H)


def mini_size(orientation: str):
    return (MINI_V_W, MINI_V_H) if orientation == "vertical" else (MINI_H_W, MINI_H_H)


RING_SERVICES = ("kimi", "opencode")   # 双环只属于这两家；DeepSeek 是余额 hero
MINI_TITLE_BAND = 19                   # 迷你卡标题带高度（标题 y=5 + 行高 14）


def panel_ring_anchors(orientation: str):
    """面板层双环锚点（panel 坐标系）：Kimi/OpenCode 卡片的 hero 圆心。"""
    out = {}
    for name, rect in zip(RING_SERVICES, panel_card_rects(orientation)):
        out[name] = (rect.center().x(), rect.y() + HERO_CY)
    return out


def mini_ring_anchors(orientation: str):
    """迷你卡层双环锚点（mini 坐标系）：标题带以下的垂直居中点，
    与单元内标题同轴（水平居中）。"""
    out = {}
    for name, cell in zip(RING_SERVICES, mini_cell_rects(orientation)):
        cy = cell.y() + MINI_TITLE_BAND + (cell.height() - MINI_TITLE_BAND) / 2
        out[name] = (cell.center().x(), cy)
    return out


def _shape_orientation(rect):
    """按矩形宽高推断排列方向（面板与迷你卡共用同一判断）。"""
    return "vertical" if rect.width() < rect.height() else "horizontal"


@dataclass
class MorphState:
    """一次形变的方向与锚点。

    一次动画内方向固定（panel_orientation / card_orientation 不变），
    progress 为动画进度 0→1；中途反向只更新 target 与 end_* 字段，
    start_* 保持当前帧状态，几何与透明度连续。
    """

    progress: float = 0.0             # 动画进度 0→1
    start_t: float = 0.0              # 起始服务形变进度（0=面板 1=迷你卡）
    target_t: float = 0.0             # 目标服务形变进度
    start_rect: QRect = field(default_factory=QRect)
    end_rect: QRect = field(default_factory=QRect)
    panel_orientation: str = "horizontal"   # 淡出层的排列方向
    card_orientation: str = "horizontal"    # 淡入层（迷你卡）的排列方向


def interpolate_morph(state: MorphState, fraction: float):
    """返回当前帧：(窗口矩形, 圆环锚点)。

    圆环锚点为 Kimi/OpenCode 两环的 (cx, cy)——面板 hero 圆心 ⇄ 迷你卡
    单元圆心按 fraction 插值，端点与静态层锚点严格一致（无端点跳环）。
    """
    rect = _lerp_rect(state.start_rect, state.end_rect, fraction)
    p_anchors = panel_ring_anchors(state.panel_orientation)
    c_anchors = mini_ring_anchors(state.card_orientation)
    anchors = []
    for idx in range(2):
        name = RING_SERVICES[idx]
        px, py = p_anchors[name]
        cx, cy = c_anchors[name]
        anchors.append((_lerp(px, cx, fraction), _lerp(py, cy, fraction)))
    return rect, anchors


def edge_rect(edge, expanded, along, geo):
    """按屏幕可用区域定位贴边窗口（迷你卡两单元沿边排列）。均钳制在屏内。

    expanded=True：上/下边 → 512×196 横面板；左/右边 → 176×572 竖面板
    （高度超过屏幕时钳制到可用高度）。
    expanded=False：上/下边 → 192×96 横排迷你卡；左/右边 → 96×192 竖排迷你卡。
    """
    vertical = edge in VERTICAL_EDGES
    if expanded:
        w, h = (PANEL_V_W, min(PANEL_V_H, geo.height())) if vertical else (PANEL_H_W, PANEL_H_H)
    else:
        w, h = (MINI_V_W, MINI_V_H) if vertical else (MINI_H_W, MINI_H_H)
    if edge in ("top", "bottom"):
        x = _clamp(int(along), geo.left(), geo.right() - w + 1)
        y = geo.top() if edge == "top" else geo.bottom() - h + 1
        return QRect(x, y, w, h)
    y = _clamp(int(along), geo.top(), geo.bottom() - h + 1)
    x = geo.left() if edge == "left" else geo.right() - w + 1
    return QRect(x, y, w, h)
