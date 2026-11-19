"""Managed headless PoB engine resolver, dual-upstream bootstrapper, and atomic incremental updater."""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

USER_AGENT = "pob-cli/0.2.0 (+https://github.com/PathOfBuildingCommunity/PathOfBuilding)"

POBTOOLS_LATEST_URL = "https://github.com/Hsiung-Shao/PobTools-zh/releases/latest"
POBTOOLS_DOWNLOAD_BASE = "https://github.com/Hsiung-Shao/PobTools-zh/releases/download"
POB_MANIFEST_URL = "https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/master/manifest.xml"

# Strict fast-fail budget for metadata checks (no retries on connect timeout)
CHECK_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0)
ASSET_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0)
ASSET_MAX_RETRIES = 1

UPDATE_STATE_FILENAME = "update_state.json"
POBTOOLS_META_FILENAME = ".pobtools_version.json"

STATE_UP_TO_DATE = "up_to_date"
STATE_UPDATED = "updated"
STATE_DEGRADED_NETWORK = "degraded_network_error"
STATE_DEGRADED_ROLLBACK = "degraded_gate_rollback"
STATE_BOOTSTRAP_MISSING = "bootstrap_missing"

OK_STATES = frozenset({STATE_UP_TO_DATE, STATE_UPDATED})

# Clean host files/directories copied from legacy PobTools[20261102] into managed runtime
LEGACY_SEED_FILES = ("pob-zh.exe", "pob-zh.ini", "LICENSE", "NOTICE.md")
LEGACY_SEED_DIRS = ("engine", "Data", "Fonts", "PoE1")

GITHUB_MIRROR_PREFIXES = (
    "https://github.com/",
    "https://raw.githubusercontent.com/",
    "https://objects.githubusercontent.com/",
)


class UpdateError(RuntimeError):
    """Base error for engine bootstrap/update failures."""


class EngineGateError(UpdateError):
    """Raised when headless engine smoke probe (gate_result + hello) fails."""


@dataclass(frozen=True)
class ManifestFileEntry:
    """Single file entry parsed from PathOfBuilding manifest.xml."""

    raw_name: str
    local_rel: str
    part: str
    sha1: str
    runtime: str | None
    platform: str | None


@dataclass(frozen=True)
class PobManifest:
    """Parsed PathOfBuilding manifest.xml."""

    version: str
    branch: str
    platform: str
    sources: dict[str, dict[str, str]]
    files: dict[str, ManifestFileEntry]
    raw_xml: str


def _get_project_root() -> Path:
    env_home = os.environ.get("POB_HOME", "").strip()
    if env_home:
        return Path(env_home)
    return Path(__file__).resolve().parents[3]


def _get_runtime_dir(runtime_dir: Path | None = None) -> Path:
    if runtime_dir is not None:
        return Path(runtime_dir)
    env_rt = os.environ.get("POB_RUNTIME_DIR", "").strip()
    if env_rt:
        return Path(env_rt)
    return _get_project_root() / ".runtime"


def is_valid_engine_dir(path: Path | None) -> bool:
    """Return True if path contains a runnable pob-zh.exe and PoE1/ directory."""
    if path is None:
        return False
    p = Path(path)
    return (p / "pob-zh.exe").is_file() and (p / "PoE1").is_dir()


def _find_legacy_dir(legacy_dir: Path | None = None) -> Path | None:
    if legacy_dir is not None:
        p = Path(legacy_dir)
        return p if is_valid_engine_dir(p) else None

    env_legacy = os.environ.get("POB_LEGACY_DIR")
    if env_legacy is not None:
        stripped = env_legacy.strip()
        if not stripped:
            return None
        p = Path(stripped)
        return p if is_valid_engine_dir(p) else None

    candidates = [_get_project_root() / "PobTools[20261102]"]
    if not os.environ.get("POB_HOME", "").strip():
        candidates.append(Path("F:/poe/PobTools[20261102]"))

    for cand in candidates:
        if is_valid_engine_dir(cand):
            return cand
    return None


def resolve_engine_root(
    runtime_dir: Path | None = None,
    legacy_dir: Path | None = None,
) -> Path:
    """Resolve the active PoB engine directory.

    Priority:
      1. `POB_ROOT` environment variable if valid (`pob-zh.exe` + `PoE1/`).
      2. Managed `<runtime_dir>/engine` (`POB_RUNTIME_DIR/engine`, default `<project>/.runtime/engine`) if valid.
      3. Legacy `PobTools[20261102]` fallback if valid.
      4. Default managed path `<runtime_dir>/engine` (for fresh bootstrap).
    """
    env_root = os.environ.get("POB_ROOT", "").strip()
    if env_root:
        p = Path(env_root)
        if is_valid_engine_dir(p):
            return p

    managed = _get_runtime_dir(runtime_dir) / "engine"
    if is_valid_engine_dir(managed):
        return managed

    leg = _find_legacy_dir(legacy_dir)
    if leg is not None:
        return leg

    if env_root:
        return Path(env_root)
    return managed


def seed_from_legacy_dir(legacy_dir: Path, target_dir: Path) -> bool:
    """Losslessly copy clean host + PoE1 assets from legacy_dir into an uninitialized target_dir.

    Never modifies or deletes anything inside legacy_dir.
    Returns True if target_dir was seeded, False if already initialized or legacy_dir is invalid.
    """
    src = Path(legacy_dir)
    dst = Path(target_dir)
    if is_valid_engine_dir(dst):
        return False
    if not is_valid_engine_dir(src):
        return False

    dst.mkdir(parents=True, exist_ok=True)
    for fname in LEGACY_SEED_FILES:
        src_file = src / fname
        if src_file.is_file():
            shutil.copy2(src_file, dst / fname)

    for dname in LEGACY_SEED_DIRS:
        src_sub = src / dname
        if src_sub.is_dir():
            shutil.copytree(src_sub, dst / dname, dirs_exist_ok=True)

    return is_valid_engine_dir(dst)


def resolve_proxy(proxy: str | None = None) -> str | None:
    """Resolve HTTP/SOCKS proxy URL from explicit arg or environment variables."""
    if proxy is not None and proxy.strip():
        return proxy.strip()
    for var in ("POB_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


def resolve_mirror(mirror: str | None = None) -> str | None:
    """Resolve optional GitHub mirror prefix from explicit arg or POB_GITHUB_MIRROR."""
    if mirror is not None and mirror.strip():
        return mirror.strip().rstrip("/")
    env_mirror = os.environ.get("POB_GITHUB_MIRROR", "").strip()
    return env_mirror.rstrip("/") if env_mirror else None


def rewrite_github_url(url: str, mirror: str | None = None) -> str:
    """Optionally rewrite a GitHub URL through a user-configured mirror prefix."""
    eff_mirror = resolve_mirror(mirror)
    if not eff_mirror:
        return url
    if not url.startswith(GITHUB_MIRROR_PREFIXES):
        return url
    if "{url}" in eff_mirror:
        return eff_mirror.format(url=url)
    return f"{eff_mirror}/{url}"


def _make_http_client(
    *,
    timeout: httpx.Timeout,
    proxy: str | None = None,
    follow_redirects: bool = True,
    max_connections: int = 16,
) -> httpx.Client:
    eff_proxy = resolve_proxy(proxy)
    limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": follow_redirects,
        "headers": {"User-Agent": USER_AGENT},
        "trust_env": False,
        "limits": limits,
    }
    if eff_proxy:
        kwargs["proxy"] = eff_proxy
    return httpx.Client(**kwargs)


def _parse_semver(ver: str | None) -> tuple[int, ...]:
    if not ver:
        return ()
    cleaned = ver.strip().lstrip("vV")
    nums = [int(x) for x in re.findall(r"\d+", cleaned)]
    return tuple(nums)


def _is_version_gte(ver_a: str | None, ver_b: str | None) -> bool:
    a = _parse_semver(ver_a)
    b = _parse_semver(ver_b)
    if not b:
        return True
    if not a:
        return False
    max_len = max(len(a), len(b))
    a_pad = a + (0,) * (max_len - len(a))
    b_pad = b + (0,) * (max_len - len(b))
    return a_pad >= b_pad


def _is_version_newer(remote: str | None, local: str | None) -> bool:
    r = _parse_semver(remote)
    l = _parse_semver(local)
    if not r:
        return False
    if not l:
        return True
    max_len = max(len(r), len(l))
    r_pad = r + (0,) * (max_len - len(r))
    l_pad = l + (0,) * (max_len - len(l))
    return r_pad > l_pad


def verify_pob_sha1(data: bytes, expected_sha1: str) -> bool:
    """Verify bytes against PoB manifest SHA1 (raw bytes, with official LF/CRLF tolerance)."""
    exp = expected_sha1.strip().lower()
    if hashlib.sha1(data).hexdigest() == exp:
        return True
    # Mirror official PoB UpdateCheck.lua line 194/276 tolerance for text files from GitHub raw
    if b"\n" in data and b"\r\n" not in data:
        if hashlib.sha1(data.replace(b"\n", b"\r\n")).hexdigest() == exp:
            return True
    if b"\r\n" in data:
        if hashlib.sha1(data.replace(b"\r\n", b"\n")).hexdigest() == exp:
            return True
    return False


def parse_pob_manifest(xml_text: str) -> PobManifest:
    """Parse a PathOfBuilding manifest.xml string into structured metadata & file entries."""
    root = ET.fromstring(xml_text)
    if root.tag != "PoBVersion":
        raise UpdateError(f"Invalid PoB manifest root element: {root.tag}")

    ver_elem = root.find("Version")
    if ver_elem is None or not ver_elem.attrib.get("number"):
        raise UpdateError("PoB manifest is missing <Version number=...>")

    version = ver_elem.attrib["number"].strip()
    branch = (ver_elem.attrib.get("branch") or "master").strip()
    platform = (ver_elem.attrib.get("platform") or "win32").strip()

    sources: dict[str, dict[str, str]] = {}
    for src_elem in root.findall("Source"):
        part = (src_elem.attrib.get("part") or "").strip()
        url = (src_elem.attrib.get("url") or "").strip()
        plat = (src_elem.attrib.get("platform") or "any").strip()
        if part and url:
            sources.setdefault(part, {})[plat] = url

    files: dict[str, ManifestFileEntry] = {}
    for f_elem in root.findall("File"):
        raw_name = (f_elem.attrib.get("name") or "").strip()
        part = (f_elem.attrib.get("part") or "program").strip()
        sha1 = (f_elem.attrib.get("sha1") or "").strip().lower()
        plat = f_elem.attrib.get("platform")
        runtime = f_elem.attrib.get("runtime")
        if not raw_name or not sha1:
            continue
        if plat and plat != "win32":
            continue
        if runtime and runtime != "win32":
            continue
        local_rel = raw_name.replace("{space}", " ")
        # Path traversal guard
        norm_parts = Path(local_rel).parts
        if Path(local_rel).is_absolute() or ".." in norm_parts:
            raise UpdateError(f"Unsafe manifest path: {raw_name}")
        files[raw_name] = ManifestFileEntry(
            raw_name=raw_name,
            local_rel=local_rel,
            part=part,
            sha1=sha1,
            runtime=runtime,
            platform=plat,
        )

    if not sources or not files:
        raise UpdateError("PoB manifest contains no sources or files.")

    return PobManifest(
        version=version,
        branch=branch,
        platform=platform,
        sources=sources,
        files=files,
        raw_xml=xml_text,
    )


def build_manifest_file_url(manifest: PobManifest, entry: ManifestFileEntry, mirror: str | None = None) -> str:
    """Build the raw download URL for a PoB manifest file entry."""
    part_sources = manifest.sources.get(entry.part)
    if not part_sources:
        raise UpdateError(f"No <Source> defined in manifest for part='{entry.part}'")
    base = part_sources.get(manifest.platform) or part_sources.get("any")
    if not base:
        raise UpdateError(f"No matching platform source for part='{entry.part}'")
    base = base.replace("{branch}", manifest.branch)
    # Official PoB GitHub raw keeps `{space}` literal in runtime filenames (URL-escaped)
    quoted = urllib.parse.quote(entry.raw_name, safe="/")
    url = base + quoted
    return rewrite_github_url(url, mirror=mirror)


def find_local_manifest_mismatches(poe1_dir: Path) -> list[ManifestFileEntry]:
    """Verify local PoE1/ files against local PoE1/manifest.xml and return missing/tampered entries."""
    manifest_path = Path(poe1_dir) / "manifest.xml"
    if not manifest_path.is_file():
        return []
    manifest = parse_pob_manifest(manifest_path.read_text(encoding="utf-8", errors="replace"))
    mismatched: list[ManifestFileEntry] = []
    for entry in manifest.files.values():
        fp = Path(poe1_dir) / entry.local_rel
        if not fp.is_file():
            mismatched.append(entry)
            continue
        try:
            data = fp.read_bytes()
        except OSError:
            mismatched.append(entry)
            continue
        if not verify_pob_sha1(data, entry.sha1):
            mismatched.append(entry)
    return mismatched


def diff_pob_manifest(
    poe1_dir: Path,
    remote_manifest: PobManifest,
    *,
    verify_local_sha1: bool = True,
) -> tuple[list[ManifestFileEntry], list[Path]]:
    """Compute PoE1/ files to download and obsolete files to delete against remote_manifest.

    When verify_local_sha1=True (default), hashes actual files on disk against remote_manifest
    so tampered files (such as PoE1/UpdateCheck.lua) are always detected and repaired.
    """
    poe1 = Path(poe1_dir)
    local_manifest: PobManifest | None = None
    local_manifest_path = poe1 / "manifest.xml"
    if local_manifest_path.is_file():
        try:
            local_manifest = parse_pob_manifest(local_manifest_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            local_manifest = None

    to_download: list[ManifestFileEntry] = []
    for entry in remote_manifest.files.values():
        local_path = poe1 / entry.local_rel
        if not local_path.is_file():
            to_download.append(entry)
            continue
        if verify_local_sha1:
            try:
                data = local_path.read_bytes()
            except OSError:
                to_download.append(entry)
                continue
            if not verify_pob_sha1(data, entry.sha1):
                to_download.append(entry)
        elif local_manifest is not None:
            local_entry = local_manifest.files.get(entry.raw_name)
            if local_entry is None or local_entry.sha1 != entry.sha1:
                to_download.append(entry)
        else:
            to_download.append(entry)

    to_delete: list[Path] = []
    if local_manifest is not None:
        remote_rels = {e.local_rel.lower() for e in remote_manifest.files.values()}
        for old_entry in local_manifest.files.values():
            if old_entry.local_rel.lower() not in remote_rels:
                if (poe1 / old_entry.local_rel).is_file():
                    to_delete.append(Path("PoE1") / old_entry.local_rel)

    return to_download, to_delete


def _render_local_manifest_xml(remote_manifest: PobManifest) -> str:
    """Serialize remote manifest XML ensuring platform/branch attributes exist on <Version>."""
    root = ET.fromstring(remote_manifest.raw_xml)
    ver_elem = root.find("Version")
    if ver_elem is not None:
        ver_elem.set("platform", remote_manifest.platform)
        ver_elem.set("branch", remote_manifest.branch)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def detect_local_versions(engine_dir: Path | None, runtime_dir: Path | None = None) -> dict[str, str | None]:
    """Detect local PoB, PobTools-zh, and DataV2 versions from disk metadata + cached state."""
    pob_ver: str | None = None
    pobtools_ver: str | None = None
    pobtools_tag: str | None = None
    data_ver: str | None = None

    if engine_dir is not None:
        eng = Path(engine_dir)
        manifest_path = eng / "PoE1" / "manifest.xml"
        if manifest_path.is_file():
            try:
                root = ET.parse(manifest_path).getroot()
                v = root.find("Version")
                if v is not None and v.attrib.get("number"):
                    pob_ver = v.attrib["number"].strip()
            except Exception:
                pass

        trans_path = eng / "Data" / "translations_version.json"
        if trans_path.is_file():
            try:
                tdata = json.loads(trans_path.read_text(encoding="utf-8"))
                if isinstance(tdata, dict) and tdata.get("dataVersion"):
                    data_ver = str(tdata["dataVersion"]).strip()
            except Exception:
                pass

        meta_path = eng / POBTOOLS_META_FILENAME
        if meta_path.is_file():
            try:
                mdata = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(mdata, dict):
                    pobtools_ver = mdata.get("pobToolsVersion") or pobtools_ver
                    pobtools_tag = mdata.get("pobToolsTag") or pobtools_tag
                    data_ver = data_ver or mdata.get("dataVersion")
            except Exception:
                pass

    prev = load_update_state(runtime_dir)
    if prev:
        if not pob_ver and prev.get("localPobVersion"):
            pob_ver = str(prev["localPobVersion"])
        if not pobtools_ver and prev.get("pobToolsVersion"):
            pobtools_ver = str(prev["pobToolsVersion"])
        if not pobtools_tag and prev.get("pobToolsTag"):
            pobtools_tag = str(prev["pobToolsTag"])
        if not data_ver and prev.get("dataVersion"):
            data_ver = str(prev["dataVersion"])

    return {
        "localPobVersion": pob_ver,
        "pobToolsVersion": pobtools_ver,
        "pobToolsTag": pobtools_tag,
        "dataVersion": data_ver,
    }


def _write_pobtools_meta(
    engine_dir: Path,
    *,
    pobtools_version: str | None,
    pobtools_tag: str | None,
    data_version: str | None,
) -> None:
    meta_path = Path(engine_dir) / POBTOOLS_META_FILENAME
    payload = {
        "pobToolsVersion": pobtools_version,
        "pobToolsTag": pobtools_tag,
        "dataVersion": data_version,
        "updatedAt": time.time(),
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_update_state_path(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / UPDATE_STATE_FILENAME


def load_update_state(runtime_dir: Path | None = None) -> dict[str, Any]:
    """Load persisted update_state.json from runtime_dir, returning {} if absent/corrupt."""
    path = get_update_state_path(runtime_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_update_state(state: dict[str, Any], runtime_dir: Path | None = None) -> Path:
    """Atomically persist update_state.json to runtime_dir."""
    path = get_update_state_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def _build_user_hint(
    state_name: str,
    local_pob_version: str | None = None,
    network_error: str | None = None,
) -> str | None:
    if state_name in OK_STATES:
        return None
    ver_str = local_pob_version or "unknown"
    err_part = f"；原因: {network_error}" if network_error else ""
    if state_name == STATE_DEGRADED_NETWORK:
        return (
            f"GitHub 连接失败，PoB 未能检查/更新至最新版（当前本地版本 {ver_str}，"
            f"可能缺失最新赛季天赋树或技能改动{err_part}）。"
            "请检查网络代理（设置环境变量 HTTPS_PROXY / POB_PROXY 或执行 "
            "`pob update --proxy http://127.0.0.1:<port>`），"
            "或使用 `pob bootstrap --from-dir <本地目录>` 离线导入。"
        )
    if state_name == STATE_BOOTSTRAP_MISSING:
        return (
            f"本地未找到可用 PoB 引擎且无法从 GitHub 下载（{network_error or '离线或网络不通'}）。"
            "请配置代理后执行 `pob bootstrap --proxy http://127.0.0.1:<port>`"
            "（或设置 HTTPS_PROXY / POB_PROXY / POB_GITHUB_MIRROR），"
            "或使用 `pob bootstrap --from-dir <本地PobTools目录>` 离线导入。"
        )
    if state_name == STATE_DEGRADED_ROLLBACK:
        return (
            f"PoB 引擎更新后冒烟探针 gate_result 未通过{err_part}，"
            f"已自动回滚至原本地版本 {ver_str} 以保持可用。"
            "可稍后执行 `pob update --force` 重试。"
        )
    return f"PoB 更新状态异常 ({state_name}, 本地版本 {ver_str}{err_part})。"


def get_update_warning(state: dict[str, Any] | None) -> str | None:
    """Return structured userHint warning whenever state is not up_to_date/updated."""
    if not state:
        return None
    st = str(state.get("state") or "")
    if not st or st in OK_STATES:
        return None
    hint = state.get("userHint")
    if isinstance(hint, str) and hint.strip():
        return hint.strip()
    return _build_user_hint(
        st,
        local_pob_version=state.get("localPobVersion"),
        network_error=state.get("networkError"),
    )


def format_stderr_update_alert(state: dict[str, Any] | None) -> str | None:
    """Format a concise, visible single-block Chinese warning for stderr when degraded."""
    warning = get_update_warning(state)
    if not warning or not state:
        return None
    st = state.get("state") or "unknown"
    ver = state.get("localPobVersion") or "unknown"
    return f"[pob 告警 | {st} | 本地 PoB {ver}] {warning}"


def probe_engine_gate(engine_dir: Path, timeout_sec: float = 20.0) -> dict[str, Any]:
    """Run headless `pob-zh.exe --engine-headless` smoke probe and verify gate_result + hello."""
    eng = Path(engine_dir)
    exe_path = eng / "pob-zh.exe"
    game_dir = eng / "PoE1"

    if not exe_path.is_file():
        return {"ok": False, "error": f"pob-zh.exe not found at {exe_path}", "stderrTail": []}
    if not game_dir.is_dir():
        return {"ok": False, "error": f"PoE1 directory not found at {game_dir}", "stderrTail": []}

    out_queue: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
    stderr_tail: collections.deque[str] = collections.deque(maxlen=50)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = subprocess.Popen(
        [str(exe_path), "--engine-headless", str(game_dir)],
        cwd=str(game_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )

    def _read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out_queue.put(obj)
        except Exception as exc:  # noqa: BLE001
            out_queue.put(exc)
        finally:
            out_queue.put(None)

    def _read_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                s = line.rstrip("\r\n")
                if s:
                    stderr_tail.append(s)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_read_stdout, name="pob-gate-stdout", daemon=True).start()
    threading.Thread(target=_read_stderr, name="pob-gate-stderr", daemon=True).start()

    gate_data: dict[str, Any] | None = None
    hello_data: dict[str, Any] | None = None
    ver_data: dict[str, Any] = {}
    deadline = time.monotonic() + timeout_sec

    try:
        while gate_data is None or hello_data is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "ok": False,
                    "error": f"Timed out after {timeout_sec:.1f}s waiting for gate_result/hello",
                    "gate": gate_data,
                    "hello": hello_data,
                    "stderrTail": list(stderr_tail)[-10:],
                }
            try:
                msg = out_queue.get(timeout=remaining)
            except queue.Empty:
                return {
                    "ok": False,
                    "error": f"Timed out after {timeout_sec:.1f}s waiting for gate_result/hello",
                    "gate": gate_data,
                    "hello": hello_data,
                    "stderrTail": list(stderr_tail)[-10:],
                }

            if msg is None or isinstance(msg, Exception):
                return {
                    "ok": False,
                    "error": f"Engine exited before startup handshake completed: {msg}",
                    "gate": gate_data,
                    "hello": hello_data,
                    "stderrTail": list(stderr_tail)[-10:],
                }

            event = msg.get("event")
            data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
            if event == "gate_result":
                gate_data = data
                if not data.get("ok"):
                    return {
                        "ok": False,
                        "error": f"gate_result failed: {data.get('failed')}",
                        "failed": data.get("failed"),
                        "gate": gate_data,
                        "hello": hello_data,
                        "stderrTail": list(stderr_tail)[-10:],
                    }
            elif event == "hello":
                hello_data = data

        # Optional quick version{} RPC to capture caps
        remaining = max(0.0, deadline - time.monotonic())
        if proc.stdin is not None and remaining > 0.5:
            try:
                proc.stdin.write(json.dumps({"id": 1, "method": "version", "params": {}}) + "\n")
                proc.stdin.flush()
                ver_deadline = time.monotonic() + min(5.0, remaining)
                while time.monotonic() < ver_deadline:
                    msg = out_queue.get(timeout=max(0.1, ver_deadline - time.monotonic()))
                    if msg is None or isinstance(msg, Exception):
                        break
                    if msg.get("id") == 1 and isinstance(msg.get("result"), dict):
                        ver_data = msg["result"]
                        break
            except Exception:
                pass

        return {
            "ok": True,
            "gate": gate_data,
            "hello": hello_data,
            "pobVersion": ver_data.get("pobVersion") or hello_data.get("pobVersion"),
            "bridgeVersion": hello_data.get("bridge"),
            "caps": ver_data.get("caps") or {},
            "stderrTail": list(stderr_tail)[-10:],
        }
    finally:
        try:
            proc.kill()
            proc.wait(timeout=3.0)
        except Exception:
            pass


def _extract_tag_from_location(location: str) -> str:
    """Extract release tag from GitHub 302 Location header."""
    if "/tag/" in location:
        tag = location.rsplit("/tag/", 1)[-1].split("?", 1)[0].strip("/")
        if tag:
            return urllib.parse.unquote(tag)
    raise UpdateError(f"Could not extract release tag from Location header: {location!r}")


def fetch_pobtools_latest_info(
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    timeout: httpx.Timeout = CHECK_TIMEOUT,
    fetch_manifest: bool = True,
) -> dict[str, Any]:
    """Resolve latest PobTools-zh tag via 302 redirect (NEVER api.github.com) and optional manifest JSON."""
    latest_url = rewrite_github_url(POBTOOLS_LATEST_URL, mirror=mirror)
    with _make_http_client(timeout=timeout, proxy=proxy, follow_redirects=False) as client:
        resp = client.get(latest_url)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            tag = _extract_tag_from_location(loc)
        elif resp.status_code == 200 and "/tag/" in str(resp.url):
            tag = _extract_tag_from_location(str(resp.url))
        else:
            raise UpdateError(f"Unexpected status {resp.status_code} from PobTools-zh releases/latest")

    info: dict[str, Any] = {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "manifest": None,
        "dataManifest": None,
    }
    if not fetch_manifest:
        return info

    manifest_url = rewrite_github_url(
        f"{POBTOOLS_DOWNLOAD_BASE}/{tag}/PobTools-manifest-{tag}.json",
        mirror=mirror,
    )
    with _make_http_client(timeout=timeout, proxy=proxy, follow_redirects=True) as client:
        mresp = client.get(manifest_url)
        mresp.raise_for_status()
        manifest_data = mresp.json()
        info["manifest"] = manifest_data
        if isinstance(manifest_data, dict):
            if manifest_data.get("version"):
                info["version"] = str(manifest_data["version"]).strip()
            data_tag = manifest_data.get("dataTag")
            if data_tag:
                data_manifest_url = rewrite_github_url(
                    f"{POBTOOLS_DOWNLOAD_BASE}/{data_tag}/PobTools-manifest-{data_tag}.json",
                    mirror=mirror,
                )
                dresp = client.get(data_manifest_url)
                if dresp.status_code == 200:
                    info["dataManifest"] = dresp.json()
    return info


def fetch_pob_manifest_remote(
    *,
    etag: str | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    timeout: httpx.Timeout = CHECK_TIMEOUT,
) -> tuple[str, PobManifest | None, str | None]:
    """Fetch official PathOfBuilding manifest.xml with optional If-None-Match ETag.

    Returns ("not_modified", None, etag) on 304, or ("ok", PobManifest, new_etag) on 200.
    """
    url = rewrite_github_url(POB_MANIFEST_URL, mirror=mirror)
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag

    with _make_http_client(timeout=timeout, proxy=proxy, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code == 304:
            return ("not_modified", None, resp.headers.get("etag") or etag)
        resp.raise_for_status()
        new_etag = resp.headers.get("etag") or etag
        manifest = parse_pob_manifest(resp.text)
        return ("ok", manifest, new_etag)


def _download_bytes_with_retry(
    client: httpx.Client,
    url: str,
    retries: int = ASSET_MAX_RETRIES,
) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= retries:
                raise
            time.sleep(0.25)
    assert last_exc is not None
    raise last_exc


def _download_pob_files_to_staging(
    manifest: PobManifest,
    entries: list[ManifestFileEntry],
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    max_workers: int = 12,
) -> dict[Path, Path]:
    """Concurrently download PoB manifest entries to staging_dir and verify every SHA1."""
    staged: dict[Path, Path] = {}
    if not entries:
        return staged

    with _make_http_client(
        timeout=ASSET_TIMEOUT,
        proxy=proxy,
        follow_redirects=True,
        max_connections=max(max_workers, 8),
    ) as client:

        def _fetch_one(entry: ManifestFileEntry) -> tuple[Path, Path]:
            url = build_manifest_file_url(manifest, entry, mirror=mirror)
            data = _download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES)
            if not verify_pob_sha1(data, entry.sha1):
                raise UpdateError(
                    f"SHA1 mismatch for '{entry.local_rel}': expected {entry.sha1}, "
                    f"got {hashlib.sha1(data).hexdigest()}"
                )
            rel_path = Path("PoE1") / entry.local_rel
            out_path = staging_dir / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            return (rel_path, out_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(entries))) as pool:
            futures = [pool.submit(_fetch_one, e) for e in entries]
            for fut in concurrent.futures.as_completed(futures):
                rel_path, out_path = fut.result()
                staged[rel_path] = out_path

    return staged


def _extract_zip_safely(
    zip_path: Path,
    dest_dir: Path,
    *,
    skip_prefixes: tuple[str, ...] = (),
) -> dict[Path, Path]:
    """Extract a zip safely into dest_dir (guarding against Zip Slip and single wrapper dir)."""
    extracted: dict[Path, Path] = {}
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if not infos:
            return extracted

        # Detect single top-level wrapper folder (e.g. PobTools-1.8.12/pob-zh.exe)
        top_levels = {Path(info.filename).parts[0] for info in infos if Path(info.filename).parts}
        strip_top = False
        if len(top_levels) == 1:
            only_top = next(iter(top_levels))
            if only_top.lower() not in {"data", "engine", "fonts", "poe1", "ui", "pobtools", "filters"}:
                strip_top = True

        for info in infos:
            parts = Path(info.filename).parts
            if strip_top and len(parts) > 1:
                parts = parts[1:]
            if not parts or any(p in ("..", "") for p in parts):
                continue
            rel = Path(*parts)
            if rel.is_absolute():
                raise UpdateError(f"Unsafe zip member path: {info.filename}")
            first_lower = rel.parts[0].lower()
            if any(first_lower == sp.lower() for sp in skip_prefixes):
                continue
            out = (dest_dir / rel).resolve()
            try:
                out.relative_to(dest_resolved)
            except ValueError as exc:
                raise UpdateError(f"Zip Slip detected: {info.filename}") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_f, open(out, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            extracted[rel] = out

    return extracted


def _pick_pobtools_asset(manifest_data: dict[str, Any], prefer_update: bool) -> dict[str, Any] | None:
    assets = manifest_data.get("assets") or []
    if not isinstance(assets, list):
        return None
    zips = [a for a in assets if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".zip")]
    if not zips:
        return None
    if prefer_update:
        for a in zips:
            if "update" in str(a.get("name", "")).lower():
                return a
    for a in zips:
        name_l = str(a.get("name", "")).lower()
        if "update" not in name_l and "datav2" not in name_l:
            return a
    return zips[0]


def _pick_data_asset(data_manifest: dict[str, Any]) -> dict[str, Any] | None:
    assets = data_manifest.get("assets") or []
    if not isinstance(assets, list):
        return None
    for a in assets:
        if isinstance(a, dict) and "datav2" in str(a.get("name", "")).lower() and str(a.get("name", "")).lower().endswith(".zip"):
            return a
    for a in assets:
        if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".zip"):
            return a
    return None


def _download_and_stage_release_zip(
    tag: str,
    asset: dict[str, Any],
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_prefixes: tuple[str, ...] = ("PoE1",),
) -> dict[Path, Path]:
    name = str(asset["name"])
    expected_sha256 = str(asset.get("sha256") or "").strip().lower()
    url = rewrite_github_url(f"{POBTOOLS_DOWNLOAD_BASE}/{tag}/{urllib.parse.quote(name)}", mirror=mirror)

    zip_tmp = staging_dir / "_downloads" / name
    zip_tmp.parent.mkdir(parents=True, exist_ok=True)
    with _make_http_client(timeout=ASSET_TIMEOUT, proxy=proxy, follow_redirects=True) as client:
        data = _download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES)
    if expected_sha256:
        actual = hashlib.sha256(data).hexdigest().lower()
        if actual != expected_sha256:
            raise UpdateError(f"SHA256 mismatch for '{name}': expected {expected_sha256}, got {actual}")
    zip_tmp.write_bytes(data)

    extract_root = staging_dir / "_extract" / Path(name).stem
    extract_root.mkdir(parents=True, exist_ok=True)
    return _extract_zip_safely(zip_tmp, extract_root, skip_prefixes=skip_prefixes)


def _apply_staged_files_with_gate(
    engine_dir: Path,
    staged_files: dict[Path, Path],
    deleted_rel_paths: list[Path] | None = None,
    *,
    probe_fn: Callable[[Path], dict[str, Any]] = probe_engine_gate,
) -> dict[str, Any]:
    """Backup existing files to .bak/, atomically replace from staged_files, probe gate, and rollback on failure."""
    eng = Path(engine_dir)
    bak_dir = eng / ".bak"
    staging_dir = eng / ".staging"

    if bak_dir.exists():
        shutil.rmtree(bak_dir, ignore_errors=True)
    bak_dir.mkdir(parents=True, exist_ok=True)

    backed_up: list[tuple[Path, Path]] = []
    created: list[Path] = []

    def _rollback() -> None:
        for dst, bak in reversed(backed_up):
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if bak.exists():
                    os.replace(bak, dst)
            except Exception:
                try:
                    if bak.exists():
                        shutil.copy2(bak, dst)
                except Exception:
                    pass
        for created_file in reversed(created):
            try:
                if created_file.is_file():
                    created_file.unlink()
            except Exception:
                pass

    try:
        for rel_path, staged_src in staged_files.items():
            dst = eng / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                bak = bak_dir / rel_path
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, bak)
                backed_up.append((dst, bak))
            else:
                created.append(dst)
            os.replace(staged_src, dst)

        for rel_del in deleted_rel_paths or []:
            dst = eng / rel_del
            if dst.is_file():
                bak = bak_dir / rel_del
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, bak)
                backed_up.append((dst, bak))
                dst.unlink()

        probe = probe_fn(eng)
        if not probe.get("ok"):
            _rollback()
            return {
                "ok": False,
                "rolledBack": True,
                "probe": probe,
                "error": probe.get("error") or "gate_result probe failed",
            }
        return {
            "ok": True,
            "rolledBack": False,
            "probe": probe,
        }
    except Exception as exc:  # noqa: BLE001
        _rollback()
        return {
            "ok": False,
            "rolledBack": True,
            "probe": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        shutil.rmtree(bak_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)


def bootstrap_engine(
    *,
    target_dir: Path | None = None,
    runtime_dir: Path | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    include_zh: bool = True,
    from_dir: Path | None = None,
    legacy_dir: Path | None = None,
    probe_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bootstrap a complete managed PoB engine from offline directory, legacy seed, or GitHub dual upstreams."""
    rt_dir = _get_runtime_dir(runtime_dir)
    eng_dir = Path(target_dir) if target_dir is not None else (rt_dir / "engine")
    probe_callable = probe_fn or probe_engine_gate

    # 1. Explicit offline directory import
    if from_dir is not None:
        src = Path(from_dir)
        if not is_valid_engine_dir(src):
            raise UpdateError(f"Offline source directory is invalid (requires pob-zh.exe and PoE1/): {src}")
        eng_dir.mkdir(parents=True, exist_ok=True)
        for fname in LEGACY_SEED_FILES:
            if (src / fname).is_file():
                shutil.copy2(src / fname, eng_dir / fname)
        for dname in LEGACY_SEED_DIRS:
            if (src / dname).is_dir():
                shutil.copytree(src / dname, eng_dir / dname, dirs_exist_ok=True)

        probe = probe_callable(eng_dir)
        if not probe.get("ok"):
            raise EngineGateError(f"Offline bootstrap failed gate_result probe: {probe.get('error')}")
        vers = detect_local_versions(eng_dir, rt_dir)
        state = {
            "state": STATE_UPDATED,
            "localPobVersion": probe.get("pobVersion") or vers["localPobVersion"],
            "remotePobVersion": probe.get("pobVersion") or vers["localPobVersion"],
            "pobToolsVersion": vers["pobToolsVersion"],
            "pobToolsTag": vers["pobToolsTag"],
            "dataVersion": vers["dataVersion"],
            "checkedAt": time.time(),
            "engineDir": str(eng_dir),
            "networkError": None,
            "userHint": None,
            "source": f"offline:{src}",
            "probe": probe,
        }
        save_update_state(state, rt_dir)
        return state

    # 2. If legacy_dir is explicitly provided (or discovered when target is empty), seed + incremental update
    leg = _find_legacy_dir(legacy_dir) if legacy_dir is not None else None
    if leg is not None and not is_valid_engine_dir(eng_dir):
        seed_from_legacy_dir(leg, eng_dir)
        return check_and_apply_updates(
            engine_dir=eng_dir,
            runtime_dir=rt_dir,
            proxy=proxy,
            mirror=mirror,
            include_zh=include_zh,
            force=True,
            probe_fn=probe_callable,
        )

    # 3. Clean online dual-upstream bootstrap from GitHub (PobTools-zh + PathOfBuilding master)
    staging_dir = eng_dir / ".staging"
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tools = pool.submit(
                fetch_pobtools_latest_info,
                proxy=proxy,
                mirror=mirror,
                timeout=CHECK_TIMEOUT,
                fetch_manifest=True,
            )
            fut_pob = pool.submit(
                fetch_pob_manifest_remote,
                etag=None,
                proxy=proxy,
                mirror=mirror,
                timeout=CHECK_TIMEOUT,
            )
            tools_info = fut_tools.result()
            _, pob_manifest, pob_etag = fut_pob.result()
        assert pob_manifest is not None

        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True, exist_ok=True)
        eng_dir.mkdir(parents=True, exist_ok=True)

        staged_files: dict[Path, Path] = {}

        # Download & stage PobTools-zh full host zip
        tools_manifest = tools_info.get("manifest")
        if not isinstance(tools_manifest, dict):
            raise UpdateError("PobTools-zh release manifest JSON missing.")
        app_asset = _pick_pobtools_asset(tools_manifest, prefer_update=False)
        if not app_asset:
            raise UpdateError("No PobTools-*.zip asset found in release manifest.")
        staged_files.update(
            _download_and_stage_release_zip(
                tools_info["tag"],
                app_asset,
                staging_dir,
                proxy=proxy,
                mirror=mirror,
                skip_prefixes=("PoE1",),
            )
        )

        # Optional bilingual DataV2 pack (honoring minApp)
        installed_data_ver: str | None = None
        data_manifest = tools_info.get("dataManifest")
        if include_zh and isinstance(data_manifest, dict):
            min_app = data_manifest.get("minApp")
            if _is_version_gte(tools_info["version"], min_app):
                data_asset = _pick_data_asset(data_manifest)
                if data_asset:
                    data_tag = str(data_manifest.get("tag") or tools_manifest.get("dataTag") or "")
                    staged_files.update(
                        _download_and_stage_release_zip(
                            data_tag,
                            data_asset,
                            staging_dir,
                            proxy=proxy,
                            mirror=mirror,
                            skip_prefixes=("PoE1",),
                        )
                    )
                    installed_data_ver = data_tag or None

        # Download & stage official PoB PoE1/ files
        to_download, _ = diff_pob_manifest(eng_dir / "PoE1", pob_manifest, verify_local_sha1=True)
        staged_files.update(
            _download_pob_files_to_staging(
                pob_manifest,
                to_download,
                staging_dir,
                proxy=proxy,
                mirror=mirror,
            )
        )
        manifest_staged = staging_dir / "PoE1" / "manifest.xml"
        manifest_staged.parent.mkdir(parents=True, exist_ok=True)
        manifest_staged.write_text(_render_local_manifest_xml(pob_manifest), encoding="utf-8")
        staged_files[Path("PoE1") / "manifest.xml"] = manifest_staged

        apply_res = _apply_staged_files_with_gate(eng_dir, staged_files, probe_fn=probe_callable)
        if not apply_res["ok"]:
            err = apply_res.get("error") or "gate_result failed"
            state = {
                "state": STATE_DEGRADED_ROLLBACK,
                "localPobVersion": None,
                "remotePobVersion": pob_manifest.version,
                "pobToolsVersion": None,
                "dataVersion": None,
                "checkedAt": time.time(),
                "engineDir": str(eng_dir),
                "networkError": err,
                "userHint": _build_user_hint(STATE_DEGRADED_ROLLBACK, None, err),
            }
            save_update_state(state, rt_dir)
            raise EngineGateError(err)

        _write_pobtools_meta(
            eng_dir,
            pobtools_version=tools_info["version"],
            pobtools_tag=tools_info["tag"],
            data_version=installed_data_ver,
        )
        probe = apply_res["probe"]
        state = {
            "state": STATE_UPDATED,
            "localPobVersion": pob_manifest.version,
            "remotePobVersion": pob_manifest.version,
            "pobToolsVersion": tools_info["version"],
            "pobToolsTag": tools_info["tag"],
            "dataVersion": installed_data_ver or detect_local_versions(eng_dir, rt_dir)["dataVersion"],
            "pobManifestEtag": pob_etag,
            "checkedAt": time.time(),
            "engineDir": str(eng_dir),
            "networkError": None,
            "userHint": None,
            "updatedFilesCount": len(staged_files),
            "probe": probe,
        }
        save_update_state(state, rt_dir)
        return state
    except (httpx.HTTPError, OSError, UpdateError) as exc:
        if isinstance(exc, EngineGateError):
            raise
        shutil.rmtree(staging_dir, ignore_errors=True)
        err_msg = f"{type(exc).__name__}: {exc}"
        state = {
            "state": STATE_BOOTSTRAP_MISSING,
            "localPobVersion": None,
            "remotePobVersion": None,
            "pobToolsVersion": None,
            "dataVersion": None,
            "checkedAt": time.time(),
            "engineDir": str(eng_dir),
            "networkError": err_msg,
            "userHint": _build_user_hint(STATE_BOOTSTRAP_MISSING, None, err_msg),
        }
        save_update_state(state, rt_dir)
        return state


def check_and_apply_updates(
    *,
    engine_dir: Path | None = None,
    runtime_dir: Path | None = None,
    legacy_dir: Path | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    offline: bool = False,
    check_only: bool = False,
    force: bool = False,
    include_zh: bool = True,
    probe_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check GitHub upstreams (with fast-fail timeout) and atomically apply incremental updates.

    Never invokes PoB's internal UpdateCheck.lua / UpdateApply.lua or api.github.com.
    Never modifies legacy PobTools[20261102]; seeds into <runtime_dir>/engine before writing.
    """
    rt_dir = _get_runtime_dir(runtime_dir)
    prev_state = load_update_state(rt_dir)
    probe_callable = probe_fn or probe_engine_gate
    managed_dir = rt_dir / "engine"

    # Determine active inspection directory & whether we need to seed managed_dir before writing
    needs_seed_from_legacy: Path | None = None
    explicit_engine = engine_dir is not None

    if engine_dir is not None:
        active_dir = Path(engine_dir)
    else:
        env_root = os.environ.get("POB_ROOT", "").strip()
        if env_root and is_valid_engine_dir(Path(env_root)):
            active_dir = Path(env_root)
            explicit_engine = True
        elif is_valid_engine_dir(managed_dir):
            active_dir = managed_dir
        else:
            leg = _find_legacy_dir(legacy_dir)
            if leg is not None:
                active_dir = leg
                needs_seed_from_legacy = leg
            else:
                # No local engine at all
                if offline:
                    err = "offline mode enabled and no local PoB engine found"
                    state = {
                        "state": STATE_BOOTSTRAP_MISSING,
                        "localPobVersion": None,
                        "remotePobVersion": None,
                        "pobToolsVersion": None,
                        "dataVersion": None,
                        "checkedAt": time.time(),
                        "engineDir": str(managed_dir),
                        "networkError": err,
                        "userHint": _build_user_hint(STATE_BOOTSTRAP_MISSING, None, err),
                    }
                    save_update_state(state, rt_dir)
                    return state
                return bootstrap_engine(
                    target_dir=managed_dir,
                    runtime_dir=rt_dir,
                    proxy=proxy,
                    mirror=mirror,
                    include_zh=include_zh,
                    probe_fn=probe_callable,
                )

    if not is_valid_engine_dir(active_dir):
        if offline:
            err = f"engine directory invalid: {active_dir}"
            state = {
                "state": STATE_BOOTSTRAP_MISSING,
                "localPobVersion": None,
                "remotePobVersion": None,
                "pobToolsVersion": None,
                "dataVersion": None,
                "checkedAt": time.time(),
                "engineDir": str(active_dir),
                "networkError": err,
                "userHint": _build_user_hint(STATE_BOOTSTRAP_MISSING, None, err),
            }
            save_update_state(state, rt_dir)
            return state
        return bootstrap_engine(
            target_dir=active_dir,
            runtime_dir=rt_dir,
            proxy=proxy,
            mirror=mirror,
            include_zh=include_zh,
            probe_fn=probe_callable,
        )

    local_vers = detect_local_versions(active_dir, rt_dir)
    local_pob_ver = local_vers["localPobVersion"]
    local_tools_ver = local_vers["pobToolsVersion"]
    local_tools_tag = local_vers["pobToolsTag"]
    local_data_ver = local_vers["dataVersion"]

    if offline:
        state = {
            "state": STATE_UP_TO_DATE,
            "localPobVersion": local_pob_ver,
            "remotePobVersion": prev_state.get("remotePobVersion"),
            "pobToolsVersion": local_tools_ver,
            "pobToolsTag": local_tools_tag,
            "dataVersion": local_data_ver,
            "pobManifestEtag": prev_state.get("pobManifestEtag"),
            "checkedAt": time.time(),
            "engineDir": str(active_dir),
            "offline": True,
            "networkError": None,
            "userHint": None,
        }
        save_update_state(state, rt_dir)
        return state

    # Concurrent fast-fail metadata checks (< 3s connect timeout, no retries)
    prev_etag = None if force else prev_state.get("pobManifestEtag")
    # Only send ETag if previous state was for the same local PoB version
    if prev_etag and prev_state.get("localPobVersion") != local_pob_ver:
        prev_etag = None

    tools_info: dict[str, Any] | None = None
    pob_status: str | None = None
    remote_manifest: PobManifest | None = None
    new_etag: str | None = prev_etag
    net_errors: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_pob = pool.submit(
            fetch_pob_manifest_remote,
            etag=prev_etag,
            proxy=proxy,
            mirror=mirror,
            timeout=CHECK_TIMEOUT,
        )
        fut_tools = pool.submit(
            fetch_pobtools_latest_info,
            proxy=proxy,
            mirror=mirror,
            timeout=CHECK_TIMEOUT,
            fetch_manifest=False,
        )
        try:
            pob_status, remote_manifest, new_etag = fut_pob.result()
        except Exception as exc:  # noqa: BLE001
            net_errors.append(f"PoB manifest: {type(exc).__name__}: {exc}")

        try:
            tools_info = fut_tools.result()
        except Exception as exc:  # noqa: BLE001
            net_errors.append(f"PobTools-zh: {type(exc).__name__}: {exc}")

    if net_errors:
        err_msg = "; ".join(net_errors)
        state = {
            "state": STATE_DEGRADED_NETWORK,
            "localPobVersion": local_pob_ver,
            "remotePobVersion": remote_manifest.version if remote_manifest else None,
            "pobToolsVersion": local_tools_ver,
            "pobToolsTag": local_tools_tag,
            "dataVersion": local_data_ver,
            "pobManifestEtag": prev_state.get("pobManifestEtag"),
            "checkedAt": time.time(),
            "engineDir": str(active_dir),
            "networkError": err_msg,
            "userHint": _build_user_hint(STATE_DEGRADED_NETWORK, local_pob_ver, err_msg),
        }
        save_update_state(state, rt_dir)
        return state

    # On 304 Not Modified, still verify local PoE1/ files against local manifest.xml
    # so locally tampered files (e.g. PoE1/UpdateCheck.lua) are detected and repaired.
    poe1_dir = active_dir / "PoE1"
    to_download: list[ManifestFileEntry] = []
    to_delete: list[Path] = []
    remote_pob_ver = local_pob_ver

    if pob_status == "not_modified":
        tampered = find_local_manifest_mismatches(poe1_dir)
        if tampered:
            # Fetch full manifest (without ETag) to repair tampered files against latest upstream
            try:
                pob_status, remote_manifest, new_etag = fetch_pob_manifest_remote(
                    etag=None,
                    proxy=proxy,
                    mirror=mirror,
                    timeout=CHECK_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                err_msg = f"PoB manifest repair fetch: {type(exc).__name__}: {exc}"
                state = {
                    "state": STATE_DEGRADED_NETWORK,
                    "localPobVersion": local_pob_ver,
                    "remotePobVersion": None,
                    "pobToolsVersion": local_tools_ver,
                    "pobToolsTag": local_tools_tag,
                    "dataVersion": local_data_ver,
                    "pobManifestEtag": prev_state.get("pobManifestEtag"),
                    "checkedAt": time.time(),
                    "engineDir": str(active_dir),
                    "networkError": err_msg,
                    "userHint": _build_user_hint(STATE_DEGRADED_NETWORK, local_pob_ver, err_msg),
                }
                save_update_state(state, rt_dir)
                return state

    if remote_manifest is not None:
        remote_pob_ver = remote_manifest.version
        to_download, to_delete = diff_pob_manifest(poe1_dir, remote_manifest, verify_local_sha1=True)

    # Evaluate PobTools-zh & DataV2 update availability
    assert tools_info is not None
    remote_tools_tag = tools_info["tag"]
    remote_tools_ver = tools_info["version"]
    tools_needs_update = force or (
        remote_tools_tag != local_tools_tag and _is_version_newer(remote_tools_ver, local_tools_ver)
    )
    tools_manifest: dict[str, Any] | None = None
    data_manifest: dict[str, Any] | None = None
    data_needs_update = False
    remote_data_tag: str | None = None

    if tools_needs_update or (include_zh and not local_data_ver) or force or (remote_tools_tag != local_tools_tag):
        try:
            full_tools = fetch_pobtools_latest_info(
                proxy=proxy,
                mirror=mirror,
                timeout=CHECK_TIMEOUT,
                fetch_manifest=True,
            )
            tools_info = full_tools
            tools_manifest = full_tools.get("manifest")
            data_manifest = full_tools.get("dataManifest")
            remote_tools_ver = full_tools["version"]
            if isinstance(tools_manifest, dict) and tools_manifest.get("dataTag"):
                remote_data_tag = str(tools_manifest["dataTag"])
            if include_zh and isinstance(data_manifest, dict) and remote_data_tag:
                target_app_ver = remote_tools_ver if tools_needs_update else (local_tools_ver or remote_tools_ver)
                if (force or remote_data_tag != local_data_ver) and _is_version_gte(
                    target_app_ver, data_manifest.get("minApp")
                ):
                    data_needs_update = True
        except Exception as exc:  # noqa: BLE001
            err_msg = f"PobTools-zh manifest: {type(exc).__name__}: {exc}"
            state = {
                "state": STATE_DEGRADED_NETWORK,
                "localPobVersion": local_pob_ver,
                "remotePobVersion": remote_pob_ver,
                "pobToolsVersion": local_tools_ver,
                "pobToolsTag": local_tools_tag,
                "dataVersion": local_data_ver,
                "pobManifestEtag": prev_state.get("pobManifestEtag"),
                "checkedAt": time.time(),
                "engineDir": str(active_dir),
                "networkError": err_msg,
                "userHint": _build_user_hint(STATE_DEGRADED_NETWORK, local_pob_ver, err_msg),
            }
            save_update_state(state, rt_dir)
            return state

    pob_needs_update = bool(to_download or to_delete or (remote_manifest and remote_pob_ver != local_pob_ver))
    anything_to_update = pob_needs_update or tools_needs_update or data_needs_update

    if not anything_to_update:
        state = {
            "state": STATE_UP_TO_DATE,
            "localPobVersion": local_pob_ver,
            "remotePobVersion": remote_pob_ver,
            "pobToolsVersion": local_tools_ver or remote_tools_ver,
            "pobToolsTag": local_tools_tag or remote_tools_tag,
            "dataVersion": local_data_ver or remote_data_tag,
            "pobManifestEtag": new_etag,
            "checkedAt": time.time(),
            "engineDir": str(active_dir),
            "updateAvailable": False,
            "networkError": None,
            "userHint": None,
        }
        save_update_state(state, rt_dir)
        return state

    if check_only:
        state = {
            "state": STATE_UP_TO_DATE,
            "localPobVersion": local_pob_ver,
            "remotePobVersion": remote_pob_ver,
            "pobToolsVersion": local_tools_ver,
            "remotePobToolsVersion": remote_tools_ver,
            "pobToolsTag": local_tools_tag,
            "dataVersion": local_data_ver,
            "pobManifestEtag": prev_state.get("pobManifestEtag"),
            "checkedAt": time.time(),
            "engineDir": str(active_dir),
            "updateAvailable": True,
            "pobFilesChanged": len(to_download),
            "pobFilesDeleted": len(to_delete),
            "pobToolsUpdateAvailable": tools_needs_update,
            "dataUpdateAvailable": data_needs_update,
            "networkError": None,
            "userHint": None,
        }
        save_update_state(state, rt_dir)
        return state

    # Seed managed runtime from legacy directory before applying any writes so legacy dir stays read-only
    write_dir = active_dir
    if needs_seed_from_legacy is not None and not explicit_engine:
        seed_from_legacy_dir(needs_seed_from_legacy, managed_dir)
        write_dir = managed_dir

    staging_dir = write_dir / ".staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    staged_files: dict[Path, Path] = {}
    try:
        if remote_manifest is not None and pob_needs_update:
            staged_files.update(
                _download_pob_files_to_staging(
                    remote_manifest,
                    to_download,
                    staging_dir,
                    proxy=proxy,
                    mirror=mirror,
                )
            )
            manifest_staged = staging_dir / "PoE1" / "manifest.xml"
            manifest_staged.parent.mkdir(parents=True, exist_ok=True)
            manifest_staged.write_text(_render_local_manifest_xml(remote_manifest), encoding="utf-8")
            staged_files[Path("PoE1") / "manifest.xml"] = manifest_staged

        if tools_needs_update and isinstance(tools_manifest, dict):
            asset = _pick_pobtools_asset(tools_manifest, prefer_update=True)
            if asset:
                staged_files.update(
                    _download_and_stage_release_zip(
                        remote_tools_tag,
                        asset,
                        staging_dir,
                        proxy=proxy,
                        mirror=mirror,
                        skip_prefixes=("PoE1",),
                    )
                )

        if data_needs_update and isinstance(data_manifest, dict) and remote_data_tag:
            d_asset = _pick_data_asset(data_manifest)
            if d_asset:
                staged_files.update(
                    _download_and_stage_release_zip(
                        remote_data_tag,
                        d_asset,
                        staging_dir,
                        proxy=proxy,
                        mirror=mirror,
                        skip_prefixes=("PoE1",),
                    )
                )
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(staging_dir, ignore_errors=True)
        err_msg = f"Download/stage failed: {type(exc).__name__}: {exc}"
        state = {
            "state": STATE_DEGRADED_NETWORK,
            "localPobVersion": local_pob_ver,
            "remotePobVersion": remote_pob_ver,
            "pobToolsVersion": local_tools_ver,
            "pobToolsTag": local_tools_tag,
            "dataVersion": local_data_ver,
            "pobManifestEtag": prev_state.get("pobManifestEtag"),
            "checkedAt": time.time(),
            "engineDir": str(write_dir),
            "networkError": err_msg,
            "userHint": _build_user_hint(STATE_DEGRADED_NETWORK, local_pob_ver, err_msg),
        }
        save_update_state(state, rt_dir)
        return state

    apply_res = _apply_staged_files_with_gate(
        write_dir,
        staged_files,
        deleted_rel_paths=to_delete,
        probe_fn=probe_callable,
    )
    if not apply_res["ok"]:
        err_msg = apply_res.get("error") or "gate_result probe failed"
        state = {
            "state": STATE_DEGRADED_ROLLBACK,
            "localPobVersion": local_pob_ver,
            "remotePobVersion": remote_pob_ver,
            "pobToolsVersion": local_tools_ver,
            "pobToolsTag": local_tools_tag,
            "dataVersion": local_data_ver,
            "pobManifestEtag": prev_state.get("pobManifestEtag"),
            "checkedAt": time.time(),
            "engineDir": str(write_dir),
            "rolledBack": True,
            "networkError": err_msg,
            "userHint": _build_user_hint(STATE_DEGRADED_ROLLBACK, local_pob_ver, err_msg),
        }
        save_update_state(state, rt_dir)
        return state

    final_tools_ver = remote_tools_ver if tools_needs_update else (local_tools_ver or remote_tools_ver)
    final_tools_tag = remote_tools_tag if tools_needs_update else (local_tools_tag or remote_tools_tag)
    final_data_ver = remote_data_tag if data_needs_update else (local_data_ver or remote_data_tag)
    if tools_needs_update or data_needs_update:
        _write_pobtools_meta(
            write_dir,
            pobtools_version=final_tools_ver,
            pobtools_tag=final_tools_tag,
            data_version=final_data_ver,
        )

    final_pob_ver = remote_pob_ver if pob_needs_update else local_pob_ver
    state = {
        "state": STATE_UPDATED,
        "localPobVersion": final_pob_ver,
        "remotePobVersion": remote_pob_ver,
        "pobToolsVersion": final_tools_ver,
        "pobToolsTag": final_tools_tag,
        "dataVersion": final_data_ver,
        "pobManifestEtag": new_etag,
        "checkedAt": time.time(),
        "engineDir": str(write_dir),
        "updatedFilesCount": len(staged_files),
        "deletedFilesCount": len(to_delete),
        "networkError": None,
        "userHint": None,
        "probe": apply_res["probe"],
    }
    save_update_state(state, rt_dir)
    return state
