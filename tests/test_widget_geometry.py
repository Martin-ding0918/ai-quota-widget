"""Qt integration tests for rendering data and guarding account changes."""

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QFontMetricsF, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from widget_app import config
from widget_app.cards import COLOR_GRAY, COLOR_GREEN, center_font
from widget_app.services import Metric, Service
from widget_app.widget import BLINK_HALF_PERIOD_MS, QuotaWidget


class TestWidget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(config, "load_config", return_value={**config.DEFAULTS, "service_preferences": {}}), \
                patch.object(config, "get_stored_token", return_value=""), \
                patch.object(config, "migrate_tokens_to_keyring", return_value=False):
            self.widget = QuotaWidget()

    def tearDown(self):
        if self.widget._settings_dialog is not None:
            self.widget._settings_dialog.reject()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_late_old_account_response_cannot_replace_current_cards(self):
        old_client = self.widget._client
        self.widget._client = self.widget._make_client()
        current = Service(1, "当前服务", "custom", Metric("余额", "$32"))
        self.widget._services = {1: current}
        stale = {1: Service(1, "另一帐户", "custom", Metric("余额", "$22"))}
        self.widget._fetch_inflight = True
        self.widget._on_snapshot_received(old_client, stale)
        self.widget._on_failed(old_client, "前一个账户")
        self.assertEqual(self.widget._services, {1: current})
        self.assertTrue(self.widget._fetch_inflight)
        self.assertEqual(self.widget._last_error, "")

    def test_token_save_failure_does_not_escape_or_gray_data(self):
        from types import SimpleNamespace
        self.widget._fetch_inflight = True
        self.widget._manual_pending = True
        client = SimpleNamespace(token="new-token", base_url="http://a", username="u")
        with patch("widget_app.widget.config.get_stored_token", return_value="old-token"), \
                patch("widget_app.widget.config.set_stored_token",
                      side_effect=config.ConfigError("backend down")), \
                patch.object(self.widget, "refresh") as refresh:
            self.widget._finish_poll(client)
        self.assertFalse(self.widget._fetch_inflight)
        self.assertFalse(self.widget._manual_pending)
        refresh.assert_called_once_with(manual=True)
        self.assertEqual(self.widget._last_error, "")
        self.assertIn("新 token 未能保存", self.widget._credential_warning)
        self.assertIn("新 token 未能保存", self.widget._error_details())
        service = Service(1, "Kimi", "kimi", Metric("5 小时", remaining=80))
        self.assertEqual(self.widget._ring_color(service).rgb(), COLOR_GREEN.rgb())

    def test_token_save_success_clears_warning(self):
        from types import SimpleNamespace
        self.widget._credential_warning = "旧警告"
        client = SimpleNamespace(token="new-token", base_url="http://a", username="u")
        with patch("widget_app.widget.config.get_stored_token", return_value="old-token"), \
                patch("widget_app.widget.config.set_stored_token") as save:
            self.widget._finish_poll(client)
        save.assert_called_once()
        self.assertEqual(self.widget._credential_warning, "")

    def test_ring_color_error_and_blink(self):
        service = Service(5, "OpenCode", "opencode", Metric("5 小时", remaining=46.0))
        self.assertEqual(self.widget._ring_color(service).rgb(), COLOR_GREEN.rgb())
        self.assertEqual(self.widget._ring_color(replace(service, error="断线")).rgb(), COLOR_GRAY.rgb())
        low = replace(service, hero=Metric("5 小时", remaining=8.0))
        for point, alpha in ((0.0, 255), (BLINK_HALF_PERIOD_MS / 1000, 70)):
            with patch("widget_app.widget.time.monotonic", return_value=point):
                self.assertEqual(self.widget._ring_color(low).alpha(), alpha)

    def test_settings_cancel_closes_without_saving_and_can_reopen(self):
        self.widget._services = {1: Service(1, "OpenCode", "opencode", Metric("5 小时", remaining=46.0))}
        with patch.object(config, "save_config") as save:
            self.widget.show_settings()
            first = self.widget._settings_dialog
            self.widget.show_settings()
            self.assertIs(self.widget._settings_dialog, first)
            self.assertFalse(first.isModal())
            first.reject()
            self.assertIsNone(self.widget._settings_dialog)
            self.widget.show_settings()
            self.assertIsNot(self.widget._settings_dialog, first)
            save.assert_not_called()

    def test_mini_restart_uses_explicit_edge_even_with_square_card(self):
        self.widget._snap_edge = "right"
        self.widget._panel_vertical = self.widget._mini_vertical = True
        from widget_app.layout import Rect
        self.widget._services = {1: Service(1, "OpenCode", "opencode"), 2: Service(2, "DeepSeek", "deepseek")}
        self.widget._cfg["service_preferences"][self.widget._scope()] = {"mini_ids": [1]}
        geo = Rect(0, 0, 1024, 768)
        mini, panel = self.widget._grid(True, geo), self.widget._grid(False, geo)
        self.assertEqual((mini.width, mini.height), (96, 96))
        self.assertEqual(len({rect.x for rect in panel.cards}), 1)
        self.assertLess(panel.width, panel.height)

    def test_settings_saves_order_and_mini_for_current_scope(self):
        self.widget._cfg["username"] = "test"
        self.widget._services = {i: Service(i, f"Service {i}", "opencode") for i in (1, 2, 3, 4)}
        self.widget.show_settings()
        dialog = self.widget._settings_dialog
        dialog.items.setCurrentRow(3)
        dialog._move(-1)
        dialog.mini_a.setCurrentIndex(dialog.mini_a.findData(4))
        client = self.widget._client
        with patch.object(config, "save_config") as save:
            self.widget._save_settings()
            save.assert_called_once()
        panel, mini = self.widget._selection()
        self.assertEqual(panel, [1, 2, 4, 3])
        self.assertEqual(mini[0], 4)
        self.assertIs(self.widget._client, client)
        self.assertIsNone(self.widget._settings_dialog)

    def test_insecure_http_requires_explicit_confirmation(self):
        self.widget._cfg["username"] = "test"
        self.widget._cfg["server_url"] = "http://10.0.0.100:4188"
        self.widget._services = {1: Service(1, "OpenCode", "opencode")}
        self.widget.show_settings()
        dialog = self.widget._settings_dialog
        self.assertFalse(dialog.allow_insecure_http())
        with patch.object(config, "save_config") as save, \
                patch("widget_app.widget.QMessageBox.warning") as warning:
            self.widget._save_settings()
            save.assert_not_called()
            warning.assert_called_once()
        dialog.allow_insecure.setChecked(True)
        with patch.object(config, "save_config") as save:
            self.widget._save_settings()
            save.assert_called_once()
            self.assertTrue(save.call_args.args[0]["allow_insecure_http"])

    def test_loopback_http_needs_no_confirmation(self):
        self.widget._cfg["username"] = "test"
        self.widget._cfg["server_url"] = "http://localhost:4188"
        self.widget._services = {1: Service(1, "OpenCode", "opencode")}
        self.widget.show_settings()
        with patch.object(config, "save_config") as save:
            self.widget._save_settings()
            save.assert_called_once()
            self.assertFalse(save.call_args.args[0]["allow_insecure_http"])

    def test_center_font_fits_hole_or_reaches_minimum(self):
        for value, hole in (("100%", 27.0), ("100%", 9.0), ("82%", 32.0)):
            font = center_font(value, hole)
            self.assertTrue(QFontMetricsF(font).horizontalAdvance(value) <= hole or font.pointSizeF() <= 6)

    def test_timed_mini_card_paints_both_rings(self):
        from datetime import datetime, timedelta, timezone
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter
        from widget_app.cards import draw_service
        now = datetime.now(timezone.utc)
        service = Service(1, "Kimi Code", "kimi",
                          Metric("5 小时", remaining=78, reset=now + timedelta(hours=2), window_seconds=18000))
        image = QImage(96, 96, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            draw_service(painter, QRectF(0, 0, 96, 96), service, now, COLOR_GREEN, mini=True)
        finally:
            painter.end()
        self.assertGreater(image.pixelColor(48, 34).alpha(), 0)

    def test_ring_card_drops_label_and_mini_title_is_subdued(self):
        from datetime import datetime, timedelta, timezone
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter
        from widget_app.cards import draw_service

        def render(service, mini):
            image = QImage(96 if mini else 160, 96 if mini else 176,
                           QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            try:
                draw_service(painter, QRectF(0, 0, image.width(), image.height()), service,
                             datetime.now(timezone.utc), COLOR_GREEN, mini=mini)
            finally:
                painter.end()
            return image

        def band(image, y0, y1, floor):
            hits = 0
            for y in range(y0, y1):
                for x in range(10, min(image.width() - 10, 150)):
                    color = image.pixelColor(x, y)
                    if color.alpha() > 60 and max(color.red(), color.green(), color.blue()) > floor:
                        hits += 1
            return hits

        now = datetime.now(timezone.utc)
        ring = Service(1, "Kimi Code", "kimi",
                      Metric("5 小时", remaining=78, reset=now + timedelta(hours=2), window_seconds=18000))
        panel, mini = render(ring, False), render(ring, True)
        # 倒计时紧跟圆环，原状态行与额度明细之间保留间距。
        self.assertGreater(band(panel, 75, 90, 150), 0)
        self.assertEqual(band(panel, 91, 99, 150), 0)
        # 迷你卡标题已弱化到次级色（≤174），不出现主色亮度
        self.assertEqual(band(mini, 7, 23, 200), 0)
        # 面板标题保持主色
        self.assertGreater(band(panel, 7, 23, 200), 0)
        # 无环的文本主显示仍保留「余额」标签说明含义
        text = Service(2, "DeepSeek", "deepseek", Metric("余额", value="¥25.92"))
        self.assertGreater(band(render(text, False), 76, 90, 150), 0)
        generic = Service(3, "Claude", "claude", Metric("每周额度", remaining=78))
        self.assertGreater(band(render(generic, False), 76, 90, 150), 0)
        self.assertGreater(band(render(generic, True), 79, 94, 150), 0)
        generic_reset = Service(4, "Claude", "claude",
                                Metric("7 天", remaining=50, reset=now + timedelta(days=3),
                                       window_seconds=604800))
        # 面板标签与倒计时合成一行（76–91），不再占用 92–99。
        self.assertGreater(band(render(generic_reset, False), 76, 91, 150), 0)
        self.assertEqual(band(render(generic_reset, False), 92, 99, 150), 0)
        # 迷你环卡 caption 在外环（底边≈82）下方，82–89 有文字。
        self.assertGreater(band(render(generic_reset, True), 82, 89, 150), 0)
        custom_five_hour = Service(5, "自定义", "custom", Metric("5 小时剩余", remaining=78))
        self.assertEqual(band(render(custom_five_hour, False), 76, 90, 150), 0)

    def test_mini_caption_prioritizes_status_and_sits_below_ring(self):
        from widget_app.cards import mini_caption
        ring = Metric("7 天", remaining=50)
        balance = Metric("余额", value="¥25.92")
        self.assertEqual(mini_caption(ring, "", "7 天"), ("7 天", 82))
        self.assertEqual(mini_caption(ring, "数据陈旧", "7 天"), ("数据陈旧", 82))
        self.assertEqual(mini_caption(balance, "", "余额"), ("余额", 77))
        self.assertEqual(mini_caption(balance, "数据陈旧", "余额"), ("数据陈旧", 77))
        self.assertEqual(mini_caption(None, "", ""), ("", 77))

    def test_hero_font_cache_has_fixed_capacity(self):
        from widget_app.cards import hero_font_size
        hero_font_size.cache_clear()
        for amount in range(300):
            hero_font_size(f"¥{amount}.00", 140, 18.0)
        self.assertEqual(hero_font_size.cache_info().currsize, 256)
        hero_font_size.cache_clear()

    def test_error_details_are_selectable_without_blocking_widget(self):
        self.widget._services = {
            4: Service(4, "OpenCode", "opencode", error="接口返回 <429>"),
            9: Service(9, "余额账户", "custom", stale=True),
        }
        self.widget._last_error = "连接超时"
        self.assertEqual(self.widget.toolTip(), "")
        self.widget.show_errors()
        dialog = self.widget._error_dialog
        self.assertFalse(dialog.isModal())
        self.assertIn("连接超时", dialog.text())
        self.assertIn("OpenCode（#4）：接口返回 <429>", dialog.text())
        self.assertIn("余额账户（#9）：数据陈旧", dialog.text())
        self.widget.show_errors()
        self.assertIs(self.widget._error_dialog, dialog)
        dialog.close()
        self.assertIsNone(self.widget._error_dialog)

    def test_docked_tick_recovers_a_missed_leave_event(self):
        self.widget._snap_edge = "right"
        self.widget._t = 0
        self.widget.move(100, 100)
        with patch("widget_app.widget.QCursor.pos", return_value=QPoint(0, 0)):
            self.widget._tick()
            self.assertTrue(self.widget._collapse_timer.isActive())
            with patch.object(self.widget, "_animate") as animate:
                self.widget._auto_collapse()
                animate.assert_called_once_with(True)

    def test_drag_release_preserves_dock_unless_moved_to_another_edge(self):
        button = Qt.MouseButton.LeftButton
        release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(40, 40), QPointF(440, 410),
                              button, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        for current, nearby, expected in (("right", "", "right"), ("left", "", "left"),
                                          ("top", "", "top"), ("bottom", "", "bottom"),
                                          ("right", "top", "top"), ("", "", ""), ("", "left", "left")):
            with self.subTest(current=current, nearby=nearby), \
                    patch.object(self.widget, "_nearest_edge", return_value=nearby), \
                    patch.object(config, "save_config") as save:
                self.widget._snap_edge = current
                self.widget._pressed_at = QPointF(700, 500)
                self.widget._drag_offset = QPoint(40, 40)
                self.widget._dragging = True
                self.widget.mouseReleaseEvent(release)
                self.assertEqual(self.widget._snap_edge, expected)
                self.assertEqual(save.call_args.args[0]["snap_edge"], expected)

    def test_menu_only_cancels_docking_on_activation(self):
        self.widget._snap_edge = "right"
        pos = QPoint(10, 10)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, self.widget.mapToGlobal(pos))
        with patch.object(config, "save_config") as save:
            self.widget.contextMenuEvent(event)
            menu = self.widget._menu
            cancel = menu.actions()[1]
            target = menu.actionGeometry(cancel).center()
            QTest.mouseClick(menu, Qt.MouseButton.RightButton, pos=target)
            self.assertEqual(self.widget._snap_edge, "right")
            save.assert_not_called()
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=target)
            self.assertEqual(self.widget._snap_edge, "")
            self.assertEqual(save.call_args.args[0]["snap_edge"], "")

    def test_drop_at_bottom_prefers_pointer_over_overhanging_side(self):
        from widget_app.layout import Rect
        self.widget.setGeometry(-100, 645, 176, 190)
        self.assertEqual(self.widget._nearest_edge(Rect(0, 0, 1000, 800), QPoint(20, 799)), "bottom")

    def test_open_dashboard_rejects_invalid_scheme(self):
        self.widget._cfg["server_url"] = "file:///C:/Windows/System32/calc.exe"
        with patch("widget_app.widget.webbrowser.open") as open_browser, \
                patch("widget_app.widget.QMessageBox.warning") as warning:
            self.widget._open_dashboard()
            open_browser.assert_not_called()
            warning.assert_called_once()
        self.widget._cfg["server_url"] = "https://openusage.example.com"
        with patch("widget_app.widget.webbrowser.open") as open_browser:
            self.widget._open_dashboard()
            open_browser.assert_called_once_with("https://openusage.example.com")

    def test_save_settings_caps_mini_ids_at_two_with_offline_services(self):
        self.widget._cfg["username"] = "test"
        scope = self.widget._scope()
        self.widget._cfg["service_preferences"][scope] = {"panel_ids": [1, 2, 99], "mini_ids": [1, 99]}
        self.widget._services = {1: Service(1, "S1", "opencode"), 2: Service(2, "S2", "opencode")}
        self.widget.show_settings()
        dialog = self.widget._settings_dialog
        dialog.mini_b.setCurrentIndex(dialog.mini_b.findData(2))
        with patch.object(config, "save_config") as save:
            self.widget._save_settings()
            saved = save.call_args.args[0]["service_preferences"][scope]["mini_ids"]
            self.assertEqual(saved, [1, 2])

    def test_geo_and_dialogs_tolerate_missing_screen(self):
        with patch("widget_app.widget.QApplication.screenAt", return_value=None), \
                patch.object(self.widget, "screen", return_value=None), \
                patch("widget_app.widget.QApplication.primaryScreen", return_value=None):
            geo = self.widget._geo()
            self.assertGreater(geo.width, 0)
            self.widget.show_errors()
            self.assertIsNotNone(self.widget._error_dialog)
            self.widget._error_dialog.close()
