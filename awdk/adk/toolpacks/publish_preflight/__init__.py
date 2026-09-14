"""Publish preflight tool pack — register publish_* tools on an adk agent.

Answers "why did my package publish fail?" when the error names the wrong thing.

The pack exists because of a specific, expensive shape. One package, one commit,
NINE upload attempts, and five different errors -- of which four were about the
MACHINE the job happened to land on and one was about the account:

    Read version    ModuleNotFoundError: tomllib
    Tooling         no such option: --break-system-packages
    Install wheel   Package 'x' requires a different Python: 3.9.25
    Install wheel   ModuleNotFoundError: No module named 'x'
    Upload          429 Too many new projects created

Nothing regressed between them. A build fleet is rarely uniform, so a green
publish is evidence about the runner that took the job, not about the lane. Each
failure was fixed in turn, and each fix revealed the next one underneath.

Two of those are worth naming because they are silent until the very last step:

  * a wheel whose DISTRIBUTION name and IMPORT name differ installs fine and
    fails on `import`. Tests do not catch it -- they import from the source tree
    by its on-disk name and never touch the installed artifact.
  * `requires-python` is enforced at INSTALL, not at build. An interpreter too
    old to install the wheel will still build it, run the tests, and pass every
    step before the one that matters.

So the tools here verify the ARTIFACT rather than the tree, choose an
interpreter rather than assuming one, and translate a publish error into the
cause rather than the symptom.

Read-only. Nothing here uploads anything; `publish_preflight` is what you run
BEFORE the thing that does.
"""

from .tools import TOOLS  # noqa: F401
