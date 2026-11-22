"""POSIX (macOS/Linux) native LuaJIT + lua-utf8.so prebuilt download and source build."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .constants import (
    _ASSET_TIMEOUT_SPEC,
    _CHECK_TIMEOUT_SPEC,
    ASSET_MAX_RETRIES,
    POB_AGENT_DOWNLOAD_BASE,
    POB_AGENT_RELEASES_LATEST_URL,
    POSIX_LUAJIT_TARBALL_URL,
    POSIX_LUAUTF8_TARBALL_URL,
    UpdateError,
    _to_httpx_timeout,
    rewrite_github_url,
)
from .paths import get_posix_platform_tag
from .remote import _download_bytes_with_retry, _extract_tag_from_location


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def _extract_tar_gz_safely(tar_path: Path, dest_dir: Path, *, strip_components: int = 0) -> None:
    import tarfile

    dest_resolved = dest_dir.resolve()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            if strip_components > 0:
                if len(parts) <= strip_components:
                    continue
                parts = parts[strip_components:]
            if not parts or any(p in ("..", "") for p in parts):
                continue
            rel = Path(*parts)
            if rel.is_absolute():
                raise UpdateError(f"Unsafe tar member path: {member.name}")
            out = (dest_dir / rel).resolve()
            try:
                out.relative_to(dest_resolved)
            except ValueError as exc:
                raise UpdateError(f"Tar Slip detected: {member.name}") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            src_f = tf.extractfile(member)
            if src_f is None:
                continue
            with src_f, open(out, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            if member.mode & 0o111:
                os.chmod(out, 0o755)


def _try_download_prebuilt_posix_runtime(
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[Path, Path] | None:
    """Attempt to download a prebuilt pob-runtime-<os>-<arch>.tar.gz from pob4agents releases."""
    plat_tag = get_posix_platform_tag()
    asset_name = f"pob-runtime-{plat_tag}.tar.gz"
    latest_url = rewrite_github_url(POB_AGENT_RELEASES_LATEST_URL, mirror=mirror)
    make_client = _pkg()._make_http_client
    try:
        with make_client(timeout=_to_httpx_timeout(_CHECK_TIMEOUT_SPEC), proxy=proxy, follow_redirects=False) as client:
            resp = client.get(latest_url)
            if resp.status_code in (301, 302, 303, 307, 308):
                tag = _extract_tag_from_location(resp.headers.get("location", ""))
            elif resp.status_code == 200 and "/tag/" in str(resp.url):
                tag = _extract_tag_from_location(str(resp.url))
            else:
                return None

        asset_url = rewrite_github_url(
            f"{POB_AGENT_DOWNLOAD_BASE}/{tag}/{asset_name}",
            mirror=mirror,
        )
        with make_client(timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC), proxy=proxy, follow_redirects=True) as client:
            aresp = client.get(asset_url)
            if aresp.status_code != 200:
                return None
            tar_tmp = staging_dir / "_downloads" / asset_name
            tar_tmp.parent.mkdir(parents=True, exist_ok=True)
            tar_tmp.write_bytes(aresp.content)

        extract_root = staging_dir / "_posix_prebuilt"
        extract_root.mkdir(parents=True, exist_ok=True)
        _extract_tar_gz_safely(tar_tmp, extract_root)

        luajit_src = extract_root / "bin" / "luajit"
        utf8_src = extract_root / "bin" / "lua-utf8.so"
        if not luajit_src.is_file():
            luajit_src = extract_root / "luajit"
            utf8_src = extract_root / "lua-utf8.so"
        if not (luajit_src.is_file() and utf8_src.is_file()):
            return None

        os.chmod(luajit_src, 0o755)
        os.chmod(utf8_src, 0o755)
        return {
            Path("bin") / "luajit": luajit_src,
            Path("bin") / "lua-utf8.so": utf8_src,
        }
    except Exception:
        return None


def _build_posix_runtime_from_source(
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[Path, Path]:
    """Compile minimal native LuaJIT 2.1 (PoB 2.68+ commit) and lua-utf8.so locally on macOS/Linux."""
    cc_bin = os.environ.get("CC", "").strip() or shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    make_bin = shutil.which("make")
    if not cc_bin or not make_bin:
        hint = (
            "Run `xcode-select --install` to install Apple Command Line Tools."
            if sys.platform == "darwin"
            else "Install build tools (`sudo apt install build-essential` or equivalent)."
        )
        raise UpdateError(
            f"Cannot build native POSIX LuaJIT runtime (missing C compiler or make). {hint}"
        )

    build_timeout = _to_httpx_timeout((10.0, 60.0, 10.0, 10.0))
    lj_url = rewrite_github_url(POSIX_LUAJIT_TARBALL_URL, mirror=mirror)
    u8_url = rewrite_github_url(POSIX_LUAUTF8_TARBALL_URL, mirror=mirror)

    with tempfile.TemporaryDirectory(prefix="pob-posix-build-") as tmp_str:
        tmp = Path(tmp_str)
        lj_tar = tmp / "luajit.tar.gz"
        u8_tar = tmp / "luautf8.tar.gz"
        lj_src = tmp / "luajit-src"
        u8_src = tmp / "luautf8-src"
        lj_src.mkdir()
        u8_src.mkdir()

        with _pkg()._make_http_client(timeout=build_timeout, proxy=proxy, follow_redirects=True) as client:
            lj_tar.write_bytes(_download_bytes_with_retry(client, lj_url, retries=ASSET_MAX_RETRIES))
            u8_tar.write_bytes(_download_bytes_with_retry(client, u8_url, retries=ASSET_MAX_RETRIES))

        _extract_tar_gz_safely(lj_tar, lj_src, strip_components=1)
        _extract_tar_gz_safely(u8_tar, u8_src, strip_components=1)

        jobs = str(max(1, os.cpu_count() or 4))
        env = os.environ.copy()
        if sys.platform == "darwin":
            env.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0")

        lj_proc = subprocess.run(
            [make_bin, "-C", str(lj_src / "src"), f"-j{jobs}", "BUILDMODE=static", "amalg"],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if lj_proc.returncode != 0:
            tail = (lj_proc.stderr or lj_proc.stdout or "")[-600:]
            raise UpdateError(f"Failed to compile LuaJIT 2.1 (exit {lj_proc.returncode}): {tail}")

        out_bin_dir = staging_dir / "bin"
        out_bin_dir.mkdir(parents=True, exist_ok=True)
        luajit_out = out_bin_dir / "luajit"
        utf8_out = out_bin_dir / "lua-utf8.so"

        shutil.copy2(lj_src / "src" / "luajit", luajit_out)
        os.chmod(luajit_out, 0o755)

        if sys.platform == "darwin":
            cc_cmd = [
                cc_bin,
                "-O2",
                "-fPIC",
                "-bundle",
                "-undefined",
                "dynamic_lookup",
                f"-I{lj_src / 'src'}",
                str(u8_src / "lutf8lib.c"),
                "-o",
                str(utf8_out),
            ]
        else:
            cc_cmd = [
                cc_bin,
                "-O2",
                "-fPIC",
                "-shared",
                f"-I{lj_src / 'src'}",
                str(u8_src / "lutf8lib.c"),
                "-o",
                str(utf8_out),
            ]

        u8_proc = subprocess.run(cc_cmd, env=env, capture_output=True, text=True, timeout=120)
        if u8_proc.returncode != 0:
            tail = (u8_proc.stderr or u8_proc.stdout or "")[-600:]
            raise UpdateError(f"Failed to compile lua-utf8.so (exit {u8_proc.returncode}): {tail}")
        os.chmod(utf8_out, 0o755)

        # Quick self-test: compound assignment (+= / ..=) + require("lua-utf8")
        check_env = env.copy()
        check_env["LUA_CPATH"] = f"{out_bin_dir}/?.so;;"
        check_proc = subprocess.run(
            [
                str(luajit_out),
                "-e",
                'local c = 1; c += 41; assert(c == 42); local s = "po"; s ..= "b"; assert(s == "pob"); '
                'local u = require("lua-utf8"); assert(u.len("流放") == 2)',
            ],
            env=check_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if check_proc.returncode != 0:
            tail = (check_proc.stderr or check_proc.stdout or "")[-400:]
            raise UpdateError(f"Compiled POSIX LuaJIT runtime failed self-test: {tail}")

        return {
            Path("bin") / "luajit": luajit_out,
            Path("bin") / "lua-utf8.so": utf8_out,
        }


def ensure_posix_runtime(
    engine_dir: Path,
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[Path, Path]:
    """Ensure macOS/Linux native LuaJIT + lua-utf8.so exist (prebuilt download or local source build)."""
    if _pkg().is_windows():
        return {}
    if os.environ.get("POB_ENGINE_CMD", "").strip() or os.environ.get("POB_LUAJIT", "").strip():
        return {}
    eng = Path(engine_dir)
    if (eng / "bin" / "luajit").is_file() and (eng / "bin" / "lua-utf8.so").is_file():
        return {}

    prebuilt = _try_download_prebuilt_posix_runtime(staging_dir, proxy=proxy, mirror=mirror)
    if prebuilt:
        return prebuilt
    return _build_posix_runtime_from_source(staging_dir, proxy=proxy, mirror=mirror)
