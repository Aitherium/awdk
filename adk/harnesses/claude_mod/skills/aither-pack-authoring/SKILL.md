---
name: aither-pack-authoring
description: >-
  Build a tool pack for awdk agents: scaffold it with `adk pack new`, write a tool the model can call, then validate, load-test and bundle it with `adk pack validate`, `adk pack dev` and `adk pack build`. Explains the `.toolpack.yaml` fields the loader actually reads, the PKA001-PKA009 checks, the reserved namespaces a community pack may not claim, and why `validate` imports nothing while `dev` does.
---
<!-- Generated from the awskills pack (skills/aither-pack-authoring.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# aither-pack-authoring — a tool pack an adk agent can install

A pack is a folder with a manifest, an entry point that registers tools, and a test.
Four verbs carry the whole author loop — scaffold, validate, load, bundle:

```bash
adk pack new yourname.weather --dest packs --author "Your Name"
adk pack validate packs/yourname.weather    # static; imports nothing
adk pack dev packs/yourname.weather         # loads through the REAL loader, lists tools
adk pack build packs/yourname.weather       # .tar.gz + .sha256 in dist/
```

The scaffold writes `.toolpack.yaml`, `__init__.py`, `tools.py`, `tests/test_pack.py`
and `README.md`, and it works before you change a line.

## The manifest the loader reads

```yaml
id: yourname.weather        # ^[a-z][a-z0-9._-]{2,63}$
name: Weather
version: 0.1.0              # semver; bumped by you, never generated
description: >-
  One sentence saying what an agent can do once this pack is installed.
category: tool_packs
tier: community             # community | free | builder | professional | enterprise
author: Your Name
tags: [community]
tool_modules: []            # empty -> the loader file-loads __init__.py and calls register()
entitlements: []            # capabilities gated behind a listing, if any
```

`id` must not claim a first-party namespace: `aither`, `aitherium`, `adk` and `awdk` are
reserved, so `weather` becomes `yourname.weather`. Discovery is first-directory-wins, so a
community pack under a first-party id would shadow the shipped one.

## The tool the model sees

The docstring and the type hints **are** the schema. Write them for the model: say what
the tool returns and when to call it.

```python
def weather_now(city: str) -> str:
    """Current conditions for `city`, as one line. Use when the user asks about
    today's weather; takes a city name, returns text, never raises for a bad city."""
    ...
```

`__init__.py` exposes `register(registry) -> int` and returns the number of tools it
registered. The scaffold guards each registration separately — one bad tool must not
sink the pack. Imports that read a key at call time should fail soft with guidance; a
key never goes in the folder (PKA008 scans for secret shapes and reports file and line
only).

## What each verb actually checks

| verb | imports the pack? | fails on |
|---|---|---|
| `validate` | no | PKA001 manifest parses · PKA002 id shape · PKA003 reserved namespace · PKA004 semver + name + description · PKA005 an entry point exposing `register()` · PKA006 declared ui/persona files exist · PKA007 symlinks · PKA008 secret-shaped strings · PKA009 unknown tier |
| `dev` | yes | the registry cannot load the pack, or a declared tool is missing |
| `build` | no | it re-runs validation, then writes a reproducible bundle |

`validate` is deliberately static: running the code under test would be unsafe on
someone else's pack. `dev` is the one verb that executes it, and it says so.

## Ship it

```bash
adk pack build .                    # dist/<id>-<version>.tar.gz and .sha256
adk pack install ./dist/yourname.weather-0.1.0.tar.gz
adk pack enable yourname.weather
```

To publish to the community marketplace, `adk pack publish` builds, submits and uploads —
that path needs an account and is not part of the local loop. For a local or fleet
install the tarball above is enough.

**Check:** `adk pack dev .` lists your tool, and after install `adk tools list` shows it
under the pack's namespace.

## Exit codes

`0` clean · `1` a finding (the code and message say which) · `2` could not judge (no
manifest, PyYAML absent, not a directory). In CI, treat `2` as a failure — a check that
cannot run must not pass.
