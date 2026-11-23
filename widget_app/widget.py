"""Windows widget interaction, account-scoped polling, and animated card layouts."""

from __future__ import annotations

import threading
import time
import webbrowser
from datetime import datetime, timezone
from PySide6.QtCore import QEasingCurve, QEvent, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QWidget

from . import cards, config
from .api_client import OpenUsageClient, OpenUsageError
from .layout import EDGES, VERTICAL_EDGES, Rect, mini_layout, nearest_edge, panel_layout, window_rect
from .quota import poll_step
from .services import default_ids, fetch_services, merge_services, selected_ids
from .settings import SettingsDialog

POLL_IDLE_MS = 180_000
POLL_ACTIVE_MS = 60_000
COLLAPSE_MS = 1_500
ANIMATION_MS = 220
SNAP_THRESHOLD = 10
BLINK_HALF_PERIOD_MS = 800


def _rect(qrect):
    return Rect(qrect.x(), qrect.y(), qrect.width(), qrect.height())


def _qt(rect):
    return QRect(rect.x, rect.y, rect.width, rect.height)


class QuotaWidget(QWidget):
    snapshot_received = Signal(object, object)
    failed = Signal(object, str)

    def __init__(self):
        super().__init__()
        self._cfg = config.load_config()
        try:
            if config.migrate_tokens_to_keyring(self._cfg):
                config.save_config(self._cfg)
        except config.ConfigError:
            pass
        self._client = self._make_client()
        self._services = {}
        self._signatures = {}
        self._quiet = {}
        self._active = set()
        self._last_error = ""
        self._credential_warning = ""
        self._fetch_inflight = False
        self._manual_pending = False
        self._snapshot_loaded = False
        self._blink_enabled = bool(self._cfg.get("blink_enabled", True))
        self._settings_dialog = None
        self._error_dialog = None
        self._menu = None
        saved_edge = self._cfg.get("snap_edge")
        self._snap_edge = saved_edge if saved_edge in EDGES else ""
        self._panel_vertical = self._snap_edge in VERTICAL_EDGES
        self._mini_vertical = self._panel_vertical
        self._t = float(bool(self._snap_edge))
        self._animation = None
        self._animation_target = None
        self._pressed_at = None
        self._dragging = False
        self._scroll = 0

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWindowTitle("AI 用量悬浮窗")

        self._poll_timer = QTimer(self, interval=POLL_IDLE_MS)
        self._poll_timer.timeout.connect(self.refresh)
        self._poll_timer.start()
        self._tick_timer = QTimer(self, interval=1000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()
        self._collapse_timer = QTimer(self, interval=COLLAPSE_MS, singleShot=True)
        self._collapse_timer.timeout.connect(self._auto_collapse)
        self.snapshot_received.connect(self._on_snapshot_received)
        self.failed.connect(self._on_failed)

        x, y = (self._cfg.get("window_x"), self._cfg.get("window_y"))
        screen = QApplication.screenAt(QRect(x, y, 1, 1).center()) if isinstance(x, int) and isinstance(y, int) else None
        screen = screen or QApplication.primaryScreen()
        if screen:
            geo = _rect(screen.availableGeometry())
            grid = self._grid(bool(self._snap_edge), geo)
            x = x if isinstance(x, int) else geo.x + geo.width - grid.width - 40
            y = y if isinstance(y, int) else geo.y + 60
            self.setGeometry(_qt(window_rect(grid, geo, x, y, self._snap_edge)))
        self.refresh()

    def _scope(self):
        return config.credential_scope(self._cfg["server_url"], self._cfg["username"])

    def _make_client(self):
        server, user = self._cfg["server_url"], self._cfg["username"]
        return OpenUsageClient(server, user, config.get_password(server, user) if user else None,
                               token=config.get_stored_token(self._cfg, server, user))

    def has_credentials(self):
        return bool(self._client.username and (self._client.token or self._client.password is not None))

    def _selection(self):
        scope = self._scope()
        preferences = self._cfg["service_preferences"].get(scope, {})
        return selected_ids(self._services, preferences, self._cfg["legacy_service_scope"] == scope)

    def refresh(self, manual=False):
        if not self.has_credentials():
            return
        if self._fetch_inflight:
            self._manual_pending |= manual
            return
        self._fetch_inflight = True
        client = self._client
        panel, _ = self._selection()
        targets = [i for i in panel if manual or i in self._active]

        def work():
            try:
                result = fetch_services(client, targets)
            except OpenUsageError as exc:
                self.failed.emit(client, str(exc))
            except Exception as exc:
                self.failed.emit(client, f"读取出错（{type(exc).__name__}）")
            else:
                self.snapshot_received.emit(client, result)

        threading.Thread(target=work, daemon=True).start()

    def _on_snapshot_received(self, client, result):
        if client is not self._client:
            return
        before = self._selection()
        self._services = merge_services(result, self._services)
        self._last_error = ""
        self._snapshot_loaded = True
        scope = self._scope()
        if result and self._cfg["legacy_service_scope"] == scope:
            if scope not in self._cfg["service_preferences"]:
                ids = default_ids(self._services, legacy=True) or default_ids(self._services)
                self._cfg["service_preferences"][scope] = {"panel_ids": ids, "mini_ids": ids[:2]}
            self._cfg["legacy_service_scope"] = ""
            self._persist()
        panel, mini = self._selection()
        self._update_polling(panel)
        if before != (panel, mini):
            self._reflow()
        if self._settings_dialog is not None and self._settings_dialog.scope == scope:
            self._settings_dialog.populate(self._services, panel, mini)
        self._finish_poll(client)

    def _update_polling(self, panel):
        selected = set(panel)
        self._active.intersection_update(selected)
        self._quiet = {i: streak for i, streak in self._quiet.items() if i in selected}
        self._signatures = {i: sig for i, sig in self._signatures.items() if i in selected}
        for service_id in panel:
            service = self._services[service_id]
            if service.stale or service.error:
                continue
            previous = self._signatures.get(service_id)
            signature = service.signature
            active, quiet = poll_step(previous, signature, service_id in self._active,
                                      self._quiet.get(service_id, 0))
            self._signatures[service_id] = signature
            self._quiet[service_id] = quiet
            if active:
                self._active.add(service_id)
            else:
                self._active.discard(service_id)
        interval = POLL_ACTIVE_MS if self._active else POLL_IDLE_MS
        if self._poll_timer.interval() != interval:
            self._poll_timer.setInterval(interval)

    def _on_failed(self, client, error):
        if client is self._client:
            self._last_error = error
            self._finish_poll(client)

    def _finish_poll(self, client):
        try:
            token = client.token
            if token and token != config.get_stored_token(self._cfg, client.base_url, client.username):
                try:
                    config.set_stored_token(self._cfg, client.base_url, client.username, token)
                except config.ConfigError:
                    self._credential_warning = "新 token 未能保存，重启后可能需要重新登录"
                else:
                    self._credential_warning = ""
        finally:
            self._fetch_inflight = False
            self.update()
            if self._manual_pending:
                self._manual_pending = False
                self.refresh(manual=True)

    def _geo(self):
        screen = QApplication.screenAt(QCursor.pos()) if self._dragging else None
        screen = screen or QApplication.screenAt(self.frameGeometry().center()) or self.screen()
        return _rect(screen.availableGeometry())

    def _grid(self, mini, geo):
        panel, compact = self._selection()
        if mini:
            return mini_layout(len(compact), self._mini_vertical)
        return panel_layout(len(panel), self._panel_vertical, geo.width, geo.height)

    def _reflow(self, x=None, y=None):
        if self._dragging:
            return
        mini = (self._animation_target if self._animation_target is not None else self._t) >= 0.5
        self._animate(mini, x=x, y=y)

    def _animate(self, mini, x=None, y=None, geo=None):
        self._stop_animation()
        geo = geo or self._geo()
        grid = self._grid(mini, geo)
        origin = self.geometry()
        goal = _qt(window_rect(grid, geo, origin.x() if x is None else x,
                               origin.y() if y is None else y, self._snap_edge))
        start_t, target_t = self._t, float(mini)
        self._scroll = min(self._scroll, self._grid(False, geo).max_scroll)
        if origin == goal and start_t == target_t:
            self.update()
            return
        animation = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=ANIMATION_MS)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation, self._animation_target = animation, target_t

        def step(fraction):
            self._t = start_t + (target_t - start_t) * fraction
            self.setGeometry(QRect(*[round(a + (b - a) * fraction)
                                     for a, b in zip(origin.getRect(), goal.getRect())]))
            self.update()

        def finish():
            self._animation = self._animation_target = None
            self._t = target_t
            self.setGeometry(goal)
            self.update()
            animation.deleteLater()

        animation.valueChanged.connect(step)
        animation.finished.connect(finish)
        animation.start()

    def _stop_animation(self):
        if self._animation is not None:
            self._animation.stop()
            self._animation.deleteLater()
            self._animation = self._animation_target = None

    def _nearest_edge(self, geo, release):
        pointer = Rect(release.x(), release.y(), 1, 1)
        return (nearest_edge(pointer, geo, SNAP_THRESHOLD)
                or nearest_edge(_rect(self.frameGeometry()), geo, SNAP_THRESHOLD))

    def _snap(self, edge, geo):
        self._snap_edge = edge
        self._collapse_timer.stop()
        self._mini_vertical = self._panel_vertical = edge in VERTICAL_EDGES
        target = window_rect(self._grid(True, geo), geo, self.x(), self.y(), edge)
        self._save_position(target.x, target.y)
        self._animate(True, geo=geo)

    def _settle_floating(self, geo):
        target = window_rect(self._grid(False, geo), geo, self.x(), self.y())
        self._save_position(target.x, target.y)
        self._animate(False, target.x, target.y, geo=geo)

    def _auto_collapse(self):
        if self._menu is not None or self._dragging:
            self._collapse_timer.start()
        elif self._snap_edge and not self.frameGeometry().contains(QCursor.pos()):
            self._animate(True)

    def _tick(self):
        if (self._snap_edge and self._t < 0.999 and self._animation_target != 1
                and self._menu is None and not self._dragging and not self._collapse_timer.isActive()
                and not self.frameGeometry().contains(QCursor.pos())):
            self._collapse_timer.start()
        self.update()

    def enterEvent(self, event):
        self._collapse_timer.stop()
        if self._snap_edge and not self._dragging and self._menu is None and self._animation_target != 0:
            self._animate(False)

    def leaveEvent(self, event):
        if self._snap_edge and not self._dragging and self._animation_target != 1:
            self._collapse_timer.start()

    def _empty_text(self):
        if self._last_error:
            return "连接失败"
        if not self.has_credentials():
            return "连接 OpenUsage"
        if not self._snapshot_loaded:
            return "正在读取…"
        return "选择服务"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        bounds = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(bounds, 12, 12)
        painter.fillPath(path, cards.COLOR_BG)
        painter.setClipPath(path)
        panel, mini = self._selection()
        now = datetime.now(timezone.utc).astimezone()
        if self._t < 0.999:
            self._paint_layer(painter, self._grid(False, self._geo()), panel, now, False, 1 - self._t)
        if self._t > 0.001:
            self._paint_layer(painter, self._grid(True, self._geo()), mini, now, True, self._t)

    def _paint_layer(self, painter, grid, ids, now, mini, alpha):
        painter.save()
        painter.setOpacity(alpha)
        x = (self.width() - grid.width) / 2
        y = (self.height() - grid.height) / 2
        painter.translate(x, y)
        if not ids:
            cards.draw_empty(painter, QRectF(2, 2, grid.width - 4, grid.height - 4), self._empty_text())
        else:
            for service_id, cell in zip(ids, grid.cards):
                top = cell.y - (self._scroll if not mini else 0)
                if top >= grid.height or top + cell.height <= 0:
                    continue
                service = self._services[service_id]
                cards.draw_service(painter, QRectF(cell.x, top, cell.width, cell.height), service, now,
                                   self._ring_color(service), mini=mini, offline=bool(self._last_error))
        if not mini and grid.max_scroll:
            track_height = grid.height - 16
            thumb_height = max(18, track_height * grid.height / grid.content_height)
            top = 8 + (track_height - thumb_height) * self._scroll / grid.max_scroll
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(cards.COLOR_SUBTLE)
            painter.drawRoundedRect(QRectF(grid.width - 5, top, 2, thumb_height), 1, 1)
        painter.restore()

    def _ring_color(self, service):
        remaining = service.hero.remaining if service.hero else None
        if self._last_error or service.error or service.unavailable or service.stale or remaining is None:
            return QColor(cards.COLOR_GRAY)
        color = QColor(cards.color_for_remaining(remaining))
        if self._blink_enabled and remaining < 10 and int(time.monotonic() * 1000 / BLINK_HALF_PERIOD_MS) % 2:
            color.setAlpha(70)
        return color

    def wheelEvent(self, event):
        maximum = self._grid(False, self._geo()).max_scroll
        if self._t < 0.5 and maximum:
            delta = event.pixelDelta().y() or round(event.angleDelta().y() / 120 * 72)
            self._scroll = max(0, min(maximum, self._scroll - delta))
            self.update()
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_at = event.globalPosition()
            self._drag_offset = event.position().toPoint()

    def mouseMoveEvent(self, event):
        if self._pressed_at is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        if not self._dragging:
            distance = (event.globalPosition() - self._pressed_at).manhattanLength()
            if distance < QApplication.startDragDistance():
                return
            self._dragging = True
            self._stop_animation()
            self._collapse_timer.stop()
        self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._pressed_at is None:
            return
        self._pressed_at = None
        dragged, self._dragging = self._dragging, False
        if dragged:
            release = event.globalPosition().toPoint()
            self.move(release - self._drag_offset)
            screen = QApplication.screenAt(release)
            geo = _rect(screen.availableGeometry()) if screen else self._geo()
            edge = self._nearest_edge(geo, release) or self._snap_edge
            if edge:
                self._snap(edge, geo)
            else:
                self._settle_floating(geo)
        elif not self._selection()[1 if self._t >= 0.5 else 0]:
            self.show_settings()

    def eventFilter(self, watched, event):
        if (watched is self._menu
                and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                                     QEvent.Type.MouseButtonDblClick)
                and event.button() == Qt.MouseButton.RightButton):
            return True
        return super().eventFilter(watched, event)

    def contextMenuEvent(self, event):
        if self._menu is not None:
            self._menu.close()
        menu = QMenu(self)
        self._menu = menu
        menu.installEventFilter(self)
        menu.addAction("立即刷新", lambda: self.refresh(manual=True))
        menu.addAction("取消贴边" if self._snap_edge else "贴边收起", self._toggle_snap)
        for label, checked, handler in (
            ("低额度闪烁提醒", self._blink_enabled, self._toggle_blink),
            ("开机自启", config.autostart_enabled(), self._toggle_autostart),
        ):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(checked)
            action.triggered.connect(handler)
        menu.addAction("设置…", lambda: QTimer.singleShot(0, self.show_settings))
        if self._error_details():
            menu.addAction("错误详情…", lambda: QTimer.singleShot(0, self.show_errors))
        menu.addAction("打开 OpenUsage 面板", lambda: webbrowser.open(self._cfg["server_url"]))
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)

        def hide():
            if self._menu is menu:
                self._menu = None
                if self._snap_edge:
                    self._collapse_timer.start()
            menu.deleteLater()

        menu.aboutToHide.connect(hide)
        menu.popup(event.globalPos())

    def _error_details(self):
        details = [f"连接 OpenUsage：{self._last_error}"] if self._last_error else []
        if self._credential_warning:
            details.append(self._credential_warning)
        for service_id in self._selection()[0]:
            service = self._services[service_id]
            status = service.error
            if not status and (service.stale or service.unavailable):
                status = cards.service_status(service)
            if status:
                details.append(f"{service.name}（#{service_id}）：{status}")
        return "\n".join(details)

    def show_errors(self):
        if self._error_dialog is None:
            dialog = QMessageBox(QMessageBox.Icon.Information, "AI 用量悬浮窗 — 错误详情", "",
                                 QMessageBox.StandardButton.Close)
            dialog.button(QMessageBox.StandardButton.Close).setText("关闭")
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
            dialog.setModal(False)
            dialog.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
            self._error_dialog = dialog

            def close(_result):
                self._error_dialog = None
                dialog.deleteLater()

            dialog.finished.connect(close)
        dialog = self._error_dialog
        dialog.setText(self._error_details() or "暂无错误详情。")
        dialog.adjustSize()
        dialog.move(self.screen().availableGeometry().center() - dialog.rect().center())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _toggle_snap(self):
        geo = self._geo()
        if self._snap_edge:
            self._snap_edge = ""
            self._panel_vertical = False
            self._collapse_timer.stop()
            self._settle_floating(geo)
        else:
            self._snap(nearest_edge(_rect(self.frameGeometry()), geo), geo)

    def _toggle_blink(self, checked):
        self._blink_enabled = checked
        self._cfg["blink_enabled"] = checked
        self._persist()
        self.update()

    def _toggle_autostart(self, checked):
        try:
            config.set_autostart(checked)
        except config.ConfigError as exc:
            QMessageBox.warning(self, "开机自启", str(exc))

    def show_settings(self):
        if self._settings_dialog is not None:
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        panel, mini = self._selection()
        dialog = SettingsDialog(self._cfg, self._services, panel, mini)
        self._settings_dialog = dialog
        dialog.save_requested.connect(self._save_settings)

        def close(_result):
            self._settings_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(close)
        dialog.move(self.screen().availableGeometry().center() - dialog.rect().center())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _save_settings(self):
        dialog = self._settings_dialog
        server, username, password = dialog.credentials()
        if server and "://" not in server:
            server = "http://" + server
        origin = config.normalize_origin(server)
        security = config.http_security(server)
        if not username or not origin or security == "invalid":
            QMessageBox.warning(dialog, "检查连接", "请输入 HTTP 或 HTTPS 服务器地址及用户名。")
            return
        if security == "insecure" and not dialog.allow_insecure_http():
            QMessageBox.warning(
                dialog, "不安全的 HTTP",
                "此地址使用 HTTP，密码和 token 将明文传输。请勾选“允许不安全的 HTTP”后再保存。")
            return
        scope = config.credential_scope(server, username)
        changed = scope != self._scope() or server != self._cfg["server_url"]
        if scope != self._scope() and not password and config.get_password(server, username) is None:
            QMessageBox.warning(dialog, "需要密码", "请输入新帐户的密码。")
            return
        cfg = {**self._cfg, "server_url": server, "username": username,
               "allow_insecure_http": security == "insecure" and dialog.allow_insecure_http()}
        if dialog.selection_changed and scope == dialog.scope:
            previous = cfg["service_preferences"].get(scope, {})
            selected = dialog.preferences()
            for key in ("panel_ids", "mini_ids"):
                selected[key].extend(i for i in previous.get(key, []) if i not in self._services)
            cfg["service_preferences"] = {**cfg["service_preferences"], scope: selected}
        try:
            if password:
                config.set_password(server, username, password)
            config.save_config(cfg)
        except config.ConfigError as exc:
            QMessageBox.warning(dialog, "保存失败", str(exc))
            return
        self._cfg = cfg
        if changed or password:
            self._client = self._make_client()
            self._fetch_inflight = self._manual_pending = False
            self._credential_warning = ""
            self._signatures.clear()
            self._quiet.clear()
            self._active.clear()
            if changed:
                if self._error_dialog is not None:
                    self._error_dialog.close()
                self._services = {}
                self._snapshot_loaded = False
                self._last_error = ""
            self.refresh()
        else:
            panel, _ = self._selection()
            self._update_polling(panel)
        self._scroll = 0
        self._reflow()
        dialog.accept()

    def _save_position(self, x=None, y=None):
        self._cfg.update(window_x=self.x() if x is None else x,
                         window_y=self.y() if y is None else y, snap_edge=self._snap_edge)
        self._persist()

    def _persist(self):
        try:
            config.save_config(self._cfg)
        except config.ConfigError:
            pass
