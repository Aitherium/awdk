# awdk for agents

Read this if you are an agent (or a human) editing this package. It is short on
purpose: the gotchas that cost a session, and pointers to the docs that hold
the rest. adk does not read this file (or `CLAUDE.md`) at runtime — it is for you.

## What this is

PyPI distribution **`awdk`**, import package **`adk`**, Python >= 3.10.
Console scripts: `adk`, `adk-py`, `aither-adk`, `awdk`, `adk-serve`,
`adk-workspace`, `adk-bug`, `adk-shell` (all declared in `pyproject.toml`).

Layout: `adk/cli.py` (the CLI) · `server.py` · `agent.py` · `agent_loop.py` ·
`harnesses/` · `toolpacks/` · `faculties/` · `decisions/` · `shell/` · `node/` ·
`webui/packs/`.

## Build, test, lint

| command | what it asserts |
|---|---|
| `pytest` | `testpaths = ["tests"]`, asyncio mode `auto` |
| `ruff check .` | LOCAL config in `pyproject.toml`: line 100, target py312, `select = E/F/W/I`; `N` is deliberately off (camelCase wire fields) |
| `python scripts/check_exports.py` | ghost `__all__` entries; `__version__` vs `pyproject.toml` |
| `python scripts/check_moat_boundary.py` | inspects the BUILT wheel AND the sdist in `dist/` — run after `python -m build` |
| `python scripts/check_no_aither_clobber.py` | orphan `aither` console-script wrappers from older installs |

## Rules that keep it installable for strangers

- **Every `from lib.*` import lives inside `try/except ImportError` with a
  working fallback.** The top of `adk/agent_loop.py` is the canonical shape:
  it inserts the monorepo on `sys.path`, imports, and defines stub enums on
  failure. An unguarded monorepo import is a `ModuleNotFoundError` on every
  machine that ran `pip install awdk`.
- **`awgit`, `awgraph`, `awrelay` are hard runtime dependencies** because all
  three are published. Anything NOT on PyPI belongs in
  `[project.optional-dependencies]` or dev — never a hard dependency. A hard
  pin on an unpublished package breaks `pip install awdk` for everyone.
- **Read `pyproject.toml` before assuming a file ships.** The wheel `exclude`
  list strips `identities/*.yaml` (except `aither.yaml`), `nanogpt.py`,
  `formbridge/**`, `platform/memory/**`, `room_binaries/**`,
  `shell_binaries/**`; `artifacts` re-includes `toolpacks/**`,
  `webui/packs/**`, `ods/**` and `addon_manifests/*.yaml`. A file present in
  the source tree is not a file present in the wheel.
- **`CONTRIBUTING.md` is generated** — its header names the generator. Edit
  the generator, not the file; the next run overwrites a hand edit.
- **A CLI verb without an entry in `docs/CLI-REFERENCE.md` is invisible to
  users.** Add the verb and the reference in the same change.

## Read next

- `README.md` — start at the "Documentation map" section
- `docs/AGENT_DEV_GUIDE.md`
- `docs/CLI-REFERENCE.md`
- `adk/AGENT_PROMPT.md`
- `llms.txt`
