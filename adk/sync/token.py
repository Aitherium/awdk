"""Print the portal bearer: a credential helper for tools that sync as you.

``python -m adk.sync.token`` prints the token ``adk login`` stored (or the one in
the environment) and exits 0, or prints nothing and exits 1 when signed out.
awsettings runs it as its ``token_command``, so settings sync always uses the
current login and the token is never copied into another file.
"""

from __future__ import annotations

import sys


def main() -> int:
    from adk.sync.settings import _resolve_token

    token = _resolve_token()
    if not token:
        print("not signed in: run `adk login`", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
