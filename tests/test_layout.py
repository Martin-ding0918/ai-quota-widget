import unittest

from widget_app.layout import (
    CARD_CELL_SIZE, CARD_H, CARD_W, MARGIN, Rect, mini_layout, nearest_edge, panel_layout, window_rect,
)


class TestLayout(unittest.TestCase):
    def test_crossing_a_work_area_edge_still_selects_it(self):
        screen = Rect(-800, 20, 800, 720)
        for expected, rect in (
            ("left", Rect(-900, 200, 176, 190)),
            ("right", Rect(-50, 200, 176, 190)),
            ("top", Rect(-450, -60, 176, 190)),
            ("bottom", Rect(-450, 660, 176, 190)),
            ("", Rect(-450, 220, 176, 190)),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(nearest_edge(rect, screen, threshold=10), expected)

    def test_empty_and_single_have_one_centered_cell(self):
        for count in (0, 1):
            grid = panel_layout(count, False, 1200, 900)
            self.assertEqual((grid.width, grid.height), (CARD_W + 2 * MARGIN, CARD_H + 2 * MARGIN))
            self.assertEqual(grid.cards, (Rect(MARGIN, MARGIN, CARD_W, CARD_H),))

    def test_two_three_and_four_have_aligned_equal_cards(self):
        for count in (2, 3, 4):
            grid = panel_layout(count, False, 1200, 900)
            first_row = [item for item in grid.cards if item.y == MARGIN]
            self.assertEqual(len(first_row), min(count, 2 if count == 4 else 3))
            self.assertEqual(first_row[0].x, grid.width - first_row[-1].x - CARD_W)
            self.assertTrue(all((item.width, item.height) == (CARD_W, CARD_H) for item in grid.cards))
        self.assertEqual(grid.cards[2].x, grid.cards[0].x)
        self.assertEqual(grid.cards[3].x, grid.cards[1].x)

    def test_unpaired_card_is_centered(self):
        grid = panel_layout(5, False, 1200, 900)
        final = grid.cards[-1]
        self.assertEqual(2 * final.x + final.width, grid.width)

    def test_vertical_eight_cards_scroll_on_short_screen(self):
        grid = panel_layout(8, True, 800, 410)
        self.assertEqual(grid.height, 410)
        self.assertEqual({cell.x for cell in grid.cards}, {MARGIN})
        last_bottom = grid.cards[-1].y + grid.cards[-1].height
        self.assertEqual(last_bottom - grid.max_scroll, grid.height - MARGIN)

    def test_narrow_screen_wraps_all_panel_cards(self):
        grid = panel_layout(3, False, 300, 400)
        self.assertLessEqual(grid.width, 300)
        self.assertEqual(len({item.y for item in grid.cards}), 3)

    def test_mini_dimensions_follow_selection_and_edge(self):
        cell = CARD_CELL_SIZE
        for vertical in (False, True):
            single = mini_layout(1, vertical)
            self.assertEqual((single.width, single.height), (cell, cell))
            double = mini_layout(2, vertical)
            self.assertEqual((double.width, double.height), (cell, 2 * cell) if vertical else (2 * cell, cell))
            triple = mini_layout(3, vertical)
            self.assertEqual((triple.width, triple.height), (cell, 3 * cell) if vertical else (3 * cell, cell))
            self.assertEqual(len(triple.cards), 3)
            capped = mini_layout(4, vertical)
            self.assertEqual(capped, triple)

    def test_edges_clamp_to_negative_origin_screen(self):
        screen = Rect(-1200, 80, 1200, 800)
        for edge in ("left", "right", "top", "bottom"):
            grid = panel_layout(8, edge in ("left", "right"), screen.width, screen.height)
            frame = window_rect(grid, screen, 9999, 9999, edge)
            self.assertGreaterEqual(frame.x, screen.x)
            self.assertGreaterEqual(frame.y, screen.y)
            self.assertLessEqual(frame.x + frame.width, screen.x + screen.width)
            self.assertLessEqual(frame.y + frame.height, screen.y + screen.height)
            if edge == "left":
                self.assertEqual(frame.x, screen.x)
            if edge == "right":
                self.assertEqual(frame.x + frame.width, screen.x + screen.width)
            if edge == "top":
                self.assertEqual(frame.y, screen.y)
            if edge == "bottom":
                self.assertEqual(frame.y + frame.height, screen.y + screen.height)

