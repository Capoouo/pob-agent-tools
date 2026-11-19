"""Headless PoB bridge subprocess wrapper (lossless transport, thread-safe, auto-healing)."""

from __future__ import annotations

import base64
import collections
import json
import os
import queue
import re
import subprocess
import threading
import time
import zlib
from pathlib import Path
from typing import Any

from .updater import resolve_engine_root

DEFAULT_PROJECT_ROOT = Path(os.environ.get("POB_HOME", str(Path(__file__).resolve().parents[3])))
DEFAULT_POB_ROOT = resolve_engine_root()
DEFAULT_POB_DIR = DEFAULT_POB_ROOT
RUNTIME_DIR = Path(os.environ.get("POB_RUNTIME_DIR", str(DEFAULT_PROJECT_ROOT / ".runtime")))
MAX_UNDO_STEPS = 30

# Hardcoded red-line RPC blocklist (R1, R3, R4, R7, R11; R2 sidebar_breakdown is checked dynamically via caps)
FORBIDDEN_METHODS: dict[str, str] = {
    "compare_load": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_state": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_select": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_remove": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_set": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_tree": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_items": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_skills": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_config": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_use": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "config_mod_search": "R3: configModBrowser is false on 2.67.2; use list_config/set_config/set_custom_mods.",
    "config_mod_add": "R3: configModBrowser is false on 2.67.2; use list_config/set_config/set_custom_mods.",
    "set_pob_options": "R4: Writing Settings.xml at runtime is forbidden across workers.",
    "oauth_logout": "R4: Writing Settings.xml at runtime is forbidden across workers.",
    "import_from_uri": "R7: import_from_uri rejects non-ASCII/parentheses; use poe.ninja raw/overview/code + import_code.",
    "save_build_as": "R11: Direct disk writes in engine are restricted to buildPath; use save_xml and write locally in CLI.",
}

_TREE_VERSION_ATTR_RE = re.compile(r'treeVersion=["\']([^"\']+)["\']')
_TREE_VERSION_NUM_RE = re.compile(r"^(\d+)_(\d+)")


def _decode_share_code_xml(code: str) -> str | None:
    """Best-effort decode of a PoB share code (or raw XML string) to XML text."""
    text = (code or "").strip()
    if not text:
        return None
    if text.startswith("<"):
        return text
    try:
        cleaned = text.replace("-", "+").replace("_", "/")
        pad = (-len(cleaned)) % 4
        if pad:
            cleaned += "=" * pad
        raw = base64.b64decode(cleaned)
        try:
            xml_bytes = zlib.decompress(raw)
        except zlib.error:
            xml_bytes = zlib.decompress(raw, -15)
        return xml_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_xml_tree_versions(xml_text: str) -> list[str]:
    return _TREE_VERSION_ATTR_RE.findall(xml_text)


def _parse_tree_version_tuple(tv: str) -> tuple[int, int] | None:
    m = _TREE_VERSION_NUM_RE.match(tv.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _get_local_tree_versions(game_dir: Path) -> tuple[set[str], tuple[int, int] | None, str | None]:
    tree_data_dir = Path(game_dir) / "TreeData"
    if not tree_data_dir.is_dir():
        return set(), None, None
    names: set[str] = set()
    max_tuple: tuple[int, int] | None = None
    max_str: str | None = None
    for entry in tree_data_dir.iterdir():
        if not entry.is_dir():
            continue
        names.add(entry.name)
        parsed = _parse_tree_version_tuple(entry.name)
        if parsed is not None:
            if max_tuple is None or parsed > max_tuple:
                max_tuple = parsed
                max_str = f"{parsed[0]}_{parsed[1]}"
    return names, max_tuple, max_str


class BridgeError(RuntimeError):
    """Raised when the headless PoB bridge returns an error or violates a contract."""


class BridgeTimeoutError(BridgeError):
    """Raised when an RPC call exceeds its deadline and the worker is recycled."""


class BridgeWorker:
    """Manages a single pob-zh.exe --engine-headless subprocess with lock, timeout, and stderr drain."""

    def __init__(
        self,
        slot_name: str = "main",
        pob_root: Path | None = None,
        pob_dir: Path | None = None,
    ) -> None:
        self.slot_name = slot_name
        if pob_dir is not None:
            resolved_root = Path(pob_dir)
        elif pob_root is not None:
            resolved_root = Path(pob_root)
        else:
            resolved_root = resolve_engine_root()
        self.pob_root = resolved_root
        self.exe_path = self.pob_root / "pob-zh.exe"
        self.game_dir = self.pob_root / "PoE1"

        self._lock = threading.RLock()
        self.lock = self._lock
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=100)
        self._req_counter = 0

        self.pob_version: str | None = None
        self.bridge_version: str | None = None
        self.hello_info: dict[str, Any] = {}
        self.caps: dict[str, Any] = {}
        self.last_used: float = time.time()
        self.source_label: str | None = None
        self.last_good_code: str | None = None

        # Undo / Redo stacks holding lossless export_code strings (max 30 steps)
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []

        # Cached static tree data per treeVersion
        self._tree_data_cache: dict[str, dict[str, Any]] = {}

    @property
    def last_active_ts(self) -> float:
        return self.last_used

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def is_alive(self) -> bool:
        return self.is_running

    def ensure_alive(self) -> None:
        with self._lock:
            if not self.is_running:
                self.start()

    def start(self) -> None:
        """Start pob-zh.exe --engine-headless and execute the mandatory 6-step handshake (R4, R8)."""
        with self._lock:
            if self.is_running:
                return
            self._kill_unlocked()

            if not self.exe_path.is_file():
                raise BridgeError(f"pob-zh.exe not found at {self.exe_path}")
            if not self.game_dir.is_dir():
                raise BridgeError(f"PoE1 directory not found at {self.game_dir}")

            out_queue: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
            self._stdout_queue = out_queue
            self._stderr_tail.clear()

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                [str(self.exe_path), "--engine-headless", str(self.game_dir)],
                cwd=str(self.game_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )

            proc = self._proc
            threading.Thread(
                target=self._stdout_reader_loop,
                args=(proc, out_queue),
                name=f"pob-{self.slot_name}-stdout",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._stderr_drain_loop,
                args=(proc,),
                name=f"pob-{self.slot_name}-stderr",
                daemon=True,
            ).start()

            # Step 3: Consume startup events until gate_result and hello
            gate_seen = False
            hello_seen = False
            deadline = time.monotonic() + 20.0
            while not (gate_seen and hello_seen):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_unlocked()
                    raise BridgeTimeoutError(
                        f"Timed out waiting for headless startup events (stderr: {list(self._stderr_tail)[-5:]})"
                    )
                try:
                    msg = self._stdout_queue.get(timeout=remaining)
                except queue.Empty as exc:
                    self._kill_unlocked()
                    raise BridgeTimeoutError("Timed out waiting for startup events") from exc

                if msg is None or isinstance(msg, Exception):
                    self._kill_unlocked()
                    raise BridgeError(f"Subprocess exited during boot: {msg} (stderr: {list(self._stderr_tail)[-5:]})")

                event = msg.get("event")
                data = msg.get("data") or {}
                if event == "gate_result":
                    if not data.get("ok"):
                        self._kill_unlocked()
                        raise BridgeError(f"PoB bridge gate_result failed: {data.get('failed')}")
                    gate_seen = True
                elif event == "hello":
                    self.hello_info = dict(data)
                    self.pob_version = data.get("pobVersion")
                    self.bridge_version = data.get("bridge")
                    hello_seen = True

            # Step 4: Call version{} to cache caps
            ver_info = self._raw_call_unlocked("version", {}, timeout=10.0)
            self.caps = ver_info.get("caps") or {}
            self.pob_version = ver_info.get("pobVersion") or self.pob_version

            # Step 5: Enable bilingual display translation
            if self.caps.get("translateToggle", True):
                self._raw_call_unlocked("set_translate", {"enabled": True}, timeout=5.0)

            # Step 6 (R4): Always call new_build{} regardless of initial buildLoaded state
            self._raw_call_unlocked("new_build", {}, timeout=10.0)
            if not self.last_good_code:
                self.load_slot_state()
            if self.last_good_code:
                try:
                    self._raw_call_unlocked("import_code", {"code": self.last_good_code, "mode": "replace"}, timeout=15.0)
                except Exception:
                    try:
                        self._raw_call_unlocked("new_build", {}, timeout=10.0)
                    except Exception:
                        pass
            self.last_used = time.time()

    @property
    def slot_state_path(self) -> Path:
        env_rt = os.environ.get("POB_RUNTIME_DIR", "").strip()
        rt = Path(env_rt) if env_rt else RUNTIME_DIR
        return rt / f"slot_{self.slot_name}.json"

    def save_slot_state(self) -> None:
        """Persist slot build code and undo/redo stacks so daemon restarts never lose state."""
        if self.slot_name == "test":
            return
        try:
            state_path = self.slot_state_path
            state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "slot": self.slot_name,
                "last_good_code": self.last_good_code,
                "undo_stack": self.undo_stack[-MAX_UNDO_STEPS:],
                "redo_stack": self.redo_stack[-MAX_UNDO_STEPS:],
                "source_label": self.source_label,
                "updated_at": time.time(),
            }
            state_path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass

    def load_slot_state(self) -> None:
        """Load persisted slot build code and undo/redo stacks if present."""
        if self.slot_name == "test" or not self.slot_state_path.exists():
            return
        try:
            data = json.loads(self.slot_state_path.read_text(encoding="utf-8"))
            self.last_good_code = data.get("last_good_code")
            self.undo_stack = list(data.get("undo_stack") or [])
            self.redo_stack = list(data.get("redo_stack") or [])
            self.source_label = data.get("source_label")
        except Exception:
            pass

    def _stdout_reader_loop(
        self,
        proc: subprocess.Popen[str],
        out_queue: queue.Queue[dict[str, Any] | Exception | None],
    ) -> None:
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

    def _stderr_drain_loop(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                stripped = line.rstrip("\r\n")
                if stripped:
                    self._stderr_tail.append(stripped)
        except Exception:  # noqa: BLE001
            pass

    def _kill_unlocked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                pass
            self._proc = None

    def stop(self) -> None:
        with self._lock:
            self._kill_unlocked()

    def _raw_call_unlocked(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        if method in FORBIDDEN_METHODS:
            raise BridgeError(FORBIDDEN_METHODS[method])

        if not self.is_running:
            self.start()

        # Dynamic R2: allow sidebar_breakdown iff runtime caps.sidebarBreakdown is True
        if method == "sidebar_breakdown" and not bool(self.caps.get("sidebarBreakdown")):
            raise BridgeError(
                "R2: sidebarBreakdown is false on this engine version; use get_calcs + calcs_breakdown."
            )

        assert self._proc is not None and self._proc.stdin is not None
        self._req_counter += 1
        req_id = self._req_counter
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}}, ensure_ascii=False)

        try:
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            self._recover_after_crash_unlocked()
            raise BridgeError(f"Failed writing to pob-zh stdin on method '{method}': {exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._recover_after_crash_unlocked()
                raise BridgeTimeoutError(f"RPC '{method}' timed out after {timeout:.1f}s; worker restarted and healed.")
            try:
                msg = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                self._recover_after_crash_unlocked()
                raise BridgeTimeoutError(
                    f"RPC '{method}' timed out after {timeout:.1f}s; worker restarted and healed."
                ) from exc

            if msg is None or isinstance(msg, Exception):
                self._recover_after_crash_unlocked()
                raise BridgeError(f"Headless engine crashed during '{method}': {msg}")

            # Ignore asynchronous events or stale response IDs (R8)
            if msg.get("id") != req_id:
                continue

            self.last_used = time.time()
            if msg.get("ok") is False or ("error" in msg and msg["error"] is not None):
                err_obj = msg.get("error")
                if isinstance(err_obj, dict):
                    err_text = err_obj.get("message") or json.dumps(err_obj, ensure_ascii=False)
                else:
                    err_text = str(err_obj or "unknown bridge error")
                raise BridgeError(f"RPC '{method}' failed: {err_text}")

            result = msg.get("result")
            return result if isinstance(result, dict) else {"value": result}

    def _recover_after_crash_unlocked(self) -> None:
        """Kill hung/crashed subprocess, restart cleanly, and restore last_good_code if available."""
        self._kill_unlocked()
        try:
            self.start()
        except Exception:  # noqa: BLE001
            pass

    def _check_tree_version_mismatch(self, code: str) -> str | None:
        """Return an actionable mismatch message if code declares a treeVersion newer than local TreeData."""
        xml_text = _decode_share_code_xml(code)
        if not xml_text:
            return None
        requested_versions = _extract_xml_tree_versions(xml_text)
        if not requested_versions:
            return None
        local_names, max_tuple, max_str = _get_local_tree_versions(self.game_dir)
        if max_tuple is None or not max_str:
            return None
        for tv in requested_versions:
            if tv in local_names:
                continue
            parsed = _parse_tree_version_tuple(tv)
            if parsed is not None and parsed > max_tuple:
                pob_ver = self.pob_version or "unknown"
                return (
                    f"Build requires treeVersion='{tv}', which is newer than local engine max "
                    f"TreeData='{max_str}' (PoB {pob_ver}). "
                    "Run `pob update` (or set HTTPS_PROXY / POB_PROXY and run "
                    "`pob update --proxy http://127.0.0.1:<port>`) to update the local engine."
                )
        return None

    def _restore_after_failed_import_unlocked(self) -> None:
        """Heal the slot after a failed import_code (which may leave b:Shutdown() active / no build loaded)."""
        try:
            # Must call new_build{} first because b:Shutdown() breaks ensure_build() for import_code
            self._raw_call_unlocked("new_build", {}, timeout=10.0)
            if self.last_good_code:
                try:
                    self._raw_call_unlocked(
                        "import_code",
                        {"code": self.last_good_code, "mode": "replace"},
                        timeout=15.0,
                    )
                except Exception:  # noqa: BLE001
                    self._raw_call_unlocked("new_build", {}, timeout=10.0)
        except Exception:  # noqa: BLE001
            self._recover_after_crash_unlocked()

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Thread-safe RPC call with automatic timeout selection, dynamic R2, and import_code deadlock healing."""
        if timeout is None:
            timeout = 30.0 if method in {"import_from_url", "node_power", "gem_search"} else 15.0
        with self._lock:
            if method == "import_code":
                code = str((params or {}).get("code") or "")
                tv_mismatch = self._check_tree_version_mismatch(code)
                if tv_mismatch:
                    raise BridgeError(f"RPC 'import_code' failed: {tv_mismatch}")
                try:
                    return self._raw_call_unlocked(method, params, timeout=timeout)
                except BridgeError as exc:
                    self._restore_after_failed_import_unlocked()
                    tv_hint = self._check_tree_version_mismatch(code)
                    if tv_hint and tv_hint not in str(exc):
                        raise BridgeError(f"{exc} | {tv_hint}") from exc
                    raise
            return self._raw_call_unlocked(method, params, timeout=timeout)

    def update_checkpoint(self) -> str:
        """Export current build code into last_good_code for crash recovery."""
        with self._lock:
            res = self._raw_call_unlocked("export_code", {}, timeout=10.0)
            code = res.get("code", "")
            if code:
                self.last_good_code = code
            return code

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        """Fetch and cache static tree_data for the active tree version."""
        with self._lock:
            if not tree_version:
                info = self._raw_call_unlocked("get_build_info", {}, timeout=10.0)
                tree_version = info.get("treeVersion") or "latest"
            if tree_version not in self._tree_data_cache:
                params = {"version": tree_version} if tree_version != "latest" else {}
                self._tree_data_cache[tree_version] = self._raw_call_unlocked("tree_data", params, timeout=20.0)
            return self._tree_data_cache[tree_version]
