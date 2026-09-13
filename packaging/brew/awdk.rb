# Homebrew formula for AitherADK — distributed via the Aitherium tap.
#
#   brew tap aitherium/tap && brew install awdk
#
# NOT homebrew-core: their acceptable-formulae policy requires an open-source
# license and this is Business Source License 1.1. A tap is the correct venue,
# not a fallback.
#
# The `resource` blocks below are the WHOLE dependency closure, generated from a
# resolved lockfile with each sdist's real PyPI sha256 — not just the direct
# deps. `virtualenv_install_with_resources` installs ONLY what is declared here,
# so a missing transitive dep is not a warning, it is an ImportError on first
# run. This formula shipped with 4 of 25 for months while looking complete.
# Regenerate with `brew update-python-resources AitherAdk` (macOS/Linux), or
# `uv pip compile` plus the PyPI JSON API.

# The class name is DERIVED FROM THE FILENAME by Homebrew: awdk.rb must
# declare `Awdk`. It said `AitherAdk` -- residue from the distribution
# rename -- so `brew install aitherium/tap/awdk` could not even LOAD the
# formula. That nobody ever hit it is the tell: this formula had never
# been installed by anyone.
#
# Re-applied 2026-08-23 after I reverted it myself: a later PR was built
# from a copy of develop taken BEFORE the rename landed, so it silently
# restored the broken name. awgit's shrink guard cannot see that class of
# overwrite -- nothing shrank, one line changed back -- so the only defence
# is re-reading the base immediately before staging, not once per session.
class Awdk < Formula
  include Language::Python::Virtualenv

  desc "Agent Development Kit for AitherOS — build AI agent fleets with any LLM"
  homepage "https://aitherium.com"
  url "https://files.pythonhosted.org/packages/source/a/awdk/awdk-3.8.17.tar.gz"
  sha256 "PLACEHOLDER_SHA256"
  # SPDX. "Proprietary" is not an SPDX identifier and `brew audit` rejects it;
  # the LICENSE file is Business Source License 1.1.
  license "BUSL-1.1"

  depends_on "python@3.12"

  resource "aiosqlite" do
    url "https://files.pythonhosted.org/packages/4e/8a/64761f4005f17809769d23e518d915db74e6310474e733e3593cfc854ef1/aiosqlite-0.22.1.tar.gz"
    sha256 "043e0bd78d32888c0a9ca90fc788b38796843360c855a7262a532813133a0650"
  end

  resource "annotated-doc" do
    url "https://files.pythonhosted.org/packages/5a/8e/38aa427ed5402449e226975b649c5dc73ccadfefeb95e6aecb8f8ea4b6b6/annotated_doc-0.0.5.tar.gz"
    sha256 "c7e58ce09192557605d8bbd92836d7e1d520ac9580096042c0bfd197efacf1bb"
  end

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/5f/56/a8120250d128bed162cd73c76d45f6ef9991f3e068f62a8ee060afa3104a/annotated_types-0.8.0.tar.gz"
    sha256 "13b2beaad985e05e2d6407ee4c4f35590b11f8d693a258a561055cac8f64cab7"
  end

  resource "anyio" do
    url "https://files.pythonhosted.org/packages/61/cc/a381afa6efea9f496eff839d4a6a1aed3bfafc7b3ab4b0d1b243a12573dd/anyio-4.14.2.tar.gz"
    sha256 "cfa139f3ed1a23ee8f88a145ddb5ac7605b8bbfd8592baacd7ce3d8bb4313c7f"
  end

  resource "awgit" do
    url "https://files.pythonhosted.org/packages/67/f1/805b7ca6e4b410f79621882719e1ea61315d63859bde63f6b517241c9dc9/awgit-1.10.0.tar.gz"
    sha256 "6f836369c9d0ce4e8e9f85a8827210854681e5cbb1131817641a3863f95fc333"
  end

  resource "awgraph" do
    url "https://files.pythonhosted.org/packages/7b/b3/8fd2c3f816b0d5b2949cde7345a80432076f9b61ac80faec9d7862014643/awgraph-1.4.2.tar.gz"
    sha256 "f3469859e77bbcd9b3b0db90c7b0dd7e51288dd33c18fef48d11e695efa02a95"
  end

  resource "awrelay" do
    url "https://files.pythonhosted.org/packages/ce/f1/043a284568254e22288103c26ac8798cca92c2165139b8ed77114027d01e/awrelay-0.3.1.tar.gz"
    sha256 "29acbe033474278d240f08eabc5fb2bf3aab7f68774b990571d3abd04688b419"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/a3/c2/24167ea9858356b47a87a50d39908bfdb72ceeefe0041586e704e5376b3a/certifi-2026.7.22.tar.gz"
    sha256 "741e2c3b351ddf169a738da9f2c048608ff7f2c5cc02f1ebc6b118bb090d5d55"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/76/d4/81420972a676e8ffea40450d8c8c92943e7218a78fe9b64359836cc9876b/click-8.4.2.tar.gz"
    sha256 "9a6cea6e60b17ebe0a44c5cc636d94f09bd66142c1cd7d8b4cd731c4917a15f6"
  end

  resource "cryptography" do
    url "https://files.pythonhosted.org/packages/bb/ad/5d6702db60b1e40b41ef513b6967ff5848f307d50f8449baf1634f5908f1/cryptography-50.0.1.tar.gz"
    sha256 "5dd9bda1c12b4162f6ff568eeb5e0ff956c28d14406e875cfe8a63a2d414ff20"
  end

  resource "ddgs" do
    url "https://files.pythonhosted.org/packages/19/b6/b7f3118a3b0a285f54fb4e1289c39e3cba1944066828aa04af9cf80f878e/ddgs-9.15.0.tar.gz"
    sha256 "12c4148da66525031214279d3ecb5170778a484f2616cac12667d0824818d07d"
  end

  resource "fastapi" do
    url "https://files.pythonhosted.org/packages/8a/02/91e3416a8fdd715abb903a952a6bec7cdd8d14eed55d415fc8595524c319/fastapi-0.141.1.tar.gz"
    sha256 "e8822fc40db1e1858054d7a949a888695bc9bdce70139178e33bd2871a453ca1"
  end

  resource "h11" do
    url "https://files.pythonhosted.org/packages/01/ee/02a2c011bdab74c6fb3c75474d40b3052059d95df7e73351460c8588d963/h11-0.16.0.tar.gz"
    sha256 "4e35b956cf45792e4caa5885e69fba00bdbc6ffafbfa020300e549b208ee5ff1"
  end

  resource "httpcore" do
    url "https://files.pythonhosted.org/packages/06/94/82699a10bca87a5556c9c59b5963f2d039dbd239f25bc2a63907a05a14cb/httpcore-1.0.9.tar.gz"
    sha256 "6e34463af53fd2ab5d807f399a9b45ea31c3dfa2276f15a2c3f00afff6e176e8"
  end

  resource "httptools" do
    url "https://files.pythonhosted.org/packages/43/e5/d471fcb0e14523fe1c3f4ba58ca52480e7bd70ad7109a3846bc75892f7fb/httptools-0.8.0.tar.gz"
    sha256 "6b2a32f18d97e16e90827d7a819ffa8dbd8cc245fc4e1fa9d1095b54ef4bd999"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/b1/df/48c586a5fe32a0f01324ee087459e112ebb7224f646c0b5023f5e79e9956/httpx-0.28.1.tar.gz"
    sha256 "75e98c5f16b0f35b567856f597f06ff2270a374470a5c2392242528e3e3e42fc"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/cd/63/9496c57188a2ee585e0f1db071d75089a11e98aa86eb99d9d7618fc1edce/idna-3.18.tar.gz"
    sha256 "ffb385a7e039654cef1ab9ef32c6fafe283c0c0467bba1d9029738ce4a14a848"
  end

  resource "jinja2" do
    url "https://files.pythonhosted.org/packages/df/bf/f7da0350254c0ed7c72f3e33cef02e048281fec7ecec5f032d4aac52226b/jinja2-3.1.6.tar.gz"
    sha256 "0137fb05990d35f1275a587e9aee6d56da821fc83491a0fb838183be43f66d6d"
  end

  resource "markupsafe" do
    url "https://files.pythonhosted.org/packages/7e/99/7690b6d4034fffd95959cbe0c02de8deb3098cc577c67bb6a24fe5d7caa7/markupsafe-3.0.3.tar.gz"
    sha256 "722695808f4b6457b320fdc131280796bdceb04ab50fe1795cd540799ebe1698"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/18/a5/b60d21ac674192f8ab0ba4e9fd860690f9b4a6e51ca5df118733b487d8d6/pydantic-2.13.4.tar.gz"
    sha256 "c40756b57adaa8b1efeeced5c196f3f3b7c435f90e84ea7f443901bec8099ef6"
  end

  resource "pydantic-core" do
    url "https://files.pythonhosted.org/packages/9d/56/921726b776ace8d8f5db44c4ef961006580d91dc52b803c489fafd1aa249/pydantic_core-2.46.4.tar.gz"
    sha256 "62f875393d7f270851f20523dd2e29f082bcc82292d66db2b64ea71f64b6e1c1"
  end

  resource "python-dotenv" do
    url "https://files.pythonhosted.org/packages/82/ed/0301aeeac3e5353ef3d94b6ec08bbcabd04a72018415dcb29e588514bba8/python_dotenv-1.2.2.tar.gz"
    sha256 "2c371a91fbd7ba082c2c1dc1f8bf89ca22564a087c2c287cd9b662adde799cf3"
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  resource "qrcode" do
    url "https://files.pythonhosted.org/packages/8f/b2/7fc2931bfae0af02d5f53b174e9cf701adbb35f39d69c2af63d4a39f81a9/qrcode-8.2.tar.gz"
    sha256 "35c3f2a4172b33136ab9f6b3ef1c00260dd2f66f858f24d88418a015f446506c"
  end

  resource "starlette" do
    url "https://files.pythonhosted.org/packages/0f/3c/76d2fd1f1357ed0f0108d8a5aa233dcf16e2946a8559c84912fe08e01ac7/starlette-1.4.1.tar.gz"
    sha256 "b7332de6e9375593a29ba9eee1e6ecfeb3eb2043e2e19a13b4b71da73ff35540"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz"
    sha256 "dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5"
  end

  resource "typing-inspection" do
    url "https://files.pythonhosted.org/packages/55/e3/70399cb7dd41c10ac53367ae42139cf4b1ca5f36bb3dc6c9d33acdb43655/typing_inspection-0.4.2.tar.gz"
    sha256 "ba561c48a67c5958007083d386c3295464928b01faa735ab8547c5692e87f464"
  end

  resource "uvicorn" do
    url "https://files.pythonhosted.org/packages/03/18/ccce41535dee1be77735592bd19965f3972c82e07ee703d324709496b716/uvicorn-0.52.1.tar.gz"
    sha256 "112ec661814189acbccd3f7b86460147cc065fc92c0821afa78918780e4354dd"
  end

  resource "watchfiles" do
    url "https://files.pythonhosted.org/packages/cd/41/5e1a4bb12aac5f1493fa1bdc11154eca3b258ca4eba65d39c473fe19d8e9/watchfiles-1.2.0.tar.gz"
    sha256 "c995fba777f1ea992f090f9236e9284cf7a5d1a0130dd5a3d82c598cacd76838"
  end

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/f7/96/e01084f83a64bcb3a27994bd0cb0db68ff29d9c6707fae37ec19b18ba990/websockets-17.0.1.tar.gz"
    sha256 "5baa9bc0dfbae8c507e51c8cf1b6d4628086f7a87bbd3a9952bd5f035451f1cc"
  end

  def install
    virtualenv_install_with_resources
  end

  def post_install
    (var/"aither").mkpath
  end

  test do
    # `bin/aither` does NOT exist — the old test asserted a command that is not
    # one of our console scripts (adk, adk-py, awdk, adk-serve,
    # adk-workspace, adk-bug, adk-shell), so `brew test` could only ever fail.
    # Same class as the onboarding-funnel rule: never advertise a command the
    # package does not ship.
    # NOT `adk --version` -- there is no such flag and no `version`
    # subcommand (measured: both exit 2). The comment below this block warns
    # against asserting a command the package does not ship, and the previous
    # line did exactly that. `--help` proves the console script resolves, which
    # is what a formula test is for; the version is read where it actually
    # lives.
    assert_match "usage", shell_output("#{bin}/adk --help 2>&1")
    assert_match version.to_s,
                 shell_output("#{libexec}/bin/python -c 'import adk; print(adk.__version__)'")
    # The uvx/registry entrypoint must exist too — `uvx aither-adk` and the ACP
    # registry listing both invoke it by that exact name.
    assert_predicate bin/"awdk", :exist?
  end
end
