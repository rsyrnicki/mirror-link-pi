"""Regression tests for scripts/prepare-sd.sh against realistic Pi OS layouts."""

from __future__ import annotations

import os
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
