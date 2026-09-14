<!-- Every "aither-adk" below is deliberate and must survive any rename sweep:
     this is the redirect package for the OLD name. See pyproject.toml's header. -->
# aither-adk

**This package was renamed to [`awdk`](https://pypi.org/project/awdk/).**

`aither-adk` is now a thin alias that installs `awdk` and nothing else. It
exists so that existing installs, lockfiles and scripts keep working; it will
not be removed.

Nothing about your usage changes — the import name and every command are the
same:

```python
import adk
```

```bash
adk --help
adk-serve
adk-shell
```

New installs should prefer the new name:

```bash
pip install awdk
```
