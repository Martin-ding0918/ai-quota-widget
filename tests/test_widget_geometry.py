"""test_widget_geometry.py — 贴边几何纯函数 edge_rect + 收起卡颜色/闪烁的单测。"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from widget_app import config
from widget_app.quota import KimiQuota
from widget_app.widget import (
    BLINK_HALF_PERIOD_MS,
    CARD_SIZE,
    COLOR_GRAY,
    COLOR_GREEN,
    HEIGHT,
    WIDTH,
    QuotaWidget,
    edge_rect,
)

GEO = QRect(0, 0, 2560, 1400)  # 可用区域：bottom=1399, right=2559


class TestEdgeRect(unittest.TestCase):
    def test_top_collapsed_card(self):
        self.assertEqual(edge_rect("top", False, 100, GEO), QRect(100, 0, CARD_SIZE, CARD_SIZE))

    def test_top_expanded_panel(self):
        self.assertEqual(edge_rect("top", True, 100, GEO), QRect(100, 0, WIDTH, HEIGHT))

    def test_bottom_collapsed_card_hugs_bottom(self):
        r = edge_rect("bottom", False, 100, GEO)
        self.assertEqual(r, QRect(100, 1399 - CARD_SIZE + 1, CARD_SIZE, CARD_SIZE))
        self.assertEqual(r.bottom(), GEO.bottom())

    def test_bottom_expanded_panel(self):
        r = edge_rect("bottom", True, 100, GEO)
        self.assertEqual(r.bottom(), GEO.bottom())
        self.assertEqual(r.height(), HEIGHT)

    def test_left_collapsed_card(self):
        self.assertEqual(edge_rect("left", False, 100, GEO), QRect(0, 100, CARD_SIZE, CARD_SIZE))

    def test_right_collapsed_card_hugs_right(self):
        r = edge_rect("right", False, 100, GEO)
        self.assertEqual(r.right(), GEO.right())
        self.assertEqual(r, QRect(2559 - CARD_SIZE + 1, 100, CARD_SIZE, CARD_SIZE))

    def test_right_expanded_panel(self):
        r = edge_rect("right", True, 100, GEO)
        self.assertEqual(r.right(), GEO.right())
        self.assertEqual(r.width(), WIDTH)

    def test_along_clamped_inside_screen(self):
        self.assertEqual(edge_rect("top", False, -50, GEO).left(), 0)
        self.assertEqual(edge_rect("top", False, 99999, GEO).right(), GEO.right())
        self.assertEqual(edge_rect("left", False, -50, GEO).top(), 0)
        self.assertEqual(edge_rect("left", False, 99999, GEO).bottom(), GEO.bottom())

    def test_expanded_panel_clamped_inside_screen(self):
        self.assertEqual(edge_rect("top", True, 99999, GEO).right(), GEO.right())
        self.assertEqual(edge_rect("right", True, -50, GEO).top(), 0)


class TestLineBlink(unittest.TestCase):
    """主环颜色：阈值色 / 断线灰 / 低额度闪烁（打桩 time.monotonic 确定性验证）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        tmp = Path(tempfile.gettempdir()) / "kqw-test-config.json"
        tmp.write_text("{}", encoding="utf-8")
        cls._old_cfg_path = config.CONFIG_PATH
        config.CONFIG_PATH = tmp  # 不碰真实 config
        cls.widget = QuotaWidget()
        for sig in (cls.widget.fetched, cls.widget.failed):
            try:
                sig.disconnect()
            except RuntimeError:
                pass
        cls.widget._client = None  # 阻断轮询线程的影响
        cls.now = datetime.now(timezone.utc)

    @classmethod
    def tearDownClass(cls):
        config.CONFIG_PATH = cls._old_cfg_path

    def _quota(self, remaining):
        return KimiQuota(
            five_hour_remaining=remaining, month_total_remaining=50.0,
            month_code_remaining=60.0, reset_5h=self.now + timedelta(hours=2, minutes=13),
            reset_month=None, fetched_at=self.now, stale=False,
        )

    def setUp(self):
        w = self.widget
        w._quota = self._quota(46.0)
        w._last_error = ""
        w._snap_edge = "top"
        w._blink_enabled = True

    def _color_at(self, monotonic_value):
        import widget_app.widget as wmod
        real = wmod.time.monotonic
        try:
            wmod.time.monotonic = lambda: monotonic_value
            return self.widget._five_hour_color()
        finally:
            wmod.time.monotonic = real

    def test_normal_remaining_is_threshold_color(self):
        # 46% 绿色，任意相位都是不透明
        c1 = self._color_at(1000.0)
        c2 = self._color_at(1000.0 + BLINK_HALF_PERIOD_MS / 1000)
        self.assertEqual(c1.alpha(), 255)
        self.assertEqual(c2.alpha(), 255)
        self.assertEqual((c1.red(), c1.green(), c1.blue()), (COLOR_GREEN.red(), COLOR_GREEN.green(), COLOR_GREEN.blue()))

    def test_low_remaining_blinks(self):
        self.widget._quota = self._quota(8.0)
        bright = self._color_at(1000.0)                       # 相位 0（亮）
        dim = self._color_at(1000.0 + BLINK_HALF_PERIOD_MS / 1000)  # 相位 1（暗）
        self.assertEqual(bright.alpha(), 255)
        self.assertEqual(dim.alpha(), 70)

    def test_blink_disabled_stays_solid(self):
        self.widget._quota = self._quota(8.0)
        self.widget._blink_enabled = False
        c1 = self._color_at(1000.0)
        c2 = self._color_at(1000.0 + BLINK_HALF_PERIOD_MS / 1000)
        self.assertEqual(c1.alpha(), 255)
        self.assertEqual(c2.alpha(), 255)

    def test_error_or_missing_data_is_gray(self):
        self.widget._quota = None
        self.widget._last_error = "模拟断线"
        self.assertEqual(self.widget._five_hour_color().rgb(), COLOR_GRAY.rgb())
        self.widget._last_error = ""
        self.assertEqual(self.widget._five_hour_color().rgb(), COLOR_GRAY.rgb())
        self.widget._quota = self._quota(46.0)
        self.widget._quota.five_hour_remaining = None
        self.assertEqual(self.widget._five_hour_color().rgb(), COLOR_GRAY.rgb())


if __name__ == "__main__":
    unittest.main()
