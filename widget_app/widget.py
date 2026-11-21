"""悬浮窗 GUI：展开态（三环额度面板）+ 贴边收起态（迷你圆环卡），形变动画过渡。

展开态：无边框置顶 300×134 面板；鼠标按住拖动、位置记忆；右键菜单。
底部两处裸倒计时（无文字标签）：5h 倒计时在 5 小时环正下方（`2小时13分` 风格），
月倒计时在两个月度环下方居中（`11天3小时` 风格）。

贴边收起（v3/v5）：拖到屏幕边缘 10px 内松手（或菜单「贴边收起」）→ 窗口缩成 96×96
迷你卡贴边（不用离屏坐标，四边均钳制屏内）：
  - **同心双环**（v5，取代 v3 的单环双义）：内环弧长 = 剩余额度比例
    （阈值色/断线灰/<10% 闪烁，编码与面板主环一致），环心数字 = 剩余额度百分比
    （9pt bold）；外环弧长 = 距 5h 重置倒计时比例（中性色 COLOR_TIMER，
    收起淡入、展开淡出，面板态不可见）——两环编码各自独立，杜绝
    "弧长一会儿是额度一会儿是时间"的误导；
  - 卡片尺寸固定，每秒 tick 只重绘内容、不再重设窗口几何。
形变过渡：单一进度 self._t（0=面板 → 1=迷你卡）驱动几何与内容**同帧插值**——
标题 x 从左滑到居中、面板主环连续缩成内环（弧长编码不变）、倒计时外环淡入、
月环/标签/倒计时/状态点按 1−t 渐隐。动画可打断：中途反向先断开旧动画，
从当前进度续播，无跳变。悬停迷你卡滑出面板，光标移开 1.5s 自动收回
（右键菜单弹出期间不收回）；按住拖离边缘即退出贴边（抓取点按相对位置
等比映射到大面板，松手仍可再吸附）。贴边状态写入 config，重启恢复。

轮询（v4 双速自适应）：空闲档每 3 分钟读服务端缓存（`/api/usage`，开销极小）；
对比两次额度快照（`poll_step`），任一档剩余变化 >0.1% → 切活动档：每 1 分钟对
Kimi 单服务 `GET /api/usage/{id}?force=true` 实时回源；连续 3 次静默回落空闲。
活动态单服务请求失败（ApiError）时本周期降级回全量缓存读取、模式不变。
手动「立即刷新」在已记住服务 id 时走单服务 force 立即拿实时值。
请求在后台线程跑；网络异常保留旧数据灰显 + 红点，不清空、不弹窗。
本地 UI 每秒刷新一次（倒计时文字 / 弧长 / 闪烁相位），纯本地计算不发请求。
"""

from __future__ import annotations

import threading
import time
import webbrowser
from datetime import datetime, timezone

from PySide6.QtCore import (
    QEasingCurve,
    QRect,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QCursor, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QWidget,
)

from . import config
from .api_client import ApiError, OpenUsageClient, OpenUsageError
from .quota import (
    KimiQuota,
    countdown_ratio,
    format_countdown_dh,
    format_countdown_hm,
    parse_kimi_quota,
    poll_step,
    quota_signature,
)

POLL_IDLE_MS = 3 * 60 * 1000        # 空闲档：读服务端缓存，3 分钟一次（略快于服务端回源周期，压低发现滞后）
POLL_ACTIVE_MS = 60 * 1000          # 活动档：单服务 force 回源，1 分钟一次
UI_TICK_MS = 1000                   # 本地 UI 刷新：倒计时文字 / 弧长 / 闪烁
FRESH_MAX_AGE_MIN = 15              # 本地数据超过该分钟数视为陈旧（黄点）

WIDTH, HEIGHT = 300, 134            # 展开态面板尺寸
CARD_SIZE = 96                      # 贴边收起态迷你卡尺寸（方形）
RING_RADIUS = 22.0                  # 面板圆环半径（=迷你卡内环的起点）
CARD_RING_RADIUS = 23.0             # 迷你卡外环（倒计时）半径，固定
CARD_QUOTA_RING_RADIUS = 16.0       # 迷你卡内环（额度）半径
RING_CY = 56.0                      # 面板圆环圆心 y；迷你卡圆心 y=54
RING_THICKNESS = 6.0                # 面板圆环线宽
CARD_TIMER_RING_WIDTH = 4.0         # 迷你卡外环（倒计时）线宽
CARD_QUOTA_RING_WIDTH = 5.0         # 迷你卡内环（额度）线宽

SNAP_THRESHOLD = 10                 # 距屏幕可用边缘 ≤10px 松手即吸附
AUTO_COLLAPSE_MS = 1500             # 悬停展开后光标移开多久自动收回
ANIM_MS = 220                       # 形变动画基准时长（按剩余进度缩放）
BLINK_HALF_PERIOD_MS = 800          # 低额度闪烁半周期（亮/暗各 0.8s）

COLOR_BG = QColor(20, 22, 27, 215)
COLOR_TRACK = QColor(255, 255, 255, 32)
COLOR_TEXT = QColor(235, 236, 240)
COLOR_SUBTLE = QColor(158, 164, 174)
COLOR_GREEN = QColor(74, 200, 126)
COLOR_YELLOW = QColor(240, 181, 60)
COLOR_RED = QColor(237, 90, 82)
COLOR_GRAY = QColor(148, 153, 162)
COLOR_TIMER = QColor(200, 205, 215, 170)  # 迷你卡外环（倒计时）中性色，不占绿黄红语义

RING_LABELS = ("月总额", "月代码")
RING_BASE_CX = (150.0, 250.0)
EDGES = ("top", "bottom", "left", "right")


def color_for_remaining(remaining):
    if remaining is None:
        return COLOR_GRAY
    if remaining > 30:
        return COLOR_GREEN
    if remaining >= 10:
        return COLOR_YELLOW
    return COLOR_RED


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_rect(r1: QRect, r2: QRect, t: float) -> QRect:
    return QRect(
        int(round(_lerp(r1.left(), r2.left(), t))),
        int(round(_lerp(r1.top(), r2.top(), t))),
        int(round(_lerp(r1.width(), r2.width(), t))),
        int(round(_lerp(r1.height(), r2.height(), t))),
    )


def edge_rect(edge, expanded, along, geo):
    """贴边几何（纯函数，便于单测）。

    expanded=True → 300×134 面板贴边；False → 96×96 迷你卡贴边。
    along 是沿边坐标（水平边=左端 x，垂直边=顶端 y）；geo 为所在屏幕的可用区域。
    四种形态均钳制在屏内。
    """
    s = CARD_SIZE
    if edge in ("top", "bottom"):
        if expanded:
            x = _clamp(int(along), geo.left(), geo.right() - WIDTH + 1)
            y = geo.top() if edge == "top" else geo.bottom() - HEIGHT + 1
            return QRect(x, y, WIDTH, HEIGHT)
        x = _clamp(int(along), geo.left(), geo.right() - s + 1)
        y = geo.top() if edge == "top" else geo.bottom() - s + 1
        return QRect(x, y, s, s)
    if expanded:
        y = _clamp(int(along), geo.top(), geo.bottom() - HEIGHT + 1)
        x = geo.left() if edge == "left" else geo.right() - WIDTH + 1
        return QRect(x, y, WIDTH, HEIGHT)
    y = _clamp(int(along), geo.top(), geo.bottom() - s + 1)
    x = geo.left() if edge == "left" else geo.right() - s + 1
    return QRect(x, y, s, s)


def _first_service_error(services) -> str:
    if not isinstance(services, list):
        return ""
    for svc in services:
        if isinstance(svc, dict):
            result = svc.get("result")
            if isinstance(result, dict) and result.get("error"):
                return str(result["error"])
    return ""


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kimi 额度悬浮窗 — 设置")
        self.setMinimumWidth(360)
        form = QFormLayout(self)
        self.server_edit = QLineEdit(str(cfg.get("server_url") or ""))
        self.user_edit = QLineEdit(str(cfg.get("username") or ""))
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setPlaceholderText("留空则不修改已保存的密码")
        form.addRow("服务器地址", self.server_edit)
        form.addRow("用户名", self.user_edit)
        form.addRow("密码", self.pass_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.server_edit.text().strip(), self.user_edit.text().strip(), self.pass_edit.text()


class QuotaWidget(QWidget):
    fetched = Signal(object)    # KimiQuota
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._cfg = config.load_config()
        self._client = None
        self._quota = None
        self._last_error = ""
        self._fetch_inflight = False
        self._drag_offset = None
        self._grab_frac = None        # 按下点在窗口内的相对位置（0–1），用于拖出映射
        self._menu_open = False

        # 形变状态：_t 0=完整面板 → 1=迷你卡；动画可打断、从当前进度续播
        self._t = 0.0
        self._morph = None
        self._morph_target = None     # None 或 0.0/1.0（正在趋向的目标）
        self._morph_start_rect = None
        self._morph_start_t = 0.0
        self._morph_end_rect = None

        edge = str(self._cfg.get("snap_edge") or "")
        self._snap_edge = edge if edge in EDGES else ""
        self._blink_enabled = bool(self._cfg.get("blink_enabled", True))

        # v4 双速轮询状态
        self._active_polling = False   # False=空闲档（5min 缓存） True=活动档（1min force 单服务）
        self._quiet_polls = 0          # 活动档下连续无变化的轮询次数
        self._prev_sig = None          # 上一次三档额度快照（quota_signature）
        self._kimi_service_id = None   # OpenUsage 里 Kimi 服务的 id（首次全量后记住）

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Kimi 额度悬浮窗")
        self.setToolTip(f"Kimi Code 用量悬浮窗\n数据源：{self._cfg.get('server_url')}")
        # 不设固定尺寸：面板 / 迷你卡几何由贴边状态与形变决定

        self._restore_geometry()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_IDLE_MS)
        self._poll_timer.timeout.connect(self.refresh)
        self._poll_timer.start()

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(UI_TICK_MS)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        self._auto_collapse = QTimer(self)
        self._auto_collapse.setSingleShot(True)
        self._auto_collapse.setInterval(AUTO_COLLAPSE_MS)
        self._auto_collapse.timeout.connect(self._auto_collapse_check)

        self.fetched.connect(self._on_fetched)
        self.failed.connect(self._on_failed)
        self._rebuild_client()
        self.refresh()

    # ---- 几何与贴边 ----

    def _current_screen_geo(self):
        screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _restore_geometry(self):
        """启动时的窗口摆放：贴边状态直接以迷你卡恢复，否则恢复普通悬浮位置。"""
        if not self._snap_edge:
            self._restore_position()
            return
        geo = self._current_screen_geo()
        if geo is None:
            self._snap_edge = ""
            self._restore_position()
            return
        if self._snap_edge in ("top", "bottom"):
            along = self._cfg.get("window_x")
            if not isinstance(along, int):
                along = geo.left()
        else:
            along = self._cfg.get("window_y")
            if not isinstance(along, int):
                along = geo.top()
        self._t = 1.0
        self.setGeometry(edge_rect(self._snap_edge, False, along, geo))

    def _restore_position(self):
        geo = None
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
        x = self._cfg.get("window_x")
        y = self._cfg.get("window_y")
        if isinstance(x, int) and isinstance(y, int) and geo is not None:
            x = max(geo.left(), min(x, geo.right() - WIDTH + 1))
            y = max(geo.top(), min(y, geo.bottom() - HEIGHT + 1))
            self.setGeometry(x, y, WIDTH, HEIGHT)
        elif geo is not None:
            # 默认贴主屏右上角
            self.setGeometry(geo.right() - WIDTH - 40, geo.top() + 60, WIDTH, HEIGHT)

    def _nearest_edge(self, force=False):
        """窗口当前距哪条屏幕边最近；force=False 时仅距边 ≤10px 才返回。"""
        geo = self._current_screen_geo()
        if geo is None:
            return None
        r = self.frameGeometry()
        distances = {
            "top": r.top() - geo.top(),
            "bottom": geo.bottom() - r.bottom(),
            "left": r.left() - geo.left(),
            "right": geo.right() - r.right(),
        }
        edge, dist = min(distances.items(), key=lambda kv: kv[1])
        if force or dist <= SNAP_THRESHOLD:
            return edge
        return None

    def _target_rects(self, along=None):
        """当前贴边状态的两档目标矩形：(面板, 迷你卡)。along 缺省取自当前几何。"""
        geo = self._current_screen_geo()
        if geo is None or not self._snap_edge:
            return None, None
        if along is None:
            r = self.frameGeometry()
            along = r.left() if self._snap_edge in ("top", "bottom") else r.top()
        return (edge_rect(self._snap_edge, True, along, geo),
                edge_rect(self._snap_edge, False, along, geo))

    def _save_snap(self, panel=None, card=None):
        if panel is not None:
            self._cfg["window_x"] = panel.x()
            self._cfg["window_y"] = panel.y()
        elif card is not None:
            self._cfg["window_x"] = card.x()
            self._cfg["window_y"] = card.y()
        self._cfg["snap_edge"] = self._snap_edge
        config.save_config(self._cfg)

    # ---- 形变动画 ----

    def _morph_to(self, target, end_rect, animated=True):
        """形变到 target（0.0=面板 1.0=迷你卡）与 end_rect；可打断、续播。"""
        self._stop_morph()
        if (
            not animated
            or not self.isVisible()
            or (abs(target - self._t) < 0.005 and self.geometry() == end_rect)
        ):
            self._morph_target = None
            self._t = target
            self.setGeometry(end_rect)
            self.update()
            return
        self._morph_target = target
        self._morph_start_t = self._t
        self._morph_start_rect = QRect(self.geometry())
        self._morph_end_rect = QRect(end_rect)
        anim = QVariantAnimation(self)
        anim.setStartValue(self._morph_start_t)
        anim.setEndValue(target)
        anim.setDuration(max(60, int(ANIM_MS * abs(target - self._morph_start_t))))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(self._on_morph_tick)
        anim.finished.connect(self._on_morph_done)
        self._morph = anim
        anim.start()

    def _on_morph_tick(self, value):
        self._t = float(value)
        if (
            self._morph_target is not None
            and self._morph_target != self._morph_start_t
            and self._morph_start_rect is not None
            and self._morph_end_rect is not None
        ):
            frac = (value - self._morph_start_t) / (self._morph_target - self._morph_start_t)
            self.setGeometry(_lerp_rect(self._morph_start_rect, self._morph_end_rect, frac))
        self.update()

    def _on_morph_done(self):
        self._morph = None
        self._morph_target = None
        if self._morph_end_rect is not None:
            self.setGeometry(self._morph_end_rect)  # 终态校准，消除插值取整误差
        self.update()

    def _stop_morph(self):
        """停在当前进度（几何与 _t 都冻结），供反向续播或拖动接管。"""
        if self._morph is not None:
            anim = self._morph
            self._morph = None
            self._morph_target = None
            self._morph_end_rect = None
            anim.stop()
            anim.deleteLater()
        else:
            self._morph_end_rect = None

    def _snap(self, edge, animated=True):
        """吸附到指定边缘并收起成迷你卡；状态落盘。"""
        self._snap_edge = edge
        self._auto_collapse.stop()
        panel, card = self._target_rects()
        if panel is None:
            return
        self._save_snap(panel=panel, card=card)
        self._morph_to(1.0, card, animated=animated)

    def _unsnap(self, grab_frac=None):
        """退出贴边模式恢复普通悬浮面板；抓取点按相对位置等比映射。"""
        self._snap_edge = ""
        self._auto_collapse.stop()
        geo = self._current_screen_geo()
        if geo is not None:
            fx, fy = grab_frac if grab_frac else (0.0, 0.0)
            cursor = QCursor.pos()
            x = _clamp(int(cursor.x() - fx * WIDTH), geo.left(), geo.right() - WIDTH + 1)
            y = _clamp(int(cursor.y() - fy * HEIGHT), geo.top(), geo.bottom() - HEIGHT + 1)
            target = QRect(x, y, WIDTH, HEIGHT)
            self._cfg["window_x"] = x
            self._cfg["window_y"] = y
        else:
            target = QRect(self.x(), self.y(), WIDTH, HEIGHT)
        self._cfg["snap_edge"] = ""
        config.save_config(self._cfg)
        self._morph_to(0.0, target)

    def _expand_from_edge(self, animated=True):
        """悬停迷你卡 → 滑出完整面板（贴边展开态）。"""
        if not self._snap_edge:
            return
        self._auto_collapse.stop()
        panel, _card = self._target_rects()
        if panel is None:
            return
        self._morph_to(0.0, panel, animated=animated)

    def _collapse_to_card(self, animated=True):
        if not self._snap_edge:
            return
        _panel, card = self._target_rects()
        if card is None:
            return
        self._morph_to(1.0, card, animated=animated)

    def _auto_collapse_check(self):
        if self._menu_open:
            self._auto_collapse.start()  # 菜单弹出期间不收回，关闭后再查
            return
        if self._snap_edge and not self.underMouse() and self._t < 0.999:
            self._collapse_to_card(animated=True)

    # ---- 数据 ----

    def _rebuild_client(self):
        username = self._cfg.get("username") or ""
        password = config.get_password(username) if username else None
        self._client = OpenUsageClient(
            self._cfg.get("server_url") or "",
            username,
            password,
            token=self._cfg.get("token") or "",
        )

    def has_credentials(self) -> bool:
        username = self._cfg.get("username") or ""
        if not username:
            return False
        if self._cfg.get("token"):
            return True
        return config.get_password(username) is not None

    def refresh(self, force_service: bool = False):
        """发起一次后台轮询；结果经信号回主线程。

        force_service=True（手动「立即刷新」）且已记住 Kimi 服务 id 时，走单服务
        force 回源立即拿实时值；定时器触发的轮询按空闲/活动双速分路（见模块 docstring）。
        """
        if self._fetch_inflight or self._client is None:
            return
        self._fetch_inflight = True
        client = self._client
        use_single = (
            self._kimi_service_id is not None
            and (force_service or self._active_polling)
        )
        service_id = self._kimi_service_id

        def work():
            services = None
            try:
                if use_single:
                    try:
                        services = [client.get_service_usage(service_id, force=True)]
                    except ApiError:
                        # 单服务请求失败（如服务被删/瞬时错误）：本周期降级回全量
                        # 缓存读取，档位不变——网络抖动不在 idle/active 间来回跳。
                        services = client.get_usage()
                else:
                    services = client.get_usage()
                quota = parse_kimi_quota(services, now=datetime.now(timezone.utc))
            except OpenUsageError as exc:
                self.failed.emit(str(exc))
                return
            except Exception as exc:  # 兜底，避免后台线程静默死亡
                self.failed.emit(f"刷新异常：{exc}")
                return
            if quota is None:
                detail = _first_service_error(services)
                self.failed.emit("未获取到 Kimi Code 数据" + (f"：{detail}" if detail else ""))
                return
            self.fetched.emit(quota)

        threading.Thread(target=work, daemon=True).start()

    def _on_fetched(self, quota: KimiQuota):
        self._quota = quota
        self._last_error = ""
        self._fetch_inflight = False
        if quota.service_id is not None:
            self._kimi_service_id = quota.service_id
        # 双速轮询档位判定：首次（无基线）只建基线不触发
        sig = quota_signature(quota)
        active, quiet = poll_step(self._prev_sig, sig, self._active_polling, self._quiet_polls)
        self._prev_sig = sig
        self._quiet_polls = quiet
        if active != self._active_polling:
            self._active_polling = active
            self._poll_timer.setInterval(POLL_ACTIVE_MS if active else POLL_IDLE_MS)
            self._poll_timer.start()  # 按新节奏立即重排下一次
        token = self._client.token if self._client else ""
        if token and token != self._cfg.get("token"):
            # 401 自动重登录后 token 换新，落盘避免下次启动重复登录
            self._cfg["token"] = token
            config.save_config(self._cfg)
        self.update()

    def _on_failed(self, message: str):
        self._last_error = message
        self._fetch_inflight = False
        self.update()

    # ---- 本地每秒刷新 ----

    def _tick(self):
        if self._snap_edge and self._t > 0.99 and self._morph_target is None:
            self._update_card_tooltip()
        self.update()

    def _update_card_tooltip(self):
        quota = self._quota
        now = datetime.now(timezone.utc).astimezone()
        if quota is not None and quota.five_hour_remaining is not None:
            tip = (f"5h 剩余 {quota.five_hour_remaining:.0f}%，"
                   f"重置 {format_countdown_hm(quota.reset_5h, now)}")
        else:
            tip = "Kimi 额度悬浮窗（无数据）"
        if self.toolTip() != tip:
            self.setToolTip(tip)

    # ---- 绘制 ----

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        t = self._t
        w = self.width()

        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, self.height() - 1.0), 12.0, 12.0)
        p.fillPath(path, COLOR_BG)

        now = datetime.now(timezone.utc).astimezone()
        quota = self._quota

        # 同心双环（v5）：内环=5h 剩余额度（面板主环连续缩成它，弧长编码全程不变），
        # 外环=距重置倒计时（中性色，随收起淡入，面板态不可见）——弧长不再混用两种语义
        cx = _lerp(50.0, w / 2.0, t)
        cy = _lerp(RING_CY, 54.0, t)
        self._draw_double_ring(p, cx, cy, t, quota)

        # 面板专属元素：按 1−t 渐隐（月环收拢、倒计时、状态点/时间、5小时标签）
        if t < 0.995:
            p.setOpacity((1.0 - t) * (0.45 if self._last_error else 1.0))

            p.setPen(QPen(COLOR_SUBTLE))
            p.setFont(self._font(9))
            p.drawText(
                QRectF(w - 132, 8, 74, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                self._time_text(),
            )
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._status_color())
            p.drawEllipse(QRectF(w - 46, 11.5, 7, 7))

            values = (
                quota.month_total_remaining if quota is not None else None,
                quota.month_code_remaining if quota is not None else None,
            )
            for base_cx, remaining_m, label in zip(RING_BASE_CX, values, RING_LABELS):
                mcx = _lerp(base_cx, w / 2.0, t)
                self._draw_ring(p, mcx, remaining_m, label)

            p.setPen(QPen(COLOR_SUBTLE))
            p.setFont(self._font(9))
            reset_5h = quota.reset_5h if quota is not None else None
            reset_month = quota.reset_month if quota is not None else None
            p.drawText(
                QRectF(cx - 45, 98, 90, 15),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                format_countdown_hm(reset_5h, now),
            )
            mcx_cd = _lerp(200.0, w / 2.0, t)
            p.drawText(
                QRectF(mcx_cd - 45, 98, 90, 15),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                format_countdown_dh(reset_month, now),
            )
            p.drawText(
                QRectF(cx - 45, RING_CY + RING_RADIUS + 4.0, 90, 14),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                "5小时",
            )
            p.setOpacity(1.0)

        # 标题：永不渐隐，x 从左侧 14 连续滑到随窗宽居中
        title_x = _lerp(14.0, (w - 60.0) / 2.0, t)
        p.setPen(QPen(COLOR_SUBTLE))
        p.setFont(self._font(9))
        p.drawText(
            QRectF(title_x, 8, 70, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "Kimi Code",
        )

    def _draw_ring(self, p: QPainter, cx, remaining, label, *, cy=RING_CY, r=RING_RADIUS,
                   span_override=None, color=None, width=RING_THICKNESS,
                   center_text=True, center_pt=10):
        """单环：轨道 + 弧 + 可选环心数字/下方标签。

        弧长取 span_override（如倒计时比例），缺省用 remaining（额度百分比）。
        """
        ring_color = color if color is not None else color_for_remaining(remaining)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(COLOR_TRACK, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawEllipse(rect)
        span = span_override if span_override is not None else remaining
        if span is not None:
            arc = int(360 * 16 * max(0.0, min(100.0, span)) / 100.0)
            if arc > 0:
                p.setPen(QPen(ring_color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawArc(rect, 90 * 16, -arc)

        if center_text:
            p.setPen(QPen(ring_color if remaining is not None else COLOR_GRAY))
            p.setFont(self._font(center_pt, bold=True))
            text = f"{remaining:.0f}%" if remaining is not None else "—"
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        if label:
            p.setPen(QPen(COLOR_SUBTLE))
            p.setFont(self._font(8))
            p.drawText(QRectF(cx - 45, cy + r + 4.0, 90, 14), Qt.AlignmentFlag.AlignCenter, label)

    def _draw_double_ring(self, p: QPainter, cx, cy, t, quota):
        """迷你卡同心双环：内环=5h 剩余额度（阈值色/断线灰/<10% 闪烁），
        外环=距重置倒计时（中性色 COLOR_TIMER）。

        外环透明度 = t：收起过程淡入、展开淡出，面板态完全不可见；两环弧长编码
        各自独立（额度 / 时间），不互相插值。断线时内环变灰，外环维持中性色
        （无数据满环兜底沿用 _countdown_span_pct）。
        """
        remaining = quota.five_hour_remaining if quota is not None else None

        if t > 0.004:  # 外环（倒计时）：随收起淡入
            p.save()
            p.setOpacity(t)
            self._draw_ring(
                p, cx, None, None,
                cy=cy, r=CARD_RING_RADIUS, span_override=self._countdown_span_pct(),
                color=COLOR_TIMER, width=CARD_TIMER_RING_WIDTH, center_text=False,
            )
            p.restore()

        # 内环（额度）：面板主环连续缩成它，编码全程不变
        r = _lerp(RING_RADIUS, CARD_QUOTA_RING_RADIUS, t)
        self._draw_ring(
            p, cx, remaining, None,
            cy=cy, r=r, color=self._five_hour_color(),
            width=_lerp(RING_THICKNESS, CARD_QUOTA_RING_WIDTH, t),
            center_pt=9,
        )

    def _countdown_span_pct(self) -> float:
        """迷你卡弧长（0–100）：距 5h 重置的剩余时间比例；无数据按满环兜底。"""
        quota = self._quota
        if quota is None or quota.reset_5h is None:
            return 100.0
        ratio = countdown_ratio(quota.reset_5h, datetime.now(timezone.utc))
        return 100.0 if ratio is None else ratio * 100.0

    def _five_hour_color(self):
        """主环颜色：阈值色；断线/无数据灰；<10% 且开启提醒时闪烁（降透明度）。"""
        quota = self._quota
        if self._last_error or quota is None or quota.five_hour_remaining is None:
            return QColor(COLOR_GRAY)
        color = QColor(color_for_remaining(quota.five_hour_remaining))
        if (
            self._blink_enabled
            and quota.five_hour_remaining < 10
            and int(time.monotonic() * 1000 / BLINK_HALF_PERIOD_MS) % 2 == 1
        ):
            color.setAlpha(70)
        return color

    def _status_color(self):
        if self._last_error:
            return COLOR_RED
        if self._quota is None:
            return COLOR_YELLOW
        age = (datetime.now(timezone.utc) - self._quota.fetched_at).total_seconds()
        if self._quota.stale or age > FRESH_MAX_AGE_MIN * 60:
            return COLOR_YELLOW
        return COLOR_GREEN

    def _time_text(self) -> str:
        if self._quota is None:
            return "--:--"
        return self._quota.fetched_at.astimezone().strftime("%H:%M")

    def _font(self, pt: int, bold: bool = False) -> QFont:
        f = QFont()
        f.setPointSize(pt)
        f.setBold(bold)
        return f

    # ---- 交互 ----

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._stop_morph()  # 抓取即冻结当前形变，拖动期间不再动画
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            size = self.frameGeometry().size()
            self._grab_frac = (
                (event.position().x() / size.width()) if size.width() else 0.0,
                (event.position().y() / size.height()) if size.height() else 0.0,
            )

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            frac = self._grab_frac
            self._grab_frac = None
            edge = self._nearest_edge()
            if edge is not None:
                self._snap(edge, animated=True)
            elif self._snap_edge:
                self._unsnap(grab_frac=frac)
            else:
                self._save_position()

    def enterEvent(self, event):
        # 悬停迷你卡（或正在收回中）→ 反向滑出完整面板
        if self._snap_edge and (self._t > 0.001 or self._morph_target == 1.0):
            self._expand_from_edge(animated=True)

    def leaveEvent(self, event):
        # 贴边展开态：光标移开 → 稍后自动收回
        if self._snap_edge and self._t < 0.999:
            self._auto_collapse.start()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_refresh = menu.addAction("立即刷新")
        act_snap = menu.addAction("取消贴边" if self._snap_edge else "贴边收起")
        act_blink = menu.addAction("低额度闪烁提醒")
        act_blink.setCheckable(True)
        act_blink.setChecked(self._blink_enabled)
        act_auto = menu.addAction("开机自启")
        act_auto.setCheckable(True)
        act_auto.setChecked(config.autostart_enabled())
        act_settings = menu.addAction("设置…")
        act_open = menu.addAction("打开 OpenUsage 面板")
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        self._menu_open = True
        try:
            chosen = menu.exec(event.globalPos())
        finally:
            self._menu_open = False
        if chosen is None:
            return
        if chosen is act_refresh:
            self.refresh(force_service=True)  # 手动刷新走 force，立即拿真实回源值
        elif chosen is act_snap:
            if self._snap_edge:
                self._unsnap()
            else:
                edge = self._nearest_edge(force=True)
                if edge is not None:
                    self._snap(edge, animated=True)
        elif chosen is act_blink:
            self._blink_enabled = act_blink.isChecked()
            self._cfg["blink_enabled"] = self._blink_enabled
            try:
                config.save_config(self._cfg)
            except config.ConfigError:
                pass
            self.update()
        elif chosen is act_auto:
            try:
                config.set_autostart(act_auto.isChecked())
            except config.ConfigError as exc:
                act_auto.setChecked(not act_auto.isChecked())
                QMessageBox.warning(self, "开机自启", str(exc))
        elif chosen is act_settings:
            self.show_settings()
        elif chosen is act_open:
            webbrowser.open(self._cfg.get("server_url") or "")
        elif chosen is act_quit:
            QApplication.quit()

    def show_settings(self):
        dlg = SettingsDialog(self._cfg, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        server, username, password = dlg.values()
        if not server or not username:
            QMessageBox.warning(self, "设置", "服务器地址和用户名不能为空")
            return
        if password:
            try:
                config.set_password(username, password)
            except config.ConfigError as exc:
                QMessageBox.critical(self, "保存凭据失败", str(exc))
                return
        self._cfg["server_url"] = server
        self._cfg["username"] = username
        self._cfg["token"] = ""  # 强制重新登录
        try:
            config.save_config(self._cfg)
        except config.ConfigError as exc:
            QMessageBox.warning(self, "保存配置失败", str(exc))
            return
        self._rebuild_client()
        self._quota = None
        self.update()
        self.refresh()

    # ---- 位置 ----

    def _save_position(self):
        self._cfg["window_x"] = self.x()
        self._cfg["window_y"] = self.y()
        try:
            config.save_config(self._cfg)
        except config.ConfigError:
            pass  # 位置保存失败不影响使用
