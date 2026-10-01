---
name: awrepl
description: "A persistent Python REPL an agent can keep poking at across calls. Use to inspect live objects instead of re-running a script and guessing from memory."
---

# awrepl — a REPL that keeps its state

```bash
awrepl run "items = [1, 2, 3]"                 # execute once, print result
awrepl --session s1 run "x = load()"           # session: state survives between calls
awrepl --session s1 run "print(len(x))"
awrepl --json run "1 + 1"                      # structured result
awrepl serve                                   # interactive loop
awrepl --self-test
```

Library form: `from awrepl import ReplSession; s = ReplSession("agent-1"); s.execute(code)`.

**Exit codes:** 0 code ran · non-zero when the code raised (`--traceback` for the
full trace).

**Trap:** flags `--session/--json/--traceback` are top-level — put them BEFORE `run`.
Without `--session`, each `run` is a fresh namespace.
