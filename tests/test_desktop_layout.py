import pytest

from adk.desktop.layout import MODES, Rect, split

WORK = Rect(0, 0, 1920, 1040)


def test_modes():
    assert MODES == ("side_by_side", "stacked", "grid")


def test_side_by_side_two_no_gap():
    assert split(WORK, 2, gap=0) == [Rect(0, 0, 960, 1040), Rect(960, 0, 960, 1040)]


def test_side_by_side_gap_right_edge_exact():
    cells = split(WORK, 2, gap=8)
    assert cells == [Rect(0, 0, 956, 1040), Rect(964, 0, 956, 1040)]
    assert cells[-1].right == 1920


def test_stacked_rows():
    cells = split(WORK, 3, mode="stacked", gap=0)
    assert [c.h for c in cells] == [346, 346, 348]
    assert all(c.w == 1920 and c.x == 0 for c in cells)
    assert cells[-1].bottom == 1040


def test_grid_three_is_two_by_two():
    cells = split(WORK, 3, mode="grid", gap=0)
    assert cells == [Rect(0, 0, 960, 520), Rect(960, 0, 960, 520), Rect(0, 520, 960, 520)]


def test_remainder_absorbed_by_last_cell():
    cells = split(Rect(0, 0, 1000, 500), 3, gap=0)
    assert [c.w for c in cells] == [333, 333, 334]
    assert cells[-1].right == 1000


def test_gap_clamped_to_axis():
    cells = split(Rect(0, 0, 100, 50), 2, gap=500)
    assert all(c.w >= 1 for c in cells)
    assert cells[0].right <= cells[1].x
    assert cells[-1].right == 100


def test_negative_origin_preserved():
    area = Rect(-1920, -40, 1920, 1080)
    cells = split(area, 2, gap=0)
    assert cells == [Rect(-1920, -40, 960, 1080), Rect(-960, -40, 960, 1080)]
    assert cells[-1].right == 0


def test_single_window_fills_area():
    assert split(WORK, 1, gap=8) == [WORK]


def test_zero_count_is_empty():
    assert split(WORK, 0) == []


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        split(WORK, 2, mode="spiral")
