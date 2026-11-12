"""Headless PoB bridge subprocess wrapper (lossless transport, thread-safe, auto-healing)."""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_PROJECT_ROOT = Path(os.environ.get("POB_HOME", str(Path(__file__).resolve().parents[3])))
DEFAULT_POB_ROOT = Path(os.environ.get("POB_ROOT", str(DEFAULT_PROJECT_ROOT / "PobTools[20261102]")))
DEFAULT_POB_DIR = DEFAULT_POB_ROOT
RUNTIME_DIR = Path(os.environ.get("POB_RUNTIME_DIR", str(DEFAULT_PROJECT_ROOT / ".runtime")))
MAX_UNDO_STEPS = 30

# Hardcoded red-line RPC blocklist (R1, R2, R3, R4, R7, R11)
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
    "sidebar_breakdown": "R2: sidebarBreakdown is false on 2.67.2; use get_calcs + calcs_breakdown.",
    "config_mod_search": "R3: configModBrowser is false on 2.67.2; use list_config/set_config/set_custom_mods.",
    "config_mod_add": "R3: configModBrowser is false on 2.67.2; use list_config/set_config/set_custom_mods.",
    "set_pob_options": "R4: Writing Settings.xml at runtime is forbidden across workers.",
    "oauth_logout": "R4: Writing Settings.xml at runtime is forbidden across workers.",
    "import_from_uri": "R7: import_from_uri rejects non-ASCII/parentheses; use poe.ninja raw/overview/code + import_code.",
    "save_build_as": "R11: Direct disk writes in engine are restricted to buildPath; use save_xml and write locally in CLI.",
}


class BridgeError(RuntimeError):
    """Raised when the headless PoB bridge returns an error or violates a contract."""


class BridgeTimeoutError(BridgeError):
    """Raised when an RPC call exceeds its deadline and the worker is recycled."""


class BridgeWorker:
    """Manages a single pob-zh.exe --engine-headless subprocess with lock, timeout, and stderr drain."""

    def __init__(
        self,
        slot_name: str = "main",
        pob_root: Path = DEFAULT_POB_ROOT,
        pob_dir: Path | None = None,
    ) -> None:
        self.slot_name = slot_name
        self.pob_root = Path(pob_dir or pob_root)
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
                    pass
            self.last_used = time.time()

    @property
    def slot_state_path(self) -> Path:
        return RUNTIME_DIR / f"slot_{self.slot_name}.json"

    def save_slot_state(self) -> None:
        """Persist slot build code and undo/redo stacks so daemon restarts never lose state."""
        if self.slot_name == "test":
            return
        try:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "slot": self.slot_name,
                "last_good_code": self.last_good_code,
                "undo_stack": self.undo_stack[-MAX_UNDO_STEPS:],
                "redo_stack": self.redo_stack[-MAX_UNDO_STEPS:],
                "source_label": self.source_label,
                "updated_at": time.time(),
            }
            self.slot_state_path.write_text(json.dumps(payload), encoding="utf-8")
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

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Thread-safe RPC call with automatic timeout selection and lossless return."""
        if timeout is None:
            timeout = 30.0 if method in {"import_from_url", "node_power", "gem_search"} else 15.0
        with self._lock:
            res = self._raw_call_unlocked(method, params, timeout=timeout)
            return res

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
