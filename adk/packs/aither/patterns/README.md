# Bundled prompt patterns

A pattern is a directory holding `system.md` (the system prompt), optionally `user.md`
(prepended to the input) and `README.md`. The directory name is the pattern name.
Discovery order, first match wins: `AITHER_PATTERN_DIRS` (os.pathsep-separated), then
`~/.aither/patterns`, then this bundled directory. `adk patterns list` shows every
pattern and which directory it came from; `adk patterns run <name> --in FILE` applies one.
Import more with `adk patterns import <dir-or-fabric-clone> [--names a,b] [--overwrite]`;
a Fabric clone root is recognised and its `data/patterns` used. Each import writes a
`PROVENANCE.txt` (source path, sha256 of system.md, date) next to the copied files.
Template variables: `adk patterns run <name> --var lang_code=fr` replaces `{{lang_code}}` in
system.md/user.md (Fabric's `-v=#lang_code:fr`); placeholders without a value stay verbatim.
Pattern names are single directory names; a path-like name (`../x`) is refused. Measured
2026-10-23: a full Fabric clone (265 patterns) imports and loads 265/265.
The directory-per-pattern shape follows Fabric (https://github.com/danielmiessler/Fabric,
MIT License, Daniel Miessler). The prompts here are original; nothing from Fabric ships.
