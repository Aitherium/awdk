"""Enable `python -m adk ...` (mirrors the `adk` console-script entry point).

Also lets the fleet manager's local runtime spawn agents via `python -m adk run`
without depending on the console script being on PATH.
"""

from adk.cli import main

if __name__ == "__main__":
    main()
