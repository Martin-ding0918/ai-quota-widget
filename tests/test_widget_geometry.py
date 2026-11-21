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


class TestCenterFont(unittest.IsolatedAsyncioTestCase):
    """环心数字自适应字号：宽文本缩到放进孔径，不变式 = 放得下或已到 6pt 下限。

    注意：必须用真实平台（windows）的字体度量——offscreen 平台缺字体引擎，
    QFontMetricsF 对 pointSize 不敏感，测出的缩放是假的。
    """

    async def asyncSetUp(self):
        os.environ.pop("QT_QPA_PLATFORM", None)  # 强制真实平台才有真字体度量
        self.app = QApplication.instance() or QApplication([])
        tmp = Path(tempfile.gettempdir()) / "kqw-cf-config.json"
        tmp.write_text("{}", encoding="utf-8")
        self._old_cfg_path = config.CONFIG_PATH
        config.CONFIG_PATH = tmp
        self.widget = QuotaWidget()

    async def asyncTearDown(self):
        config.CONFIG_PATH = self._old_cfg_path

    def test_shrinks_until_text_fits_hole(self):
        from PySide6.QtGui import QFontMetricsF
        # 迷你卡内环孔径 ~23px："100%" 10pt 必然放不下 → 必须缩
        f = self.widget._center_font("100%", 23.0, 10.0)
        fm = QFontMetricsF(f)
        self.assertLessEqual(fm.horizontalAdvance("100%"), 23.0)
        self.assertLess(f.pointSizeF(), 10.0)
        self.assertGreaterEqual(f.pointSizeF(), 6.0)

    def test_floor_at_six_points_for_absurdly_small_hole(self):
        from PySide6.QtGui import QFontMetricsF
        f = self.widget._center_font("100%", 8.0, 10.0)
        self.assertLessEqual(f.pointSizeF(), 6.5)  # 下限保护：缩到 6pt 附近为止

    def test_roomy_hole_keeps_base_size(self):
        from PySide6.QtGui import QFontMetricsF
        # 单字符/面板孔径充足 → 保持基准字号
        f = self.widget._center_font("—", 34.0, 10.0)
        self.assertEqual(f.pointSizeF(), 10.0)
        f2 = self.widget._center_font("82%", 34.0, 10.0)
        self.assertGreaterEqual(f2.pointSizeF(), 8.0)  # 两位数只是微缩或不变

    def test_fractional_shrink_steps(self):
        from PySide6.QtGui import QFontMetricsF
        # 中间孔径：0.5pt 步进平滑缩小，最终放得下
        f = self.widget._center_font("100%", 27.0, 10.0)
        fm = QFontMetricsF(f)
        self.assertLessEqual(fm.horizontalAdvance("100%"), 27.0)
        self.assertGreaterEqual(f.pointSizeF(), 6.0)
        self.assertLess(f.pointSizeF(), 10.0)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
