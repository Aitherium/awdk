# vendored from aither-kaggle-agent@a5cd9b6532eb039dd5b7c27633293dfd55672a9c:agent/synth/rules.py -- verbatim (see _provenance.py)
"""Rules of the h25 synthetic games -- stdlib only.

This module is embedded VERBATIM into every generated game file
(``<env-dir>/<id>/<ver>/<id>.py``), so the solver that computes a level's
optimal length (``agent/synth/solve.py``) and the game the engine runs share one
transition function byte for byte. Keep it free of imports other than the
standard library and free of module-level side effects.

A level is a plain dict (JSON-serialisable) with ``family``, ``w``, ``h`` and
family fields; :func:`prep` turns it into the prepared form every other function
takes. States are hashable (ints / tuples). Actions follow the ARC-AGI-3
convention: 1 up, 2 down, 3 left, 4 right, 6 click at a board cell ``(x, y)``.
"""

SY_PLAY = 0
SY_WIN = 1
SY_LOSE = 2
SY_DIRS = {1: (0, -1), 2: (0, 1), 3: (-1, 0), 4: (1, 0)}
SY_MOVE_FAMILIES = ("push", "keydoor", "tilt")
SY_CLICK_FAMILIES = ("lights", "recolor")

# Render roles (the generated game maps each to a palette colour).
R_FLOOR = 0
R_WALL = 1
R_PLAYER = 2
R_BOX = 3
R_TARGET = 4
R_BOX_ON = 5
R_ON = 6
R_KEY0 = 7
R_KEY1 = 8
R_LAVA = 9
R_GOAL = 10
R_TILE = 11
R_C0 = 12  # recolor palette colours R_C0 .. R_C0 + k - 1 (k <= 4)
N_ROLES = 16
# Roles each family can draw (the palette gives these distinct colours).
SY_FAMILY_ROLES = {
    "push": (R_FLOOR, R_WALL, R_PLAYER, R_BOX, R_TARGET, R_BOX_ON),
    "lights": (R_FLOOR, R_WALL, R_ON),
    "keydoor": (R_FLOOR, R_WALL, R_PLAYER, R_KEY0, R_KEY1, R_LAVA, R_GOAL),
    "recolor": (R_FLOOR, R_WALL, R_C0, R_C0 + 1, R_C0 + 2, R_C0 + 3),
    "tilt": (R_FLOOR, R_WALL, R_PLAYER, R_GOAL, R_TILE),
}


def _cells(pairs):
    return frozenset((int(p[0]), int(p[1])) for p in pairs)


def prep(lv):
    """Prepared level: the dict plus frozensets for membership tests."""
    P = dict(lv)
    P["_walls"] = _cells(lv.get("walls", []))
    P["_lava"] = _cells(lv.get("lava", []))
    P["_targets"] = _cells(lv.get("targets", []))
    if lv["family"] == "keydoor":
        P["_keys"] = {(int(k[0]), int(k[1])): i for i, k in enumerate(lv["keys"])}
        P["_doors"] = {(int(d[0]), int(d[1])): i for i, d in enumerate(lv["doors"])}
    return P


def _free(P, x, y):
    return 0 <= x < P["w"] and 0 <= y < P["h"] and (x, y) not in P["_walls"]


def initial_state(P):
    fam = P["family"]
    if fam == "push":
        return (tuple(P["player"]), tuple(sorted(tuple(b) for b in P["boxes"])))
    if fam == "lights":
        return int(P["start"])
    if fam == "keydoor":
        return (int(P["player"][0]), int(P["player"][1]), 0)
    if fam == "recolor":
        return tuple(int(c) for c in P["canvas"])
    if fam == "tilt":
        return tuple(tuple(t) for t in P["tiles"])
    raise ValueError("unknown family %r" % fam)


def is_win(P, s):
    fam = P["family"]
    if fam == "push":
        return frozenset(s[1]) == P["_targets"]
    if fam == "lights":
        return s == 0
    if fam == "keydoor":
        return (s[0], s[1]) == tuple(P["goal"])
    if fam == "recolor":
        return list(s) == list(P["target"])
    if fam == "tilt":
        return tuple(s[0]) == tuple(P["goal"])
    return False


def board_actions(P):
    """Every (action, cell) the solver branches on -- the effective action set."""
    if P["family"] in SY_MOVE_FAMILIES:
        return [(a, None) for a in (1, 2, 3, 4)]
    if P["family"] == "lights":
        return [(6, (x, y)) for y in range(P["h"]) for x in range(P["w"])]
    return [(6, (x, y)) for y in range(P["ch"]) for x in range(P["cw"])]


def _push(P, s, a):
    (px, py), boxes = s
    dx, dy = SY_DIRS[a]
    nx, ny = px + dx, py + dy
    if not _free(P, nx, ny):
        return s
    if (nx, ny) in boxes:
        bx, by = nx + dx, ny + dy
        if not _free(P, bx, by) or (bx, by) in boxes:
            return s
        boxes = tuple(sorted([b for b in boxes if b != (nx, ny)] + [(bx, by)]))
    return ((nx, ny), boxes)


def _lights(P, s, cell):
    w, h = P["w"], P["h"]
    x, y = cell
    if not (0 <= x < w and 0 <= y < h):
        return s
    for cx, cy in ((x, y), (x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
        if 0 <= cx < w and 0 <= cy < h:
            s ^= 1 << (cy * w + cx)
    return s


def _keydoor(P, s, a):
    x, y, held = s
    dx, dy = SY_DIRS[a]
    nx, ny = x + dx, y + dy
    if not _free(P, nx, ny):
        return s, SY_PLAY
    d = P["_doors"].get((nx, ny))
    if d is not None and not held & (1 << d):
        return s, SY_PLAY
    k = P["_keys"].get((nx, ny))
    if k is not None:
        held |= 1 << k
    if (nx, ny) in P["_lava"]:
        return (nx, ny, held), SY_LOSE
    return (nx, ny, held), SY_PLAY


def _recolor(P, s, cell):
    x, y = cell
    cw, ch, k = P["cw"], P["ch"], P["k"]
    if not (0 <= x < cw and 0 <= y < ch):
        return s
    i = y * cw + x
    return s[:i] + ((s[i] + 1) % k,) + s[i + 1:]


def _tilt(P, s, a):
    dx, dy = SY_DIRS[a]
    order = sorted(range(len(s)), key=lambda i: -(s[i][0] * dx + s[i][1] * dy))
    pos = list(s)
    for i in order:
        x, y = pos[i]
        while True:
            nx, ny = x + dx, y + dy
            if not _free(P, nx, ny) or (nx, ny) in pos:
                break
            x, y = nx, ny
        pos[i] = (x, y)
    return tuple(pos)


def apply(P, s, a, cell=None):
    """One action -> (next state, SY_PLAY | SY_WIN | SY_LOSE). A move into a wall,
    a click off the board or an action the family ignores returns ``s`` unchanged."""
    fam = P["family"]
    status = SY_PLAY
    if fam == "push" and a in SY_DIRS:
        s = _push(P, s, a)
    elif fam == "keydoor" and a in SY_DIRS:
        s, status = _keydoor(P, s, a)
    elif fam == "tilt" and a in SY_DIRS:
        s = _tilt(P, s, a)
    elif fam == "lights" and a == 6 and cell is not None:
        s = _lights(P, s, cell)
    elif fam == "recolor" and a == 6 and cell is not None:
        s = _recolor(P, s, cell)
    if status == SY_PLAY and is_win(P, s):
        status = SY_WIN
    return s, status


def render_roles(P, s):
    """The board as rows of render roles (h x w)."""
    w, h = P["w"], P["h"]
    g = [[R_FLOOR] * w for _ in range(h)]
    for x, y in P["_walls"]:
        g[y][x] = R_WALL
    fam = P["family"]
    if fam == "push":
        for x, y in P["_targets"]:
            g[y][x] = R_TARGET
        for x, y in s[1]:
            g[y][x] = R_BOX_ON if (x, y) in P["_targets"] else R_BOX
        g[s[0][1]][s[0][0]] = R_PLAYER
    elif fam == "lights":
        for y in range(h):
            for x in range(w):
                if s & (1 << (y * w + x)):
                    g[y][x] = R_ON
    elif fam == "keydoor":
        for x, y in P["_lava"]:
            g[y][x] = R_LAVA
        gx, gy = P["goal"]
        g[gy][gx] = R_GOAL
        held = s[2]
        for (x, y), i in P["_keys"].items():
            if not held & (1 << i):
                g[y][x] = R_KEY0 + i
        for (x, y), i in P["_doors"].items():
            if not held & (1 << i):
                g[y][x] = R_KEY0 + i  # a door wears its key's colour; both vanish on pickup
        g[s[1]][s[0]] = R_PLAYER
    elif fam == "recolor":
        cw, ch = P["cw"], P["ch"]
        for y in range(ch):
            for x in range(cw):
                g[y][x] = R_C0 + s[y * cw + x]
                g[y][cw + 1 + x] = R_C0 + int(P["target"][y * cw + x])
    elif fam == "tilt":
        gx, gy = P["goal"]
        g[gy][gx] = R_GOAL
        for i, (x, y) in enumerate(s):
            g[y][x] = R_PLAYER if i == 0 else R_TILE
    return g
