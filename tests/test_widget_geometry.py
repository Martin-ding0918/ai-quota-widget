"""Qt integration tests for rendering data and guarding account changes."""

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from PySide6.QtGui import QFontMetricsF
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
