"""Pure layouts in logical pixels. Qt painting and window events use the same rectangles."""

from dataclasses import dataclass

CARD_W, CARD_H = 160, 176
CARD_CELL_SIZE = 96
GAP, MARGIN = 8, 8
TITLE_Y, HERO_CY, HERO_LABEL_Y, STATUS_Y = 7, 51, 76, 90
DETAIL_ROWS = (106, 138)
VERTICAL_EDGES = ("left", "right")
EDGES = ("top", "bottom", *VERTICAL_EDGES)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    content_height: int
    cards: tuple[Rect, ...]

    @property
    def max_scroll(self):
        return self.content_height - self.height


def panel_layout(count, vertical, max_width, max_height):
    count = max(1, count)
    columns = 1 if vertical else count if count <= 3 else 2
    columns = max(1, min(columns, (max_width - MARGIN) // (CARD_W + GAP)))
    card_width = min(CARD_W, max(1, max_width - 2 * MARGIN))
    rows = (count + columns - 1) // columns
    width = 2 * MARGIN + columns * card_width + (columns - 1) * GAP
    content_height = 2 * MARGIN + rows * CARD_H + (rows - 1) * GAP
    cards = []
    for index in range(count):
        row, column = divmod(index, columns)
        in_row = min(columns, count - row * columns)
        x = MARGIN + (columns - in_row) * (card_width + GAP) // 2
        cards.append(Rect(x + column * (card_width + GAP), MARGIN + row * (CARD_H + GAP),
                          card_width, CARD_H))
    return Grid(width, min(max_height, content_height), content_height, tuple(cards))


def mini_layout(count, vertical):
    count = max(1, min(count, 2))
    size = CARD_CELL_SIZE
    return Grid(size if vertical else count * size, count * size if vertical else size,
                count * size if vertical else size,
                tuple(Rect(0 if vertical else i * size, i * size if vertical else 0, size, size)
                      for i in range(count)))


def window_rect(grid, screen, x, y, edge=""):
    x = max(screen.x, min(x, screen.x + screen.width - grid.width))
    y = max(screen.y, min(y, screen.y + screen.height - grid.height))
    if edge == "top":
        y = screen.y
    elif edge == "bottom":
        y = screen.y + screen.height - grid.height
    elif edge == "left":
        x = screen.x
    elif edge == "right":
        x = screen.x + screen.width - grid.width
    return Rect(x, y, grid.width, grid.height)
