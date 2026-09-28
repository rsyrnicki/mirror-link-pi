from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mlpi import config


def test_defaults_when_no_file_or_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr(config, "PROJECT_CONFIG", tmp_path / "missing2.toml")
    monkeypatch.delenv("MLPI_INTERFACE", raising=False)
    cfg = config.load()
    assert cfg.network.interface == "usb0"
    assert cfg.network.http_port == 8080
    assert cfg.ssdp.device_uuid.startswith("c8cba096")


def test_file_overrides_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr(config, "PROJECT_CONFIG", tmp_path / "missing2.toml")
    f = tmp_path / "mlpi.toml"
    f.write_text(textwrap.dedent("""
        [network]
        interface = "enp0s20f0u1"
        http_port = 9090
    """))
    cfg = config.load(path=f)
    assert cfg.network.interface == "enp0s20f0u1"
    assert cfg.network.http_port == 9090
    # Untouched fields keep defaults
    assert cfg.network.address == ""


def test_env_overrides_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr(config, "PROJECT_CONFIG", tmp_path / "missing2.toml")
    f = tmp_path / "mlpi.toml"
    f.write_text("[network]\ninterface = \"from-file\"\n")
    monkeypatch.setenv("MLPI_INTERFACE", "from-env")
    monkeypatch.setenv("MLPI_HTTP_PORT", "1234")
    cfg = config.load(path=f)
    assert cfg.network.interface == "from-env"
    assert cfg.network.http_port == 1234


def test_empty_env_does_not_override(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr(config, "PROJECT_CONFIG", tmp_path / "missing2.toml")
    monkeypatch.setenv("MLPI_INTERFACE", "")
    cfg = config.load()
    assert cfg.network.interface == "usb0"


def test_env_coercion_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr(config, "PROJECT_CONFIG", tmp_path / "missing2.toml")
    monkeypatch.setenv("MLPI_HTTP_PORT", "not-a-number")
    with pytest.raises(ValueError, match="MLPI_HTTP_PORT"):
        config.load()


def test_unknown_keys_in_toml_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr(config, "PROJECT_CONFIG", tmp_path / "missing2.toml")
    f = tmp_path / "mlpi.toml"
    f.write_text("[network]\nbogus_field = \"ignored\"\ninterface = \"foo\"\n")
    cfg = config.load(path=f)
    assert cfg.network.interface == "foo"
    assert not hasattr(cfg.network, "bogus_field")


def test_resolve_log_file_uses_explicit_setting(tmp_path):
    cfg = config.Config()
    cfg.logging.file = str(tmp_path / "explicit.log")
    assert config.resolve_log_file(cfg) == Path(cfg.logging.file)


def test_resolve_log_file_falls_back_to_xdg_state(monkeypatch, tmp_path):
    cfg = config.Config()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_writable_dir", lambda p: False)
    assert config.resolve_log_file(cfg) == tmp_path / "mlpi" / "mlpi.log"
