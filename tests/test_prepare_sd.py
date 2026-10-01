"""Regression tests for scripts/prepare-sd.sh against realistic Pi OS layouts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "prepare-sd.sh"

# The stock Raspberry Pi OS (Trixie) config.txt: note the model-specific dwc2 line
# under [cm5], which must NOT be mistaken for the Pi Zero's peripheral overlay.
STOCK_CONFIG = """\
dtparam=audio=on
dtoverlay=vc4-kms-v3d
arm_64bit=1

[cm4]
otg_mode=1

[cm5]
dtoverlay=dwc2,dr_mode=host

[pi5]
dtoverlay=nospi10

[all]
"""


def _fake_card(tmp_path: Path, config_txt: str) -> tuple[Path, Path]:
    boot = tmp_path / "boot"
    root = tmp_path / "root"
    (boot).mkdir()
    (root / "usr/bin").mkdir(parents=True)
    (root / "etc").mkdir(parents=True)
    (boot / "config.txt").write_text(config_txt)
    (boot / "cmdline.txt").write_text("console=serial0\n")
    (root / "etc/os-release").write_text(
        'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nVERSION_CODENAME=trixie\n')
    py = root / "usr/bin/python3.13"
    py.write_text("")
    os.symlink("python3.13", root / "usr/bin/python3")
    (root / "etc/passwd").write_text("daniel:x:1000:1000::/home/daniel:/bin/bash\n")
    return boot, root


def _run(boot: Path, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), "--boot", str(boot), "--root", str(root)],
                          capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(not SCRIPT.exists(), reason="prepare-sd.sh missing")
def test_adds_peripheral_overlay_despite_stock_cm5_dwc2_line(tmp_path):
    boot, root = _fake_card(tmp_path, STOCK_CONFIG)
    assert _run(boot, root).returncode == 0
    config = (boot / "config.txt").read_text()
    # The Pi Zero's controller needs an [all]-scoped peripheral overlay.
    assert "\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n" in config
    # And the stock host line must be left untouched.
    assert "dtoverlay=dwc2,dr_mode=host" in config


@pytest.mark.skipif(not SCRIPT.exists(), reason="prepare-sd.sh missing")
def test_peripheral_overlay_added_exactly_once_on_reruns(tmp_path):
    boot, root = _fake_card(tmp_path, STOCK_CONFIG)
    _run(boot, root)
    _run(boot, root)
    config = (boot / "config.txt").read_text()
    assert config.count("dtoverlay=dwc2,dr_mode=peripheral") == 1


@pytest.mark.skipif(not SCRIPT.exists(), reason="prepare-sd.sh missing")
def test_gadget_files_installed(tmp_path):
    boot, root = _fake_card(tmp_path, STOCK_CONFIG)
    _run(boot, root)
    assert (root / "opt/mlpi/src/mlpi/gadget.py").is_file()
    assert (root / "etc/systemd/system/multi-user.target.wants/mlpi.target").is_symlink()
    assert (boot / "mlpi.toml").is_file()


def _run_phone(boot: Path, root: Path, keydir: Path, jar: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, MLPI_PHONE_SKIP_PACKAGES="1", MLPI_SCRCPY_SERVER=str(jar),
               MLPI_ADB_KEYDIR=str(keydir))
    return subprocess.run(["bash", str(SCRIPT), "--phone", "--boot", str(boot), "--root",
                           str(root)], capture_output=True, text=True, timeout=120, env=env)


@pytest.mark.skipif(not SCRIPT.exists(), reason="prepare-sd.sh missing")
def test_phone_mode_installs_server_key_and_enables_hotspot_once(tmp_path):
    boot, root = _fake_card(tmp_path, STOCK_CONFIG)
    keydir = tmp_path / "keys"
    keydir.mkdir()
    (keydir / "adbkey").write_text("-----BEGIN PRIVATE KEY-----\n")
    (keydir / "adbkey.pub").write_text("QAAAA... mlpi\n")
    jar = tmp_path / "scrcpy-server"
    jar.write_bytes(b"PK\x03\x04jar")
    out = _run_phone(boot, root, keydir, jar)
    assert out.returncode == 0, out.stderr
    assert (root / "opt/mlpi/vendor/scrcpy-server").read_bytes() == b"PK\x03\x04jar"
    key = root / "var/lib/mlpi/adb/.android/adbkey"
    assert key.read_text().startswith("-----BEGIN") and oct(key.stat().st_mode)[-3:] == "600"
    import tomllib
    toml = (boot / "mlpi.toml").read_text()
    assert "[phone]\nenabled = true" in toml
    password = tomllib.loads(toml)["phone"]["wifi_password"]
    assert len(password) >= 8 and password in out.stdout
    # Re-running keeps the password (the phone remembers it) and doesn't duplicate.
    _run_phone(boot, root, keydir, jar)
    toml2 = (boot / "mlpi.toml").read_text()
    assert toml2.count("\n[phone]\n") == 1 and password in toml2
    # The generated file is valid TOML that enables phone mode.
    assert tomllib.loads(toml2)["phone"]["enabled"] is True


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs ssh-keygen")
def test_ssh_option_installs_key_and_enables_sshd(tmp_path):
    boot, root = _fake_card(tmp_path, STOCK_CONFIG)
    (root / "usr/lib/systemd/system").mkdir(parents=True)
    (root / "usr/lib/systemd/system/ssh.service").write_text("[Unit]\n")
    keydir = tmp_path / "keys"
    env = dict(os.environ, MLPI_SSH_KEYDIR=str(keydir))
    out = subprocess.run(["bash", str(SCRIPT), "--ssh", "--boot", str(boot), "--root", str(root)],
                         capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stderr
    pub = (keydir / "id_ed25519.pub").read_text()
    assert (root / "etc/mlpi/authorized_keys").read_text() == pub
    conf = (root / "etc/ssh/sshd_config.d/mlpi.conf").read_text()
    assert "AuthorizedKeysFile .ssh/authorized_keys /etc/mlpi/authorized_keys" in conf
    assert "PasswordAuthentication no" in conf
    link = root / "etc/systemd/system/multi-user.target.wants/ssh.service"
    assert os.readlink(link) == "/usr/lib/systemd/system/ssh.service"


def test_apply_update_keeps_vendor_and_installs_units(tmp_path):
    opt, sysd, src = tmp_path / "opt", tmp_path / "systemd", tmp_path / "update"
    (opt / "vendor").mkdir(parents=True)
    (opt / "vendor/scrcpy-server").write_text("jar")
    (opt / "src").mkdir()
    (opt / "src/old.py").write_text("old")
    sysd.mkdir()
    (src / "src").mkdir(parents=True)
    (src / "src/new.py").write_text("new")
    (src / "systemd").mkdir()
    (src / "systemd/mlpi.service").write_text("[Unit]\n")
    (src / "systemd/mlpi.target").write_text("[Unit]\n")
    toml = tmp_path / "mlpi.toml"
    toml.write_text("[led]\nenabled = true\n")         # not phone mode: no package check
    calls = tmp_path / "calls"
    fake = tmp_path / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)
    env = dict(os.environ, MLPI_OPT=str(opt), MLPI_SYSTEMD_DIR=str(sysd), MLPI_TOML=str(toml),
               MLPI_SYSTEMCTL=str(fake))
    out = subprocess.run(["bash", str(REPO / "scripts/apply-update.sh"), str(src), "0",
                          "prepared: now", "git: abc"], capture_output=True, text=True,
                         timeout=60, env=env)
    assert out.returncode == 0, out.stderr
    assert (opt / "src/new.py").exists() and not (opt / "src/old.py").exists()
    assert (opt / "vendor/scrcpy-server").read_text() == "jar"
    assert (opt / "VERSION").read_text() == "prepared: now\ngit: abc\n"
    assert (sysd / "mlpi.service").exists() and not src.exists()
    assert "NOTE" not in out.stdout
    assert calls.read_text().split() == ["daemon-reload", "restart", "mlpi.target"]
