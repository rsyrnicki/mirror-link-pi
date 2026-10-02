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


def _loop_card(tmp_path):
    """A freshly flashed card as a loop device: p1 (boot), p2 (root fs), free rest."""
    if os.geteuid() != 0 or not all(shutil.which(t) for t in
                                    ("losetup", "sfdisk", "partx", "mkfs.ext4", "resize2fs")):
        pytest.skip("needs root, losetup, sfdisk, partx and e2fsprogs")
    img = tmp_path / "card.img"
    with open(img, "wb") as fh:
        fh.truncate(1024 * 1024 * 1024)
    subprocess.run(["sfdisk", "-q", str(img)], input="8192,131072,c\n139264,614400,83\n",
                   text=True, check=True)
    out = subprocess.run(["losetup", "-f", "--show", "-P", str(img)], capture_output=True,
                         text=True)
    if out.returncode:
        pytest.skip(f"no loop device: {out.stderr.strip()}")
    dev = out.stdout.strip()
    subprocess.run(["partx", "-a", dev], capture_output=True)
    for n in (1, 2):                     # containers have no udev to create the nodes
        node = f"{dev}p{n}"
        if not os.path.exists(node):
            major, minor = Path(f"/sys/class/block/{Path(node).name}/dev").read_text().split(":")
            os.mknod(node, 0o600 | 0o060000, os.makedev(int(major), int(minor)))
    subprocess.run(["mkfs.ext4", "-q", "-L", "rootfs", f"{dev}p2"], check=True)
    return dev


def test_data_partition_grows_root_and_adds_mlpi_data(tmp_path):
    dev = _loop_card(tmp_path)
    try:
        env = dict(os.environ, MLPI_DATA_SIZE_MB="128")
        script = REPO / "scripts" / "make-data-partition.sh"
        out = subprocess.run([str(script), dev, f"{dev}p2"], capture_output=True, text=True,
                             env=env, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == f"{dev}p3"
        dump = subprocess.run(["sfdisk", "-d", dev], capture_output=True, text=True).stdout
        assert "size=     1695744" in dump and "start=     1835008" in dump
        label = subprocess.run(["blkid", "-o", "value", "-s", "LABEL", f"{dev}p3"],
                               capture_output=True, text=True).stdout.strip()
        assert label == "mlpi-data"
        assert subprocess.run(["e2fsck", "-fn", f"{dev}p2"], capture_output=True).returncode == 0
        again = subprocess.run([str(script), dev, f"{dev}p2"], capture_output=True, text=True,
                               env=env, timeout=60)
        assert again.returncode != 0 and "freshly flashed" in again.stderr   # never twice

        # prepare-sd's part: fstab, volatile journal, no first-boot resize, state copied
        boot, root = _fake_card(tmp_path, STOCK_CONFIG)
        (boot / "cmdline.txt").write_text("console=serial0 root=PARTUUID=abc-02 "
                                          "init=/usr/lib/raspberrypi-sys-mods/firstboot quiet\n")
        (root / "etc/fstab").write_text("PARTUUID=abc-02 / ext4 defaults 0 1\n")
        (root / "var/lib/mlpi/adb").mkdir(parents=True)
        (root / "var/lib/mlpi/adb/adbkey").write_text("key")
        env = dict(os.environ, MLPI_DATA_PART=f"{dev}p3")
        out = subprocess.run(["bash", str(SCRIPT), "--boot", str(boot), "--root", str(root)],
                             capture_output=True, text=True, timeout=120, env=env)
        assert out.returncode == 0, out.stderr
        fstab = (root / "etc/fstab").read_text()
        assert "LABEL=mlpi-data  /var/lib/mlpi  ext4" in fstab and "nofail" in fstab
        assert "Storage=volatile" in (root / "etc/systemd/journald.conf.d/mlpi.conf").read_text()
        assert "firstboot" not in (boot / "cmdline.txt").read_text()
        mnt = tmp_path / "data"
        mnt.mkdir()
        subprocess.run(["mount", f"{dev}p3", str(mnt)], check=True)
        try:
            assert (mnt / "adb/adbkey").read_text() == "key"
        finally:
            subprocess.run(["umount", str(mnt)])
    finally:
        subprocess.run(["losetup", "-d", dev])


TRIXIE_PASSWD = "root:x:0:0:root:/root:/bin/bash\npi:x:1000:1000::/home/pi:/usr/sbin/nologin\n"
TRIXIE_SHADOW = "root:*:20711:0:99999:7:::\npi:!:20711:0:99999:7:::\n"
TRIXIE_GROUP = "adm:x:4:pi\nsudo:x:27:pi\nvideo:x:44:pi\npi:x:1000:\n"
TRIXIE_GSHADOW = "adm:*::pi\nsudo:*::pi\nvideo:*::pi\npi:!::\n"


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_login_is_set_up_offline_instead_of_the_first_boot_wizard(tmp_path):
    boot, root = _fake_card(tmp_path, STOCK_CONFIG)
    etc = root / "etc"
    (etc / "passwd").write_text(TRIXIE_PASSWD)
    (etc / "shadow").write_text(TRIXIE_SHADOW)
    (etc / "group").write_text(TRIXIE_GROUP)
    (etc / "gshadow").write_text(TRIXIE_GSHADOW)
    (etc / "subuid").write_text("pi:100000:65536\n")
    (etc / "hostname").write_text("raspberrypi\n")
    (etc / "hosts").write_text("127.0.0.1\tlocalhost\n127.0.1.1\t\traspberrypi\n")
    (etc / "cloud").mkdir()
    wants = etc / "systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "userconfig.service").symlink_to("/usr/lib/systemd/system/userconfig.service")
    (root / "home/pi").mkdir(parents=True)
    (boot / "userconf.txt").write_text("someone:$6$x\n")
    assert _run(boot, root).returncode == 0

    assert (etc / "passwd").read_text().splitlines()[1] == \
        "mlpi:x:1000:1000::/home/mlpi:/bin/bash"
    user, hash_ = (etc / "shadow").read_text().splitlines()[1].split(":")[:2]
    salt = hash_.split("$")[2]
    check = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "mlpi"],
                           capture_output=True, text=True).stdout.strip()
    assert user == "mlpi" and check == hash_
    assert (etc / "group").read_text() == TRIXIE_GROUP.replace("pi", "mlpi")
    assert (etc / "gshadow").read_text().splitlines()[1] == "sudo:*::mlpi"
    assert (etc / "subuid").read_text() == "mlpi:100000:65536\n"
    assert (root / "home/mlpi").is_dir() and not (root / "home/pi").exists()
    assert (etc / "sudoers.d/010_mlpi-nopasswd").read_text() == "mlpi ALL=(ALL) NOPASSWD: ALL\n"
    assert not (wants / "userconfig.service").exists()                  # no wizard
    assert (etc / "systemd/system/getty.target.wants/getty@tty1.service").is_symlink()
    assert (etc / "cloud/cloud-init.disabled").exists()
    assert not (boot / "userconf.txt").exists()
    assert (etc / "hostname").read_text() == "mlpi\n"
    assert "127.0.1.1\t\tmlpi" in (etc / "hosts").read_text()

    # A second run leaves the user (and its password) alone.
    before = (etc / "shadow").read_text()
    assert _run(boot, root).returncode == 0
    assert (etc / "shadow").read_text() == before


def test_wifi_password_can_be_kept(tmp_path):
    boot, root = _fake_card(tmp_path, STOCK_CONFIG)
    env = dict(os.environ, MLPI_PHONE_SKIP_PACKAGES="1", MLPI_SCRCPY_SERVER=str(SCRIPT),
               MLPI_ADB_KEYDIR=str(tmp_path / "nokey"))
    out = subprocess.run(["bash", str(SCRIPT), "--phone", "--wifi-password", "XVFpq6KCQ5WJ",
                          "--boot", str(boot), "--root", str(root)],
                         capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stderr
    assert 'wifi_password = "XVFpq6KCQ5WJ"' in (boot / "mlpi.toml").read_text()
