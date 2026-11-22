"""AI 用量悬浮窗：一个窗口、两种布局（展开三列面板 ⇄ 贴边双单元迷你卡）。

展开面板（480×190，三列）：
- Kimi Code：5h 主环（同心双环：内环额度/外环倒计时）+ 月总额/月代码额度条
  + 两档共用的月重置裸倒计时。
- OpenCode：5h 主环 + 每周/每月额度条，周/月重置分别跟随自己的额度条。
- DeepSeek：大字余额 + 充值/赠送余额 + 可用状态（不强行换算成圆环）。
- 每列独立状态点与 Tooltip：绿=正常、黄=陈旧、红=错误；单服务断线只灰显该列。

迷你卡（192×96，双单元并排）：Kimi 与 OpenCode 各占一个 96×96 单元，沿用同心
双环（内环=5h 额度阈值色、外环=5h 倒计时中性色、环心=剩余百分比）。DeepSeek
不进迷你卡；某服务无数据只将该单元灰显为 "—"，另一个不动。resets_at 缺失时
外环只画轨道，不伪装成有效倒计时。

形变：单一进度 _t（0=面板 → 1=迷你卡）驱动窗口矩形与内容同帧插值——两个主环
分别从各自展开列位置滑向迷你卡左右单元中心，标题分别插值，倒计时外环淡入，
条目与 DeepSeek 列按 1−t 渐隐。可打断、可续播。

轮询（v1.1 按服务双速）：空闲档每 3 分钟读一次全量缓存；按服务独立做变化检测
（Kimi 三档百分比 / OpenCode 三档百分比 / DeepSeek 币种+余额+状态）→ 有变化的
服务进入活动档：每 1 分钟先读全量缓存，再仅对活动服务逐个单服务 force 回源；
连续 3 次静默逐服务退出。手动「立即刷新」= 全量 force 一次覆盖三个服务。
单服务 force 失败只标记该服务错误并保留其缓存值。
"""

from __future__ import annotations

import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QEasingCurve, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QCursor, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
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
    SERVICE_KEYS,
    countdown_ratio,
    format_countdown_hm,
    parse_usage_snapshot,
    poll_step,
    service_signature,
)

POLL_IDLE_MS = 3 * 60 * 1000        # 空闲档：读服务端缓存，3 分钟一次
POLL_ACTIVE_MS = 60 * 1000          # 活动档：活动服务逐个单服务 force，1 分钟一次
UI_TICK_MS = 1000                   # 本地 UI 刷新：倒计时 / 弧长 / 闪烁
FRESH_MAX_AGE_MIN = 15              # 数据超过该分钟数视为陈旧（黄点）

WIDTH, HEIGHT = 480, 190            # 展开态三列面板
CARD_WIDTH, CARD_HEIGHT = 192, 96   # 贴边迷你卡（双单元）
RING_RADIUS = 20.0                  # 面板主环半径
CARD_RING_RADIUS = 23.0             # 迷你卡外环（倒计时）半径
CARD_QUOTA_RING_RADIUS = 16.0       # 迷你卡内环（额度）半径
PANEL_TIMER_RING_RADIUS = 24.0      # 面板外环半径（渐隐淡入用）
RING_THICKNESS = 6.0                # 面板主环线宽
CARD_TIMER_RING_WIDTH = 4.0         # 迷你卡外环线宽
CARD_QUOTA_RING_WIDTH = 5.0         # 迷你卡内环线宽
RING_CY = 46.0                      # 面板主环圆心 y
CARD_CY = 58.0                      # 迷你卡圆心 y

SNAP_THRESHOLD = 10                 # 距屏幕可用边缘 ≤10px 松手即吸附
AUTO_COLLAPSE_MS = 1500             # 悬停展开后光标移开多久自动收回
ANIM_MS = 220                       # 形变动画基准时长
BLINK_HALF_PERIOD_MS = 800          # 低额度闪烁半周期

COLOR_BG = QColor(20, 22, 27, 215)
COLOR_TRACK = QColor(255, 255, 255, 32)
COLOR_TEXT = QColor(235, 236, 240)
COLOR_SUBTLE = QColor(158, 164, 174)
COLOR_GREEN = QColor(74, 200, 126)
COLOR_YELLOW = QColor(240, 181, 60)
COLOR_RED = QColor(237, 90, 82)
COLOR_GRAY = QColor(148, 153, 162)
COLOR_TIMER = QColor(200, 205, 215, 170)  # 倒计时外环中性色，不占绿黄红语义

SERVICE_KEYS = ("kimi", "opencode", "deepseek")
SERVICE_TITLES = {"kimi": "Kimi Code", "opencode": "OpenCode", "deepseek": "DeepSeek"}
SERVICE_SHORT = {"kimi": "Kimi", "opencode": "OpenCode"}
# 三列的横向布局：列原点与列中心（面板态）
COL_X = (0.0, 160.0, 320.0)
COL_CX = (80.0, 240.0, 400.0)
# 迷你卡两单元中心（仅 Kimi/OpenCode）
CARD_CX = (48.0, 144.0)

CURRENCY_SYMBOLS = {"CNY": "¥", "USD": "$", "EUR": "€"}


def currency_symbol(code: str) -> str:
    return CURRENCY_SYMBOLS.get((code or "").upper(), (code or "").upper())


def color_for_remaining(remaining):
    if remaining is None:
        return COLOR_GRAY
    if remaining > 30:
        return COLOR_GREEN
    if remaining >= 10:
        return COLOR_YELLOW
    return COLOR_RED


def format_countdown(target, now):
    """裸倒计时短文案：4小时42分 / 30天8.5小时；过期/缺失由调用方处理。"""
    if target is None:
        return "--"
    seconds = (target - now).total_seconds()
    if seconds <= 0:
        return "0分"
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days}天"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}h{minutes}m"


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
    """贴边几何（纯函数）。expanded=True → 480×190 三列面板；False → 192×96
    迷你卡。along 是沿边坐标；geo 为所在屏幕的可用区域。均钳制在屏内。"""
    if expanded:
        w, h = WIDTH, HEIGHT
    else:
        w, h = CARD_WIDTH, CARD_HEIGHT
    if edge in ("top", "bottom"):
        x = _clamp(int(along), geo.left(), geo.right() - w + 1)
        y = geo.top() if edge == "top" else geo.bottom() - h + 1
        return QRect(x, y, w, h)
    y = _clamp(int(along), geo.top(), geo.bottom() - h + 1)
    x = geo.left() if edge == "left" else geo.right() - w + 1
    return QRect(x, y, w, h)


def _force_targets(active_services, service_ids, manual: bool):
    """本轮应单服务 force 回源的服务名列表。

    manual=True → 所有已知 id 的服务；否则仅活动档中的服务。
    未记住 service_id 的服务无法单服务轮询，自动排除。
    """
    names = ("kimi", "opencode", "deepseek")
    if manual:
        return [n for n in names if service_ids.get(n)]
    return [n for n in names if n in active_services and service_ids.get(n)]


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
        self.setWindowTitle("AI 用量悬浮窗 — 设置")
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
    # payload = (UsageSnapshot, force_errors: dict)
    snapshot_received = Signal(object)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._cfg = config.load_config()
        try:
            # 会话 token 迁入凭据管理器（先写后验再清 config.json 旧字段）
            if config.migrate_tokens_to_keyring(self._cfg):
                config.save_config(self._cfg)
        except config.ConfigError:
            pass  # 迁移失败保留 config.json 旧字段，旧回退路径仍有效
        self._client = None
        self._quota = None          # 兼容旧引用（指向 kimi 配额）
        self._quotas = {n: None for n in SERVICE_KEYS}
        self._errors = {n: "" for n in SERVICE_KEYS}
        self._service_ids = {n: None for n in SERVICE_KEYS}
        self._active_services = set()
        self._prev_signatures = {n: None for n in SERVICE_KEYS}
        self._quiet_polls = {n: 0 for n in SERVICE_KEYS}
        self._last_error = ""
        self._fetch_inflight = False
        self._drag_offset = None
        self._morph = None
        self._menu_open = False

        edge = str(self._cfg.get("snap_edge") or "")
        self._snap_edge = edge if edge in ("top", "bottom", "left", "right") else ""
        self._collapsed = False
        self._blink_enabled = bool(self._cfg.get("blink_enabled", True))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("AI 用量悬浮窗")
        self.setToolTip(f"AI 用量悬浮窗\n数据源：{self._cfg.get('server_url')}")

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

        self.snapshot_received.connect(self._on_snapshot_received)
        self.failed.connect(self._on_failed)
        self._client = self._make_client()
        self.refresh()

    # ---- 数据与轮询 ----

    def _make_client(self):
        server = self._cfg.get("server_url") or ""
        username = self._cfg.get("username") or ""
        password = config.get_password(server, username) if username else None
        token = config.get_stored_token(self._cfg, server, username)
        return OpenUsageClient(server, username, password, token=token)

    def has_credentials(self) -> bool:
        server = self._cfg.get("server_url") or ""
        username = self._cfg.get("username") or ""
        if not username:
            return False
        if config.get_stored_token(self._cfg, server, username):
            return True
        return config.get_password(server, username) is not None

    def refresh(self, force_all: bool = False):
        """发起一次后台轮询。

        force_all=True（手动「立即刷新」）→ 全量 force 一次覆盖三个服务。
        否则空闲档读缓存；活动档读缓存后再对活动服务逐个单服务 force。
        """
        if self._fetch_inflight or self._client is None:
            return
        self._fetch_inflight = True
        client = self._client
        active = set(self._active_services)
        ids = dict(self._service_ids)
        targets = _force_targets(active, ids, force_all)

        def work():
            services = None
            force_errors = {}
            try:
                services = client.get_usage(force=force_all)
                snapshot = parse_usage_snapshot(services)
                # 活动档（或手动全量后）对已知服务逐个单服务 force 回源，
                # 用实时结果覆盖全量缓存中的同 id 服务。
                for name in targets:
                    sid = ids.get(name)
                    if sid is None:
                        continue
                    try:
                        svc = client.get_service_usage(sid, force=True)
                        services = [
                            svc if s.get("id") == sid else s for s in services
                        ]
                        snapshot = parse_usage_snapshot(services)
                    except OpenUsageError as exc:
                        force_errors[name] = str(exc)  # 保留该服务缓存值并标记错误
                self.snapshot_received.emit((snapshot, force_errors))
            except OpenUsageError as exc:
                self.failed.emit(str(exc))
            except Exception as exc:  # 兜底，避免后台线程静默死亡
                self.failed.emit(f"刷新异常：{exc}")

        threading.Thread(target=work, daemon=True).start()

    def _on_snapshot_received(self, payload):
        snapshot, force_errors = payload
        for name in SERVICE_KEYS:
            quota = getattr(snapshot, name)
            self._quotas[name] = quota
            if quota is not None and quota.service_id is not None:
                self._service_ids[name] = quota.service_id
            # 两类错误都落到该服务的列头状态点：单服务 force 失败、服务端 status=error
            self._errors[name] = force_errors.get(name, "") or (snapshot.errors or {}).get(name, "")
        self._last_error = ""
        self._fetch_inflight = False

        # 401 自动重登录后 token 换新：按作用域写入凭据管理器
        if self._client is not None:
            server = self._cfg.get("server_url") or ""
            username = self._cfg.get("username") or ""
            token = self._client.token
            if token and token != config.get_stored_token(self._cfg, server, username):
                try:
                    config.set_stored_token(self._cfg, server, username, token)
                    config.save_config(self._cfg)
                except config.ConfigError:
                    pass  # 写失败：内存 token 仍可用，本次会话不受影响

        # 按服务独立双速档位判定
        any_active = False
        for name in SERVICE_KEYS:
            sig = service_signature(name, self._quotas[name])
            is_active = name in self._active_services
            active, quiet = poll_step(
                self._prev_signatures[name], sig, is_active, self._quiet_polls[name]
            )
            self._prev_signatures[name] = sig
            self._quiet_polls[name] = quiet
            if active:
                self._active_services.add(name)
                any_active = True
            else:
                self._active_services.discard(name)
        self._apply_poll_cadence(any_active)
        self._update_card_tooltip()
        self.update()

    def _on_failed(self, message: str):
        self._last_error = message
        self._fetch_inflight = False
        self.update()

    def _apply_poll_cadence(self, any_active: bool):
        interval = POLL_ACTIVE_MS if any_active else POLL_IDLE_MS
        if self._poll_timer.interval() != interval:
            self._poll_timer.setInterval(interval)
            self._poll_timer.start()  # 按新节奏立即重排下一次

    def _on_failed(self, message: str):
        self._last_error = message
        self._fetch_inflight = False
        self.update()

    # ---- 本地每秒刷新 ----

    def _tick(self):
        self._update_card_tooltip()
        self.update()

    def _update_card_tooltip(self):
        now = datetime.now(timezone.utc).astimezone()
        lines = []
        for name in ("kimi", "opencode"):
            q = self._quotas.get(name)
            title = SERVICE_SHORT[name]
            if self._errors.get(name):
                lines.append(f"{title}：{self._errors[name]}")
            elif q is None or q.five_hour_remaining is None:
                lines.append(f"{title}：暂无数据")
            else:
                reset = format_countdown(q.reset_5h, now) if q.reset_5h else None
                reset_txt = f"，{reset}后重置" if reset else ""
                lines.append(f"{title}：5h 剩余 {q.five_hour_remaining:.0f}%{reset_txt}")
        tip = "\n".join(lines) or "AI 用量悬浮窗"
        if self.toolTip() != tip:
            self.setToolTip(tip)

    # ---- 贴边几何 ----

    def _current_screen_geo(self):
        screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _restore_geometry(self):
        if not self._snap_edge:
            self._restore_position()
            return
        geo = self._current_screen_geo()
        if geo is None:
            self._snap_edge = ""
            self._restore_position()
            return
        along = self._cfg.get("window_x") if self._snap_edge in ("top", "bottom") else self._cfg.get("window_y")
        if not isinstance(along, int):
            along = geo.left() if self._snap_edge in ("top", "bottom") else geo.top()
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
            self.setGeometry(geo.right() - WIDTH - 40, geo.top() + 60, WIDTH, HEIGHT)

    def _nearest_edge(self, force=False):
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
        geo = self._current_screen_geo()
        if geo is None or not self._snap_edge:
            return None, None
        if along is None:
            r = self.frameGeometry()
            along = r.left() if self._snap_edge in ("top", "bottom") else r.top()
        return (edge_rect(self._snap_edge, True, along, geo),
                edge_rect(self._snap_edge, False, along, geo))

    def _save_snap(self, panel=None, card=None):
        rect = panel if panel is not None else card
        if rect is not None:
            self._cfg["window_x"] = rect.x()
            self._cfg["window_y"] = rect.y()
        self._cfg["snap_edge"] = self._snap_edge
        config.save_config(self._cfg)

    # ---- 形变动画 ----

    def _morph_to(self, target, end_rect, animated=True):
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
            self.setGeometry(self._morph_end_rect)
        self.update()

    def _stop_morph(self):
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
        self._snap_edge = edge
        self._collapsed = True
        self._auto_collapse.stop()
        panel, card = self._target_rects()
        if panel is None:
            return
        self._save_snap(panel=panel, card=card)
        self._morph_to(1.0, card, animated=animated)

    def _unsnap(self, grab_frac=None):
        self._snap_edge = ""
        self._collapsed = False
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
        kimi = self._quotas["kimi"]
        opencode = self._quotas["opencode"]
        deepseek = self._quotas["deepseek"]

        # 两个主环：Kimi 列 → 卡左单元；OpenCode 列 → 卡右单元
        cx_kimi = _lerp(COL_CX[0], CARD_CX[0], t)
        cx_opencode = _lerp(COL_CX[1], CARD_CX[1], t)
        cy = _lerp(RING_CY, CARD_CY, t)
        kimi_err = bool(self._last_error or self._errors.get("kimi"))
        oc_err = bool(self._last_error or self._errors.get("opencode"))
        kimi_remaining = kimi.five_hour_remaining if kimi is not None else None
        oc_remaining = opencode.five_hour_remaining if opencode is not None else None
        self._draw_double_ring(
            p, cx_kimi, cy, t,
            remaining=kimi_remaining,
            reset_at=kimi.reset_5h if kimi is not None else None,
            color=self._service_color("kimi"),
            width=_lerp(RING_THICKNESS, CARD_QUOTA_RING_WIDTH, t),
            error=kimi_err,
        )
        self._draw_double_ring(
            p, cx_opencode, cy, t,
            remaining=oc_remaining,
            reset_at=opencode.reset_5h if opencode is not None else None,
            color=self._service_color("opencode"),
            width=_lerp(RING_THICKNESS, CARD_QUOTA_RING_WIDTH, t),
            error=oc_err,
        )

        # 面板专属：条目/DeepSeek 列/状态点 按 1−t 渐隐
        if t < 0.995:
            fade = (1.0 - t) * (0.45 if self._last_error else 1.0)
            p.setOpacity(fade)
            self._draw_kimi_extras(p, kimi, now)
            self._draw_opencode_extras(p, opencode, now)
            self._draw_deepseek_column(p, deepseek, now)
            p.setOpacity(1.0)

        # 三个列头标题 + 状态点：x 随列中心插值，永不渐隐
        for idx, name in enumerate(SERVICE_KEYS):
            title = SERVICE_TITLES[name]
            col_cx = COL_CX[idx]
            unit_cx = CARD_CX[idx] if idx < len(CARD_CX) else col_cx
            title_cx = _lerp(col_cx + 12 + 30, unit_cx, t)
            p.setPen(QPen(COLOR_SUBTLE))
            p.setFont(self._font(9))
            p.drawText(
                QRectF(title_cx - 60, _lerp(8, 5, t), 120, 14),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                title,
            )
            dot_color = self._column_status_color(name)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot_color)
            dot_cx = _lerp(col_cx + 148, unit_cx + CARD_WIDTH / 2 - 14, t)
            p.drawEllipse(QRectF(dot_cx, _lerp(11, 8, t), 7, 7))

    def _draw_kimi_extras(self, p: QPainter, quota, now):
        """Kimi 列主环之外的部分：5h 倒计时、两档额度条、共用月重置。"""
        col_x = COL_X[0]
        if quota is not None:
            c5 = format_countdown(quota.reset_5h, now)
            c_month = format_countdown(quota.reset_month, now)
        else:
            c5 = c_month = "--"
        p.setPen(QPen(COLOR_SUBTLE))
        p.setFont(self._font(9))
        p.drawText(QRectF(col_x + 30, 76, 100, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "5小时")
        p.drawText(QRectF(col_x + 30, 92, 100, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, c5)
        mt = quota.month_total_remaining if quota is not None else None
        mc = quota.month_code_remaining if quota is not None else None
        self._draw_quota_bar(p, col_x + 14, 112, 132, "月总额", mt)
        self._draw_quota_bar(p, col_x + 14, 136, 132, "月代码", mc)
        p.drawText(QRectF(col_x + 14, 162, 132, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, f"月重置 {c_month}")

    def _draw_opencode_extras(self, p: QPainter, quota, now):
        col_x = COL_X[1]
        week = quota.week_remaining if quota is not None else None
        month = quota.month_remaining if quota is not None else None
        week_reset = quota.reset_week if quota is not None else None
        month_reset = quota.reset_month if quota is not None else None
        self._draw_quota_bar(p, col_x + 14, 112, 132, "每周", week)
        p.setPen(QPen(COLOR_SUBTLE))
        p.setFont(self._font(9))
        p.drawText(QRectF(col_x + 14, 130, 132, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   format_countdown(week_reset, now))
        self._draw_quota_bar(p, col_x + 14, 148, 132, "每月", month)
        p.drawText(QRectF(col_x + 14, 166, 132, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   format_countdown(month_reset, now))

    def _draw_deepseek_column(self, p: QPainter, quota, now):
        col_x = COL_X[2]
        symbol = currency_symbol(quota.currency) if quota is not None else ""
        balance = quota.balance if quota is not None else None
        p.setPen(QPen(COLOR_TEXT if balance is not None else COLOR_GRAY))
        p.setFont(self._font(16, bold=True))
        big = f"{symbol}{balance}" if balance is not None else "—"
        p.drawText(QRectF(col_x + 10, 40, 140, 30),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, big)
        p.setPen(QPen(COLOR_SUBTLE))
        p.setFont(self._font(8))
        p.drawText(QRectF(col_x + 10, 72, 140, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "余额")

        rows = []
        if quota is not None:
            if quota.topped_up_balance is not None:
                rows.append(("充值余额", f"{symbol}{quota.topped_up_balance}"))
            if quota.granted_balance is not None:
                rows.append(("赠送余额", f"{symbol}{quota.granted_balance}"))
            if quota.is_available is not None:
                dot = COLOR_GREEN if quota.is_available else COLOR_RED
                rows.append(("__status__", "可用" if quota.is_available else "不可用"))
            status_color = dot if quota.is_available is not None else COLOR_GRAY
        else:
            status_color = COLOR_GRAY
        y = 96
        p.setFont(self._font(9))
        for label, value in rows:
            if label == "__status__":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(status_color)
                p.drawEllipse(QRectF(col_x + 34, y + 3, 7, 7))
                p.setPen(QPen(status_color))
                p.drawText(QRectF(col_x + 46, y, 80, 14),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value)
            else:
                p.setPen(QPen(COLOR_SUBTLE))
                p.drawText(QRectF(col_x + 20, y, 60, 14),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
                p.drawText(QRectF(col_x + 60, y, 80, 14),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, value)
            y += 16

    def _draw_quota_bar(self, p: QPainter, x, y, w, label, remaining):
        color = color_for_remaining(remaining)
        p.setPen(QPen(COLOR_SUBTLE))
        p.setFont(self._font(8))
        p.drawText(QRectF(x, y, w * 0.6, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        if remaining is not None:
            p.drawText(QRectF(x, y, w, 12),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{remaining:.0f}%")
        track_y = y + 13
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(COLOR_TRACK)
        p.drawRoundedRect(QRectF(x, track_y, w, 5), 2.5, 2.5)
        if remaining is not None:
            fill_w = w * max(0.0, min(100.0, remaining)) / 100.0
            if fill_w > 0:
                p.setBrush(color)
                p.drawRoundedRect(QRectF(x, track_y, fill_w, 5), 2.5, 2.5)

    def _draw_double_ring(self, p: QPainter, cx, cy, t, remaining, reset_at, color,
                          width, error=False):
        """同心双环（Kimi/OpenCode 共用）：内环=剩余额度（阈值色），
        外环=距 5h 重置倒计时（中性色，透明度=t 淡入）。reset_at 缺失时
        外环只画轨道，不伪装成有效倒计时。"""
        now = datetime.now(timezone.utc)
        r_inner = _lerp(RING_RADIUS, CARD_QUOTA_RING_RADIUS, t)
        r_outer = _lerp(PANEL_TIMER_RING_RADIUS, CARD_RING_RADIUS, t)
        w_inner = _lerp(width, CARD_QUOTA_RING_WIDTH, t)
        rect_inner = QRectF(cx - r_inner, cy - r_inner, 2 * r_inner, 2 * r_inner)
        rect_outer = QRectF(cx - r_outer, cy - r_outer, 2 * r_outer, 2 * r_outer)

        if t > 0.004:  # 外环（倒计时）：随收起淡入
            p.save()
            p.setOpacity(t)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(COLOR_TRACK, CARD_TIMER_RING_WIDTH,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawEllipse(rect_outer)
            ratio = countdown_ratio(reset_at, now)
            if ratio is not None:
                span = int(360 * 16 * max(0.0, min(1.0, ratio)))
                if span > 0:
                    p.setPen(QPen(COLOR_TIMER, CARD_TIMER_RING_WIDTH,
                                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                    p.drawArc(rect_outer, 90 * 16, -span)
            p.restore()

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(COLOR_TRACK, w_inner, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawEllipse(rect_inner)
        if remaining is not None:
            arc = int(360 * 16 * max(0.0, min(100.0, remaining)) / 100.0)
            if arc > 0:
                p.setPen(QPen(color, w_inner, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawArc(rect_inner, 90 * 16, -arc)

        text = f"{remaining:.0f}%" if remaining is not None else "—"
        hole = 2 * (r_inner - w_inner / 2.0) - 4
        p.setFont(self._center_font(text, hole, _lerp(10.0, 9.0, t)))
        p.setPen(QPen(color if remaining is not None else COLOR_GRAY))
        p.drawText(rect_inner, Qt.AlignmentFlag.AlignCenter, text)

    def _service_color(self, name: str) -> QColor:
        """主环颜色：阈值色；断线/无数据灰；5h 剩余 <10% 且开启提醒时闪烁。"""
        quota = self._quotas.get(name)
        if self._last_error or self._errors.get(name) or quota is None:
            return QColor(COLOR_GRAY)
        remaining = quota.five_hour_remaining
        if remaining is None:
            return QColor(COLOR_GRAY)
        color = QColor(color_for_remaining(remaining))
        if (
            self._blink_enabled
            and remaining < 10
            and int(time.monotonic() * 1000 / BLINK_HALF_PERIOD_MS) % 2 == 1
        ):
            color.setAlpha(70)
        return color

    def _column_status_color(self, name: str) -> QColor:
        if self._last_error or self._errors.get(name):
            return COLOR_RED
        quota = self._quotas.get(name)
        if quota is None:
            return COLOR_YELLOW
        if getattr(quota, "stale", False):
            return COLOR_YELLOW
        return COLOR_GREEN

    def _font(self, pt, bold: bool = False) -> QFont:
        f = QFont()
        f.setPointSizeF(float(pt))
        f.setBold(bold)
        return f
    def _center_font(self, text: str, hole: float, base_pt: float = 10.0) -> QFont:
        """环心数字自适应字号：宽度超过孔径就逐步缩小（下限 6pt），
        避免 "100%" 这类宽文本压到同色弧线上被吞掉。"""
        f = QFont()
        f.setPointSizeF(base_pt)
        f.setBold(True)
        fm = QFontMetricsF(f)
        size = base_pt
        while size > 6.0 and fm.horizontalAdvance(text) > hole:
            size = max(6.0, size - 0.5)
            f.setPointSizeF(size)
            fm = QFontMetricsF(f)
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
            self.refresh(force_all=True)  # 手动刷新：全量 force 覆盖三个服务
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
        session_changed = (
            config.credential_scope(server, username)
            != config.credential_scope(self._cfg.get("server_url") or "", self._cfg.get("username") or "")
        )
        if password:
            try:
                config.set_password(server, username, password)
            except config.ConfigError as exc:
                QMessageBox.critical(self, "保存凭据失败", str(exc))
                return
        elif session_changed and config.get_password(server, username) is None:
            QMessageBox.warning(
                self, "设置",
                "服务器或用户名已变更：请输入新组合的密码完成登录（不会复用旧服务器的凭据）",
            )
            return
        self._cfg["server_url"] = server
        self._cfg["username"] = username
        try:
            config.save_config(self._cfg)
        except config.ConfigError as exc:
            QMessageBox.warning(self, "保存配置失败", str(exc))
            return
        self._client = self._make_client()  # 新作用域无 token → 强制重新登录
        self._quotas = {n: None for n in SERVICE_KEYS}
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
