"""AI 用量悬浮窗：一个窗口、两种布局（展开三列面板 ⇄ 贴边双单元迷你卡）。

数据状态、轮询、贴边/拖动事件与动画状态机在本文件；纯几何见 layout.py，
卡片与静态层绘制见 cards.py。

展开面板（512×196，三张 160×176 服务卡横排；左右贴边展开为 176×572 竖排）：
- Kimi Code：5h 主环（同心双环：内环额度/外环倒计时）+ 月总额/月代码额度条
  + 两档共用的月重置裸倒计时（format_countdown_hm/dh）。
- OpenCode：5h 主环 + 每周/每月额度条，周/月倒计时分别跟随自己的额度条。
- DeepSeek：大字余额 + 充值/赠送余额 + 可用状态（不强行换算成圆环）。
- 每卡独立状态点；单服务断线只灰显该卡，其他服务不受影响。

贴边迷你卡（192×96 横排 / 96×192 竖排）：Kimi 与 OpenCode 各占一个 96×96
单元，同心双环（内环=5h 额度阈值色、外环=5h 倒计时中性色、环心=剩余百分比）。
DeepSeek 不进迷你卡；某服务无数据只将该单元灰显为 "—"。resets_at 缺失时
外环只画轨道，不伪装成有效倒计时。

形变（v1.2）：绘制层重构后动画只负责几何与透明度——
- 静态层（卡片 chrome/标题/明细/余额）按 detail_alpha 淡出；
- 迷你层（标题）按 mini_alpha 淡入；
- 主环连续形变（位置/半径插值，弧长编码全程不变）；
- 窗口矩形同帧插值；一次动画内方向固定，中途反向只换目标续播；
- 静态内容缓存于 RenderCache（数据/DPR/方向变化才重建），动画帧只做
  缓存绘制与少量圆弧，保证帧预算。

轮询（按服务双速）：空闲档每 3 分钟读一次全量缓存；按服务独立做变化检测
（Kimi 三档百分比 / OpenCode 三档百分比 / DeepSeek 币种+余额+状态）→ 有变化的
服务进入活动档：每 1 分钟先读全量缓存，再仅对活动服务逐个单服务 force 回源；
连续 3 次静默逐服务退出。手动「立即刷新」= 全量 force 一次覆盖三个服务
（不再追加逐服务 force）。单服务 force 失败只标记该服务错误并保留其缓存值。
"""

from __future__ import annotations

import threading
import time
import webbrowser
from datetime import datetime, timezone

from PySide6.QtCore import QEasingCurve, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QCursor, QColor, QPainter, QPainterPath, QPen
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

from . import cards as cards_mod
from . import config
from .api_client import OpenUsageClient, OpenUsageError
from .cards import COLOR_SUBTLE, RenderCache, currency_symbol
from .layout import (
    CARD_QUOTA_RING_RADIUS,
    CARD_QUOTA_RING_WIDTH,
    CARD_RING_RADIUS,
    COUNTDOWN1_Y,
    COUNTDOWN2_Y,
    HERO_INNER_R,
    HERO_OUTER_R,
    HERO_RING_W,
    MorphState,
    PANEL_H_H,
    PANEL_H_W,
    PANEL_V_H,
    SERVICE_KEYS,
    SERVICE_TITLES,
    STATUS_Y,
    VERTICAL_EDGES,
    _lerp,
    _shape_orientation,
    edge_rect,
    interpolate_morph,
    mini_cell_rects,
    mini_ring_anchors,
    panel_card_rects,
    panel_ring_anchors,
    panel_size,
    smoothstep,
)
from .quota import (
    countdown_ratio,
    format_countdown_dh,
    format_countdown_hm,
    parse_usage_snapshot,
    poll_step,
    service_signature,
)

POLL_IDLE_MS = 3 * 60 * 1000        # 空闲档：读服务端缓存，3 分钟一次
POLL_ACTIVE_MS = 60 * 1000          # 活动档：活动服务逐个单服务 force，1 分钟一次
UI_TICK_MS = 1000                   # 本地 UI 刷新：倒计时 / 弧长 / 闪烁
SNAP_THRESHOLD = 10                 # 距屏幕可用边缘 ≤10px 松手即吸附
AUTO_COLLAPSE_MS = 1500             # 悬停展开后光标移开多久自动收回
ANIM_MS = 220                       # 形变动画基准时长
BLINK_HALF_PERIOD_MS = 800          # 低额度闪烁半周期（亮/暗各 0.8s）


def _force_targets(active_services, service_ids, manual: bool):
    """本轮应单服务 force 回源的服务名列表。

    manual=True → 所有已知 id 的服务；否则仅活动档中的服务。
    未记住 service_id 的服务无法单服务轮询，自动排除。
    """
    names = ("kimi", "opencode", "deepseek")
    if manual:
        return [n for n in names if service_ids.get(n)]
    return [n for n in names if n in active_services and service_ids.get(n)]


class SettingsDialog(QDialog):
    def __init__(self, cfg):
        super().__init__()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
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
        self._quotas = {n: None for n in SERVICE_KEYS}
        self._errors = {n: "" for n in SERVICE_KEYS}
        self._service_ids = {n: None for n in SERVICE_KEYS}
        self._active_services = set()
        self._prev_signatures = {n: None for n in SERVICE_KEYS}
        self._quiet_polls = {n: 0 for n in SERVICE_KEYS}
        self._last_error = ""
        self._fetch_inflight = False
        self._drag_offset = None
        self._grab_frac = None
        self._menu_open = False

        # 形变状态机（v1.2）：方向在一次动画内固定，_t 为服务形变进度
        self._t = 0.0
        self._morph_state = None
        self._morph = None
        self._snap_edge = ""
        self._card_orientation = "horizontal"   # 当前迷你卡排列（淡出层方向）
        self._panel_orientation = "horizontal"  # 当前面板排列（淡入层方向）
        self._blink_enabled = bool(self._cfg.get("blink_enabled", True))
        self._render_cache = RenderCache()

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

        force_all=True（手动「立即刷新」）→ 全量 force 一次覆盖三个服务，
        不再追加逐服务 force（避免重复回源）。
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
                if not force_all:
                    # 活动档：读缓存后仅对活动服务逐个单服务 force 回源
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
            # 两类错误都落到该服务的状态：单服务 force 失败、服务端 status=error
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
        self._render_cache.invalidate()  # 数据变化 → 静态层重建
        self._update_tooltip()
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
        self._update_tooltip()
        self.update()

    def _update_tooltip(self):
        now = datetime.now(timezone.utc).astimezone()
        lines = []
        for name in SERVICE_KEYS:
            title = SERVICE_TITLES[name]
            quota = self._quotas.get(name)
            error = self._errors.get(name) or (self._last_error if name == "kimi" else "")
            if error:
                lines.append(f"{title}：{error}")
            elif quota is None:
                lines.append(f"{title}：暂无数据")
            elif name == "deepseek":
                if quota.is_available is False:
                    lines.append(f"{title}：不可用")
                elif quota.balance is not None:
                    lines.append(f"{title}：余额 {currency_symbol(quota.currency)}{quota.balance}")
                else:
                    lines.append(f"{title}：暂无余额")
            elif quota.five_hour_remaining is None:
                lines.append(f"{title}：暂无额度")
            else:
                reset = format_countdown_hm(quota.reset_5h, now) if quota.reset_5h else None
                reset_txt = f"，{reset}后重置" if reset else ""
                stale_txt = "（数据陈旧）" if quota.stale else ""
                lines.append(f"{title}：5h 剩余 {quota.five_hour_remaining:.0f}%{reset_txt}{stale_txt}")
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
            x = max(geo.left(), min(x, geo.right() - PANEL_H_W + 1))
            y = max(geo.top(), min(y, geo.bottom() - PANEL_H_H + 1))
            self.setGeometry(x, y, PANEL_H_W, PANEL_H_H)
        elif geo is not None:
            self.setGeometry(geo.right() - PANEL_H_W - 40, geo.top() + 60, PANEL_H_W, PANEL_H_H)

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

    # ---- 形变状态机 ----

    def _morph_to(self, target, end_rect, animated=True):
        """形变到 target（0.0=面板 1.0=迷你卡）与 end_rect。

        方向在一次动画内固定（panel/card 层各自的方向），
        中途反向只更新 target 与 end_rect 并从当前进度续播。
        """
        self._stop_morph()
        end_vertical = end_rect.width() < end_rect.height()
        end_panel_or = "vertical" if end_vertical else "horizontal"
        end_card_or = "vertical" if end_vertical else "horizontal"
        if (
            not animated
            or not self.isVisible()
            or (abs(target - self._t) < 0.005 and self.geometry() == end_rect)
        ):
            self._morph_state = None
            self._t = target
            self._panel_orientation = end_panel_or
            self._card_orientation = end_card_or
            self.setGeometry(end_rect)
            self.update()
            return
        self._morph_state = MorphState(
            progress=0.0,
            start_t=self._t,
            target_t=target,
            start_rect=QRect(self.geometry()),
            end_rect=QRect(end_rect),
            panel_orientation=(self._panel_orientation if target == 1.0
                               else end_panel_or),
            card_orientation=(self._card_orientation if target == 0.0
                              else end_card_or),
            panel_cells=panel_card_rects(self._panel_orientation
                                         if target == 1.0 else end_panel_or),
            card_cells=mini_cell_rects(self._card_orientation
                                       if target == 0.0 else end_card_or),
        )
        self._morph_start_t = self._t
        self._morph_start_rect = QRect(self.geometry())
        self._morph_end_rect = QRect(end_rect)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(max(60, int(ANIM_MS * abs(target - self._morph_start_t))))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(self._on_morph_tick)
        anim.finished.connect(self._on_morph_done)
        self._morph = anim
        anim.start()

    def _on_morph_tick(self, fraction):
        if self._morph_state is None:
            return
        self._t = self._morph_state.start_t + (self._morph_state.target_t - self._morph_state.start_t) * fraction
        rect, _anchors = interpolate_morph(self._morph_state, fraction)
        self.setGeometry(rect)
        self.update()

    def _on_morph_done(self):
        self._morph = None
        if self._morph_state is not None:
            self._t = self._morph_state.target_t
            self._panel_orientation = self._morph_state.panel_orientation
            self._card_orientation = self._morph_state.card_orientation
            self.setGeometry(self._morph_state.end_rect)
        self._morph_state = None
        self._morph_end_rect = None
        self.update()

    def _stop_morph(self):
        if self._morph is not None:
            anim = self._morph
            self._morph = None
            self._morph_state = None
            self._morph_end_rect = None
            anim.stop()
            anim.deleteLater()
        else:
            self._morph_end_rect = None

    def _snap(self, edge, animated=True):
        self._snap_edge = edge
        self._auto_collapse.stop()
        panel, card = self._target_rects()
        if panel is None:
            return
        self._save_snap(panel=panel, card=card)
        self._morph_to(1.0, card, animated=animated)

    def _unsnap(self, grab_frac=None):
        edge = self._snap_edge
        self._snap_edge = ""
        self._auto_collapse.stop()
        geo = self._current_screen_geo()
        if geo is not None:
            fx, fy = grab_frac if grab_frac else (0.0, 0.0)
            cursor = QCursor.pos()
            # 展开面板的方向与贴边定位统一走 edge_rect（左/右边 → 竖面板）
            if edge in VERTICAL_EDGES:
                target = edge_rect(edge, True, int(cursor.y() - fy * PANEL_V_H), geo)
            else:
                target = edge_rect(edge or "top", True, int(cursor.x() - fx * PANEL_H_W), geo)
            self._cfg["window_x"] = target.x()
            self._cfg["window_y"] = target.y()
        else:
            target = QRect(self.x(), self.y(), PANEL_H_W, PANEL_H_H)
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

    # ---- 绘制（三层：背景 / 静态层 / 动态环）----

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        t = self._t

        # 底层：窗口背景
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 12.0, 12.0)
        p.fillPath(path, cards_mod.COLOR_BG)

        now = datetime.now(timezone.utc).astimezone()
        kimi = self._quotas.get("kimi")
        opencode = self._quotas.get("opencode")
        panel_alpha = 1.0 - smoothstep(0.05, 0.50, t)
        mini_alpha = smoothstep(0.30, 0.80, t)
        dim = 0.45 if self._last_error else 1.0

        # 中层：静态展开内容（数据变化时 RenderCache 重建；随 detail_alpha 淡出）
        self._render_cache.ensure(
            (self._panel_orientation, self._card_orientation),
            self.devicePixelRatioF(),
            self._card_specs(now),
        )
        if panel_alpha > 0.004:
            pix = self._render_cache.pixmap(self._panel_orientation)
            if pix is not None:
                p.setOpacity(panel_alpha * dim)
                p.drawPixmap(QRect(0, 0, w, h), pix)

        # 顶层：主环连续形变（弧长编码全程不变：内环=额度、外环=倒计时）
        anchors = self._ring_anchors()
        for name, (cx, cy) in anchors.items():
            quota = self._quotas.get(name)
            remaining = quota.five_hour_remaining if quota is not None else None
            reset_at = quota.reset_5h if quota is not None else None
            ratio = countdown_ratio(reset_at, now) if reset_at is not None else None
            color = self._service_color(name)
            cards_mod.draw_double_ring(
                p, cx, cy,
                r_inner=_lerp(HERO_INNER_R, CARD_QUOTA_RING_RADIUS, t),
                r_outer=_lerp(HERO_OUTER_R, CARD_RING_RADIUS, t),
                w_inner=_lerp(HERO_RING_W, CARD_QUOTA_RING_WIDTH, t),
                remaining=remaining,
                timer_ratio=ratio,
                color=color,
                outer_opacity=mini_alpha if name in ("kimi", "opencode") else 0.0,
            )

        # 面板层动态文字：5h/周/月 倒计时（随 detail_alpha 淡出，位置随窗口缩放）
        if panel_alpha > 0.004 and self._panel_orientation:
            pw, ph = panel_size(self._panel_orientation)
            sx, sy = w / pw, h / ph
            p.setOpacity(panel_alpha * dim)
            p.setFont(cards_mod._card_font(9))
            rects = panel_card_rects(self._panel_orientation)
            for idx, name in enumerate(("kimi", "opencode")):
                quota = self._quotas.get(name)
                r = rects[idx]
                rs = QRectF(r.x() * sx, r.y() * sy, r.width() * sx, r.height() * sy)
                if quota is not None and quota.reset_5h is not None:
                    p.drawText(QRectF(rs.x(), rs.y() + STATUS_Y * sy, rs.width(), 14 * sy),
                               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                               format_countdown_hm(quota.reset_5h, now))
            if kimi is not None and kimi.reset_month is not None:
                p.drawText(QRectF((rects[0].x() + 14) * sx, (rects[0].y() + COUNTDOWN2_Y) * sy,
                                  132 * sx, 14 * sy),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                           format_countdown_dh(kimi.reset_month, now))
            if opencode is not None:
                if opencode.reset_week is not None:
                    p.drawText(QRectF((rects[1].x() + 14) * sx, (rects[1].y() + COUNTDOWN1_Y) * sy,
                                      132 * sx, 14 * sy),
                               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                               format_countdown_dh(opencode.reset_week, now))
                if opencode.reset_month is not None:
                    p.drawText(QRectF((rects[1].x() + 14) * sx, (rects[1].y() + COUNTDOWN2_Y) * sy,
                                      132 * sx, 14 * sy),
                               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                               format_countdown_dh(opencode.reset_month, now))
            p.setOpacity(1.0)

        # 迷你层标题（随 mini_alpha 淡入，锚点与主环一致）
        if mini_alpha > 0.004:
            p.setOpacity(mini_alpha)
            cells = mini_cell_rects(self._card_orientation)
            for name, cell in zip(("kimi", "opencode"), cells):
                p.setPen(QPen(COLOR_SUBTLE))
                p.setFont(cards_mod._card_font(9))
                p.drawText(QRectF(cell.x(), cell.y() + 5, cell.width(), 14),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                           SERVICE_TITLES[name])
            p.setOpacity(1.0)

    def _card_specs(self, now) -> dict:
        """把三个服务的配额转成静态卡片渲染所需的纯文本数据（cards.py 消费）。

        倒计时文字为动态层，不在此构建；错误状态只影响状态点与主显示颜色。
        """
        out = {}
        for name in SERVICE_KEYS:
            quota = self._quotas.get(name)
            error = self._errors.get(name) or (self._last_error if name == "kimi" else "")
            stale = bool(quota.stale) if quota is not None else False
            spec = {
                "title": SERVICE_TITLES[name],
                "error": error,
                "stale": stale,
            }
            if name == "deepseek":
                spec["hero_kind"] = "balance"
                spec["hero_label"] = "余额"
                symbol = currency_symbol(quota.currency) if quota is not None else ""
                if quota is not None and quota.balance is not None:
                    spec["hero_amount"] = f"{symbol}{quota.balance}"
                else:
                    spec["hero_amount"] = "—"
                avail = quota.is_available if quota is not None else None
                if avail is not None:
                    spec["status_dot_color"] = ("#4ac87e" if avail else "#ed5a52")
                    spec["status_text"] = ("可用" if avail else "不可用")
                    spec["status_text_color"] = spec["status_dot_color"]
                topped = quota.topped_up_balance if quota is not None else None
                granted = quota.granted_balance if quota is not None else None
                spec["details"] = (
                    ("充值余额", f"{symbol}{topped}" if topped is not None else "—", None),
                    ("赠送余额", f"{symbol}{granted}" if granted is not None else "—", None),
                )
            else:
                spec["hero_kind"] = "ring"
                spec["hero_label"] = "5小时"
                if name == "kimi":
                    month_total = quota.month_total_remaining if quota is not None else None
                    month_code = quota.month_code_remaining if quota is not None else None
                    spec["details"] = (
                        ("月总额", f"{month_total:.0f}%" if month_total is not None else "", month_total),
                        ("月代码", f"{month_code:.0f}%" if month_code is not None else "", month_code),
                    )
                else:
                    week = quota.week_remaining if quota is not None else None
                    month = quota.month_remaining if quota is not None else None
                    spec["details"] = (
                        ("每周", f"{week:.0f}%" if week is not None else "", week),
                        ("每月", f"{month:.0f}%" if month is not None else "", month),
                    )
            out[name] = spec
        return out

    def _ring_anchors(self):
        """当前帧 Kimi/OpenCode 圆环锚点（widget 坐标）。

        形变中按 _t 在面板锚点与迷你卡锚点间插值；静态时即当前方向的锚点。
        """
        panel_or = self._panel_orientation
        card_or = self._card_orientation
        if self._morph_state is not None:
            start_or = _shape_orientation(self._morph_state.start_rect)
            end_or = _shape_orientation(self._morph_state.end_rect)
            if self._morph_state.target_t == 0.0:      # 展开：面板淡入(end)、卡淡出(start)
                panel_or, card_or = end_or, start_or
            else:                                       # 收起：面板淡出(start)、卡淡入(end)
                panel_or, card_or = start_or, end_or
        panel_anchors = panel_ring_anchors(panel_or)
        card_anchors = mini_ring_anchors(card_or)
        t = self._t
        out = {}
        for name in ("kimi", "opencode"):
            px, py = panel_anchors[name]
            cx2, cy2 = card_anchors[name]
            out[name] = (_lerp(px, cx2, t), _lerp(py, cy2, t))
        return out

    def _service_color(self, name: str) -> QColor:
        """主环颜色：阈值色；断线/无数据灰；5h 剩余 <10% 且开启提醒时闪烁。"""
        quota = self._quotas.get(name)
        if self._last_error or self._errors.get(name) or quota is None:
            return QColor(cards_mod.COLOR_GRAY)
        remaining = quota.five_hour_remaining
        if remaining is None:
            return QColor(cards_mod.COLOR_GRAY)
        color = QColor(cards_mod.color_for_remaining(remaining))
        if (
            self._blink_enabled
            and remaining < 10
            and int(time.monotonic() * 1000 / BLINK_HALF_PERIOD_MS) % 2 == 1
        ):
            color.setAlpha(70)
        return color

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
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
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
        target_t = self._morph_state.target_t if self._morph_state is not None else None
        if self._snap_edge and (self._t > 0.001 or target_t == 1.0):
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
        dlg = SettingsDialog(self._cfg)
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
        self._render_cache.invalidate()
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
