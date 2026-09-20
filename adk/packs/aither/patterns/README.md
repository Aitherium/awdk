# Bundled prompt patterns

A pattern is a directory holding `system.md` (the system prompt), optionally `user.md`
(prepended to the input) and `README.md`. The directory name is the pattern name.
Discovery order, first match wins: `AITHER_PATTERN_DIRS` (os.pathsep-separated), then
`~/.aither/patterns`, then this bundled directory. `adk patterns list` shows every
pattern and which directory it came from; `adk patterns run <name> --in FILE` applies one.
Import more with `adk patterns import <dir-or-fabric-clone> [--names a,b] [--overwrite]`;
a Fabric clone root is recognised and its `data/patterns` used. Each import writes a
`PROVENANCE.txt` (source path, sha256 of system.md, date) next to the copied files.
The directory-per-pattern shape follows Fabric (https://github.com/danielmiessler/Fabric,
MIT License, Daniel Miessler). The prompts here are original; nothing from Fabric ships.
