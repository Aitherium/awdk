"""The deploy/setup hints point at podman (and NVIDIA's rpm/CDI path), not Docker Desktop."""

from adk import deploy


def _joined(lines):
    return "\n".join(lines)


def test_rhel_family_gets_dnf_podman_and_cdi(monkeypatch):
    monkeypatch.setattr(deploy.Path, "exists", lambda self: False)
    text = _joined(deploy.container_engine_install_hint(
        "Linux", 'NAME="CentOS Stream"\nID="centos"\nID_LIKE="rhel fedora"\n'))
    assert "dnf install -y podman" in text
    assert "nvidia-container-toolkit" in text and "cdi generate" in text
    assert "docker-desktop" not in text


def test_awnix_is_already_provisioned():
    text = _joined(deploy.container_engine_install_hint(
        "Linux", 'NAME="awnix"\nID="centos"\n'))
    assert "part of the image" in text


def test_debian_family_gets_apt_podman(monkeypatch):
    monkeypatch.setattr(deploy.Path, "exists", lambda self: False)
    text = _joined(deploy.container_engine_install_hint("Linux", 'ID=ubuntu\nID_LIKE=debian\n'))
    assert "apt-get install -y podman" in text


def test_windows_and_macos_never_say_docker_desktop():
    for system in ("Windows", "Darwin"):
        text = _joined(deploy.container_engine_install_hint(system, ""))
        assert "docker.com" not in text
        assert "podman" in text or "awnix" in text
