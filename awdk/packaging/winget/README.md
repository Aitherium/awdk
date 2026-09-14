# winget manifests

Submitted to [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs) at
`manifests/a/Aitherium/AitherShell/<version>/`.

## Why AitherShell and not awdk

**winget installs executables — MSI, EXE, MSIX, zip. It does not install pip
packages.** `Aitherium.ADK.yaml` used to live here, pointing at
`https://aitherium.com/download/aither-adk-3.0.5-win64.exe`, which **404s**, for
a product that has no Windows binary at all. It was never submitted and could
never have been: winget's validator downloads the installer and hash-checks it.

`awdk` is distributed on PyPI (`pip install awdk` / `uvx awdk`)
and through the Homebrew tap. AitherShell is the product with a real `.exe`, so
it is the one with a winget manifest.

## The three files

Modern winget requires a **multi-file** manifest; the single-file "singleton"
form is deprecated. All three carry the same `PackageIdentifier` and
`PackageVersion`, and the folder path must match the identifier exactly.

| file | holds |
|---|---|
| `Aitherium.AitherShell.yaml` | version + which locale is default |
| `Aitherium.AitherShell.installer.yaml` | architecture, URL, **sha256** |
| `Aitherium.AitherShell.locale.en-US.yaml` | publisher, description, tags |

## Updating for a new release

1. `release-aithershell.yml` mirrors the binaries to the **public** repo —
   `Aitherium/awdk` releases, tag `shell-v<version>`. That public URL is
   the whole reason this is submittable: winget's validator fetches the
   installer with **no credentials**, so a private-repo asset returns 403 and
   the submission fails.
2. Compute the sha256 from the **downloaded artifact**, never from a build log:
   ```bash
   curl -fsSL -o s.exe \
     https://github.com/Aitherium/awdk/releases/download/shell-v<v>/aither-shell-win64.exe
   sha256sum s.exe | tr 'a-f' 'A-F'
   ```
3. Bump `PackageVersion` in all three files, and `InstallerUrl` +
   `InstallerSha256` in the installer manifest.
4. Copy into a fork of winget-pkgs at
   `manifests/a/Aitherium/AitherShell/<version>/` and open a PR.

`ReleaseDate` is quoted deliberately — unquoted, YAML parses it as a date and a
strict schema check rejects it for not being a string.
