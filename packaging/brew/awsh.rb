# Homebrew formula for awsh (AitherShell CLI) -- distributed via the Aitherium tap.
#
#   brew tap aitherium/tap && brew install awsh
#
# A BINARY formula, unlike awdk.rb: awsh is a bun-compiled single executable, so
# there is nothing to build and no resource closure. The URLs are the public
# mirror that release-aithershell.yml writes to (Aitherium/awdk releases, tag
# `shell-v<version>`) -- a private-repo asset 403s for an unauthenticated
# `brew install`, exactly as it does for the winget validator.
#
# Updating for a new release (test_awsh_packaging_manifests.py asserts the
# shape, including that every sha256 is real and names a released asset):
#   1. bump `version`
#   2. copy each sha256 from the release's SHA256SUMS.txt -- never from a build
#      log; the digest must be of the bytes users download:
#        curl -fsSL https://github.com/Aitherium/awdk/releases/download/shell-v<v>/SHA256SUMS.txt
#   3. keep conda/awsh/meta.yaml on the same version and digests.
#
# Only two platforms have a released binary (release-aithershell.yml builds
# linux-x64, macos-arm64, win64). Intel macOS and Linux arm64 have no asset, so
# the formula declares none for them rather than pointing at a 404.
class Awsh < Formula
  desc "Your terminal answers you: ask a question where a command would go"
  homepage "https://aitherium.com"
  version "1.17.0"
  # SPDX identifier; the package LICENSE is Business Source License 1.1.
  license "BUSL-1.1"

  on_macos do
    on_arm do
      url "https://github.com/Aitherium/awdk/releases/download/shell-v#{version}/aither-shell-macos-arm64"
      sha256 "ae5eba5ca1c344cfa55913c03bd893f2761461db40ca425b2c0a8cfdc87d19a9"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/Aitherium/awdk/releases/download/shell-v#{version}/aither-shell-linux-x64"
      sha256 "af0e3c756730993d3c605fdaa7b6aa126f31ccd3ca64ea5eac428c29c73266b4"
    end
  end

  def install
    # The downloaded asset keeps its release name; install it under the
    # package's primary command and add the two aliases package.json also
    # ships (`aither`, `aither-shell`) so every documented spelling resolves.
    asset = Dir["aither-shell-*"].first
    odie "no aither-shell-* binary in the download" if asset.nil?
    bin.install asset => "awsh"
    bin.install_symlink "awsh" => "aither"
    bin.install_symlink "awsh" => "aither-shell"
  end

  test do
    # `--version` is handled before config load and network (main.ts), so it
    # is safe in the sandbox; it prints "awsh <version>".
    assert_match version.to_s, shell_output("#{bin}/awsh --version")
    assert_predicate bin/"aither", :exist?
  end
end
