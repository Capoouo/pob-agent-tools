"""Tests for standalone packaging, frozen daemon spawning, runtime dir resolution, and CLI version."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import pob_cli
from pob_cli import daemon, updater
from pob_cli.cli import main


def test_version_option_matches_package_version() -> None:
    runner = CliRunner()
    res = runner.invoke(main, ["--version"])
    assert res.exit_code == 0
    assert pob_cli.__version__ in res.output


def test_internal_daemon_command_is_hidden_and_parseable() -> None:
    runner = CliRunner()
    top_help = runner.invoke(main, ["--help"])
    assert top_help.exit_code == 0
    assert "_daemon" not in top_help.output

    sub_help = runner.invoke(main, ["_daemon", "--help"])
    assert sub_help.exit_code == 0
    assert "--runtime-dir" in sub_help.output
    assert "--offline" in sub_help.output


def test_runtime_dir_resolution_installed_and_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POB_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("POB_HOME", raising=False)

    # 1. Explicit runtime_dir wins over everything
    explicit = tmp_path / "explicit_rt"
    assert updater.get_runtime_dir(explicit) == explicit

    # 2. POB_RUNTIME_DIR env wins when explicit is omitted
    env_rt = tmp_path / "env_rt"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(env_rt))
    assert updater.get_runtime_dir() == env_rt
    monkeypatch.delenv("POB_RUNTIME_DIR")

    # 3. POB_HOME env resolves to <POB_HOME>/.runtime
    env_home = tmp_path / "env_home"
    monkeypatch.setenv("POB_HOME", str(env_home))
    assert updater.get_runtime_dir() == env_home / ".runtime"
    monkeypatch.delenv("POB_HOME")

    # 4. Frozen binary (PyInstaller) never uses source checkout parents[3]; uses OS user data dir
    fake_appdata = tmp_path / "LocalAppData"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))
        assert updater.detect_source_checkout_root() is None
        assert updater.get_runtime_dir() == fake_appdata / "pob-agent-tools"
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(fake_appdata))
        assert updater.detect_source_checkout_root() is None
        assert updater.get_runtime_dir() == fake_appdata / "pob-agent-tools"


def test_build_daemon_spawn_cmd_frozen_vs_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "rt"

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert daemon._build_daemon_spawn_cmd() == [sys.executable, "-m", "pob_cli.daemon"]
    assert daemon._build_daemon_spawn_cmd(rt) == [
        sys.executable,
        "-m",
        "pob_cli.daemon",
        "--runtime-dir",
        str(rt),
    ]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert daemon._build_daemon_spawn_cmd() == [sys.executable, "_daemon"]
    assert daemon._build_daemon_spawn_cmd(rt) == [
        sys.executable,
        "_daemon",
        "--runtime-dir",
        str(rt),
    ]


def test_release_tools_pack_and_scoop_manifest_update(tmp_path: Path) -> None:
    tool_path = Path(__file__).resolve().parents[1] / "packaging" / "release_tools.py"
    spec = importlib.util.spec_from_file_location("release_tools", tool_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    dist_dir = tmp_path / "dist" / "pob"
    (dist_dir / "_internal").mkdir(parents=True)
    (dist_dir / "pob.exe").write_bytes(b"MZ-fake-pob")
    (dist_dir / "_internal" / "cert.pem").write_text("dummy", encoding="utf-8")

    out_dir = tmp_path / "release-assets"
    zip_path, zip_sha = mod.pack_windows_release(dist_dir, out_dir, "v0.2.0")

    assert zip_path.name == "pob-v0.2.0-windows-x64.zip"
    assert len(zip_sha) == 64
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "pob.exe" in names
        assert "_internal/cert.pem" in names

    sums = (out_dir / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert f"{zip_sha}  pob-v0.2.0-windows-x64.zip" in sums

    repo_manifest = Path(__file__).resolve().parents[2] / "bucket" / "pob.json"
    copied = tmp_path / "pob.json"
    copied.write_text(repo_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    mod.update_scoop_manifest(copied, "0.2.0", "v0.2.0", zip_sha)

    updated = json.loads(copied.read_text(encoding="utf-8"))
    assert updated["version"] == "0.2.0"
    assert updated["architecture"]["64bit"]["hash"] == zip_sha
    assert updated["architecture"]["64bit"]["url"].endswith("/v0.2.0/pob-v0.2.0-windows-x64.zip")

