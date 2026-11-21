"""Local HTTP daemon managing persistent named headless PoB workers with per-slot locking."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bridge_client import (
    DEFAULT_POB_DIR,
    DEFAULT_PROJECT_ROOT,
    RUNTIME_DIR,
    BridgeError,
    BridgeWorker,
)
from .updater import (
    OK_STATES,
    STATE_BOOTSTRAP_MISSING,
    check_and_apply_updates,
    format_stderr_update_alert,
    get_runtime_dir,
    get_update_warning,
    is_valid_engine_dir,
    load_update_state,
    resolve_engine_root,
)

DAEMON_STATE_FILE = RUNTIME_DIR / "daemon.json"
DAEMON_LOCK_FILE = RUNTIME_DIR / "daemon.start.lock"
REF_IDLE_TIMEOUT_SEC = 300.0        # 5 minutes (for all non-main slots)
GLOBAL_IDLE_TIMEOUT_SEC = 1800.0    # 30 minutes
UPDATE_RECHECK_INTERVAL_SEC = 1800.0  # 30 minutes

_SLOT_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5

_stderr_alert_emitted = False
_stderr_alert_checked_at: float | None = None
_kernel32: Any = None


class BootstrapMissingError(RuntimeError):
    """Raised when no local PoB engine exists and GitHub bootstrap failed or is offline."""

    def __init__(self, message: str, update_status: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.update_status = update_status or {}


def normalize_slot_name(slot: str | None) -> str:
    """Validate and normalize a named PoB worker slot slug (`^[a-zA-Z0-9._-]{1,48}$`)."""
    if slot is None:
        return "main"
    cleaned = str(slot).strip()
    if not cleaned:
        return "main"
    if cleaned in (".", "..") or not _SLOT_NAME_RE.match(cleaned):
        raise ValueError(
            f"Invalid slot name {slot!r}: must match ^[a-zA-Z0-9._-]{{1,48}}$ and not be '.' or '..'."
        )
    low = cleaned.lower()
    if low in ("main", "ref"):
        return low
    return cleaned


def _get_runtime_dir(runtime_dir: Path | None = None) -> Path:
    return get_runtime_dir(runtime_dir)


def _build_daemon_spawn_cmd(runtime_dir: Path | None = None) -> list[str]:
    """Build the command line to spawn the background daemon (supports Python and frozen exe)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_daemon"]
    else:
        cmd = [sys.executable, "-m", "pob_cli.daemon"]
    if runtime_dir is not None:
        cmd.extend(["--runtime-dir", str(runtime_dir)])
    return cmd


def _daemon_state_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.json"


def _daemon_lock_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.start.lock"


def _daemon_boot_error_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.boot_error.json"


def _is_offline_env() -> bool:
    val = os.environ.get("POB_OFFLINE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _get_kernel32() -> Any:
    global _kernel32
    if _kernel32 is None:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.GetExitCodeProcess.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        _kernel32 = k32
    return _kernel32


def _is_pid_alive(pid: int) -> bool:
    """Fast (<1ms) OS-native process liveness probe to avoid 2s stale loopback TCP timeouts."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        if pid > 0xFFFFFFFF:
            return False
        import ctypes

        k32 = _get_kernel32()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            err = ctypes.get_last_error()
            return err == _ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_uint32(0)
            if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def reset_stderr_alert_flag() -> None:
    """Reset the per-process stderr update alert deduplication flag (used by tests)."""
    global _stderr_alert_emitted, _stderr_alert_checked_at
    _stderr_alert_emitted = False
    _stderr_alert_checked_at = None


def emit_stderr_update_alert(state: dict[str, Any] | None, *, force: bool = False) -> bool:
    """Print the prominent single-block Chinese update alert to stderr (deduplicated per checkedAt/process)."""
    global _stderr_alert_emitted, _stderr_alert_checked_at
    checked_at_raw = state.get("checkedAt") if isinstance(state, dict) else None
    checked_at = float(checked_at_raw) if isinstance(checked_at_raw, (int, float)) else None
    if _stderr_alert_emitted and not force:
        if checked_at is None or checked_at == _stderr_alert_checked_at:
            return False
    alert = format_stderr_update_alert(state)
    if not alert:
        return False
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.stderr.write(alert + "\n")
    sys.stderr.flush()
    _stderr_alert_emitted = True
    if checked_at is not None:
        _stderr_alert_checked_at = checked_at
    return True


def _maybe_emit_degraded_alert(result: Any, rt_dir: Path) -> None:
    """Emit Chinese stderr alert when a daemon RPC result indicates degraded update state."""
    if not isinstance(result, dict):
        return
    upd_status = result.get("updateStatus")
    if isinstance(upd_status, dict) and str(upd_status.get("state") or "").startswith("degraded_"):
        emit_stderr_update_alert(upd_status)
        return
    if str(result.get("state") or "").startswith("degraded_"):
        emit_stderr_update_alert(result)
        return
    warn = result.get("updateWarning")
    if isinstance(warn, str) and warn.strip():
        disk_state = load_update_state(rt_dir)
        if isinstance(disk_state, dict) and str(disk_state.get("state") or "").startswith("degraded_"):
            emit_stderr_update_alert(disk_state)
        else:
            emit_stderr_update_alert({"state": "degraded_network_error", "userHint": warn})


def _call_compat(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call `fn` forwarding only keyword arguments accepted by its signature (for parallel module evolution)."""
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)

    accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if accepts_varkw:
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(*args, **filtered)


class DaemonManager:
    """Manages named headless PoB BridgeWorkers with per-slot locks, idle recycling, and hot-reload barriers."""

    def __init__(
        self,
        pob_dir: Path | None = None,
        token: str = "",
        *,
        runtime_dir: Path | None = None,
        offline: bool | None = None,
        proxy: str | None = None,
        mirror: str | None = None,
        skip_update_check: bool = False,
        disable_global_idle: bool = False,
    ) -> None:
        self.runtime_dir = _get_runtime_dir(runtime_dir)
        self._explicit_pob_dir = Path(pob_dir) if pob_dir is not None else None
        self.pob_dir = self._explicit_pob_dir or resolve_engine_root(runtime_dir=self.runtime_dir)
        self.token = token
        self.offline = _is_offline_env() if offline is None else bool(offline)
        self.proxy = proxy
        self.mirror = mirror
        self.skip_update_check = skip_update_check
        self.disable_global_idle = disable_global_idle
        self.update_status: dict[str, Any] = load_update_state(self.runtime_dir)
        self.started_at = time.time()
        self.last_active_ts = time.time()

        # Coordination lock + RW barrier:
        # - Normal slot operations take the reader count and run concurrently under per-slot locks.
        # - Hot-reload and shutdown_all take the exclusive barrier so DLLs are never touched mid-update.
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._update_check_lock = threading.RLock()
        self._reloading = False
        self._reload_owner: int | None = None
        self._reload_depth = 0
        self._active_ops = 0
        self._reader_depths: dict[int, int] = {}

        self._workers: dict[str, BridgeWorker] = {
            "main": self._new_worker("main"),
        }
        self._slot_locks: dict[str, threading.RLock] = {
            "main": threading.RLock(),
        }
        self.server: ThreadingHTTPServer | None = None
        self._stopping = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._shutdown_done = threading.Event()
        self._boot_update_thread: threading.Thread | None = None

    def _new_worker(self, slot_name: str) -> BridgeWorker:
        return _call_compat(
            BridgeWorker,
            slot_name=slot_name,
            pob_dir=self.pob_dir,
            runtime_dir=self.runtime_dir,
        )

    @property
    def main_worker(self) -> BridgeWorker:
        with self._lock:
            return self._workers["main"]

    @main_worker.setter
    def main_worker(self, worker: BridgeWorker) -> None:
        with self._lock:
            self._workers["main"] = worker

    @property
    def ref_worker(self) -> BridgeWorker | None:
        with self._lock:
            return self._workers.get("ref")

    @ref_worker.setter
    def ref_worker(self, worker: BridgeWorker | None) -> None:
        with self._lock:
            if worker is None:
                self._workers.pop("ref", None)
            else:
                self._workers["ref"] = worker

    def _get_slot_lock(self, slot: str) -> threading.RLock:
        with self._lock:
            lock = self._slot_locks.get(slot)
            if lock is None:
                lock = threading.RLock()
                self._slot_locks[slot] = lock
            return lock

    @contextlib.contextmanager
    def _reader_barrier(self) -> Iterator[None]:
        cur_tid = threading.get_ident()
        with self._cond:
            if self._reload_owner != cur_tid:
                depth = self._reader_depths.get(cur_tid, 0)
                if depth == 0:
                    while self._reloading and self._reload_owner != cur_tid:
                        self._cond.wait()
                    self._active_ops += 1
                self._reader_depths[cur_tid] = depth + 1
        try:
            yield
        finally:
            with self._cond:
                if self._reload_owner != cur_tid:
                    new_depth = self._reader_depths.get(cur_tid, 1) - 1
                    if new_depth <= 0:
                        self._reader_depths.pop(cur_tid, None)
                        self._active_ops -= 1
                        if self._active_ops == 0:
                            self._cond.notify_all()
                    else:
                        self._reader_depths[cur_tid] = new_depth

    @contextlib.contextmanager
    def _exclusive_barrier(self) -> Iterator[None]:
        cur_tid = threading.get_ident()
        with self._cond:
            while self._reloading and self._reload_owner != cur_tid:
                self._cond.wait()
            if self._reload_owner == cur_tid:
                self._reload_depth += 1
            else:
                self._reloading = True
                self._reload_owner = cur_tid
                self._reload_depth = 1
                while self._active_ops > 0:
                    self._cond.wait()
        try:
            yield
        finally:
            with self._cond:
                self._reload_depth -= 1
                if self._reload_depth == 0:
                    self._reloading = False
                    self._reload_owner = None
                    self._cond.notify_all()

    def _run_background_boot_update_check(self) -> None:
        """Non-blocking startup check_only update probe so cold start serves immediately, hot-reloading if needed."""
        try:
            probe_state: dict[str, Any] | None = None
            with self._update_check_lock:
                if self._stopping:
                    return
                probe_state = check_and_apply_updates(
                    engine_dir=self._explicit_pob_dir,
                    runtime_dir=self.runtime_dir,
                    offline=False,
                    proxy=self.proxy,
                    mirror=self.mirror,
                    check_only=True,
                )
                with self._lock:
                    if not self._stopping:
                        self.update_status = probe_state
            if probe_state and probe_state.get("updateAvailable") and not self._stopping:
                self._hot_reload_update(
                    proxy=self.proxy,
                    mirror=self.mirror,
                    force=False,
                    offline=False,
                )
        except Exception:
            pass

    def start(self) -> None:
        """Start the `main` worker immediately when an engine exists; run boot update check asynchronously."""
        launch_bg_check = False
        with self._exclusive_barrier():
            checked_at = float(self.update_status.get("checkedAt") or 0.0)
            engine_valid = is_valid_engine_dir(self.pob_dir)
            fresh_ok = (
                self.update_status.get("state") in OK_STATES
                and not self.update_status.get("updateAvailable")
                and (time.time() - checked_at) < UPDATE_RECHECK_INTERVAL_SEC
                and engine_valid
                and str(self.update_status.get("engineDir") or "") == str(self.pob_dir)
            )

            if not self.skip_update_check and not fresh_ok:
                if not engine_valid or self.offline:
                    # Synchronous bootstrap when engine is missing, or instant local metadata check when offline
                    self.update_status = check_and_apply_updates(
                        engine_dir=self._explicit_pob_dir,
                        runtime_dir=self.runtime_dir,
                        offline=self.offline,
                        proxy=self.proxy,
                        mirror=self.mirror,
                    )
                else:
                    # Engine already installed & online: defer check_only probe to background thread
                    launch_bg_check = True

            if self._explicit_pob_dir is None:
                self.pob_dir = resolve_engine_root(runtime_dir=self.runtime_dir)

            if self.update_status.get("state") == STATE_BOOTSTRAP_MISSING and not is_valid_engine_dir(self.pob_dir):
                hint = get_update_warning(self.update_status) or (
                    f"No usable PoB engine found at {self.pob_dir}. "
                    "Configure HTTPS_PROXY / POB_PROXY and run `pob bootstrap`, or use `pob bootstrap --from-dir <path>`."
                )
                raise BootstrapMissingError(hint, update_status=self.update_status)

            if launch_bg_check:
                # Start the check_only probe BEFORE main_w.start() so the ~1.5-2s network/stat
                # check overlaps the ~2.7s PoB engine boot in parallel (0ms added to cold start,
                # and fast-failing degraded states are ready before the first status call!).
                t = threading.Thread(
                    target=self._run_background_boot_update_check,
                    name="pob-boot-update-check",
                    daemon=True,
                )
                self._boot_update_thread = t
                t.start()

            main_w = self._workers["main"]
            if main_w.pob_root != self.pob_dir:
                main_w = self._new_worker("main")
                self._workers["main"] = main_w
            main_w.start()

    def _current_update_warning(self) -> str | None:
        with self._lock:
            return get_update_warning(self.update_status)

    def _attach_warning_if_degraded(self, payload: dict[str, Any]) -> dict[str, Any]:
        warn = self._current_update_warning()
        if warn:
            payload["updateWarning"] = warn
        return payload

    def _update_daemon_state_pob_dir(self) -> None:
        state_file = _daemon_state_file(self.runtime_dir)
        if not state_file.is_file():
            return
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("pid") == os.getpid():
                data["pob_dir"] = str(self.pob_dir)
                tmp = state_file.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp, state_file)
        except Exception:
            pass

    def _hot_reload_update(
        self,
        *,
        proxy: str | None = None,
        mirror: str | None = None,
        force: bool = False,
        offline: bool = False,
    ) -> dict[str, Any]:
        """Atomically snapshot all slots, stop workers to unlock Windows DLLs, update engine, and restore slots."""
        with self._update_check_lock, self._exclusive_barrier():
            if self._stopping:
                return self.update_status
            saved_slots: dict[str, dict[str, Any]] = {}
            for slot_name, worker in list(self._workers.items()):
                was_alive = worker.is_alive()
                code = worker.last_good_code
                if was_alive:
                    try:
                        exported = worker.update_checkpoint()
                        if exported:
                            code = exported
                    except Exception:
                        pass
                saved_slots[slot_name] = {
                    "was_alive": was_alive,
                    "code": code,
                    "undo": list(worker.undo_stack),
                    "redo": list(worker.redo_stack),
                    "source_label": worker.source_label,
                }
                try:
                    worker.stop()
                except Exception:
                    pass

            self.update_status = check_and_apply_updates(
                engine_dir=self._explicit_pob_dir,
                runtime_dir=self.runtime_dir,
                proxy=proxy if proxy is not None else self.proxy,
                mirror=mirror if mirror is not None else self.mirror,
                offline=offline,
                check_only=False,
                force=force,
            )

            if self._explicit_pob_dir is None:
                self.pob_dir = resolve_engine_root(runtime_dir=self.runtime_dir)
            self._update_daemon_state_pob_dir()

            engine_valid = is_valid_engine_dir(self.pob_dir)
            new_workers: dict[str, BridgeWorker] = {}
            for slot_name, snap in saved_slots.items():
                if slot_name != "main" and not (snap["was_alive"] or snap["code"]):
                    continue
                w = self._new_worker(slot_name)
                w.last_good_code = snap["code"]
                w.undo_stack = snap["undo"]
                w.redo_stack = snap["redo"]
                w.source_label = snap["source_label"]
                if engine_valid and (slot_name == "main" or snap["was_alive"]):
                    w.start()
                new_workers[slot_name] = w

            if "main" not in new_workers:
                main_w = self._new_worker("main")
                if engine_valid:
                    main_w.start()
                new_workers["main"] = main_w

            self._workers = new_workers
            return self.update_status

    def _maybe_periodic_update_check(self) -> None:
        """If daemon has been alive > 30m since last update check, check remote and hot-reload if updated."""
        if self.offline or self.skip_update_check or self._stopping:
            return
        if self._boot_update_thread is not None and self._boot_update_thread.is_alive():
            return
        with self._lock:
            checked_at = float(self.update_status.get("checkedAt") or 0.0)
        if (time.time() - checked_at) <= UPDATE_RECHECK_INTERVAL_SEC:
            return

        with self._update_check_lock:
            with self._lock:
                checked_at = float(self.update_status.get("checkedAt") or 0.0)
            if (time.time() - checked_at) <= UPDATE_RECHECK_INTERVAL_SEC:
                return
            probe_state = check_and_apply_updates(
                engine_dir=self._explicit_pob_dir,
                runtime_dir=self.runtime_dir,
                proxy=self.proxy,
                mirror=self.mirror,
                offline=False,
                check_only=True,
            )
            with self._lock:
                self.update_status = probe_state
            if probe_state.get("updateAvailable"):
                self._hot_reload_update(proxy=self.proxy, mirror=self.mirror, force=False, offline=False)

    def _get_or_create_worker_unlocked(self, slot: str) -> BridgeWorker:
        worker = self._workers.get(slot)
        if worker is None:
            worker = self._new_worker(slot)
            self._workers[slot] = worker
        if slot not in self._slot_locks:
            self._slot_locks[slot] = threading.RLock()
        return worker

    def _get_worker_in_barrier(self, slot: str) -> BridgeWorker:
        with self._lock:
            self.last_active_ts = time.time()
            worker = self._get_or_create_worker_unlocked(slot)
            slot_lock = self._slot_locks[slot]
        with slot_lock:
            worker.ensure_alive()
            return worker

    def get_worker(self, target: str = "main") -> BridgeWorker:
        slot = normalize_slot_name(target)
        with self._reader_barrier():
            return self._get_worker_in_barrier(slot)

    def check_idle(self) -> bool:
        """Recycle non-main workers idle > 5m; return True if global daemon should shut down (> 30m idle)."""
        now = time.time()
        with self._reader_barrier():
            with self._lock:
                candidates = [(name, w) for name, w in self._workers.items() if name != "main"]

            for slot_name, worker in candidates:
                if not worker.is_alive():
                    continue
                if (now - worker.last_active_ts) <= REF_IDLE_TIMEOUT_SEC:
                    continue
                slot_lock = self._get_slot_lock(slot_name)
                if not slot_lock.acquire(blocking=False):
                    continue
                try:
                    with self._lock:
                        still_same = self._workers.get(slot_name) is worker
                    if still_same and worker.is_alive() and (time.time() - worker.last_active_ts) > REF_IDLE_TIMEOUT_SEC:
                        try:
                            worker.stop()
                        except Exception:
                            pass
                        with self._lock:
                            if self._workers.get(slot_name) is worker:
                                self._workers.pop(slot_name, None)
                finally:
                    slot_lock.release()

        if self.disable_global_idle:
            return False
        return (now - self.last_active_ts) > GLOBAL_IDLE_TIMEOUT_SEC

    def shutdown_all(self) -> None:
        with self._shutdown_lock:
            already_started = self._shutdown_started
            if not already_started:
                self._shutdown_started = True
                self._stopping = True
        if already_started:
            self._shutdown_done.wait(timeout=15.0)
            return

        try:
            # Abort live subprocesses immediately before waiting on _exclusive_barrier(),
            # so any in-flight long RPC unblocks promptly instead of stalling teardown.
            with self._lock:
                workers_to_abort = list(self._workers.values())
            for worker in workers_to_abort:
                try:
                    abort_fn = getattr(worker, "abort", None)
                    if callable(abort_fn):
                        abort_fn()
                    else:
                        worker.stop()
                except Exception:
                    pass

            with self._exclusive_barrier():
                for worker in list(self._workers.values()):
                    try:
                        worker.stop()
                    except Exception:
                        pass
                self._workers = {
                    "main": self._workers.get("main") or self._new_worker("main")
                }
            state_file = _daemon_state_file(self.runtime_dir)
            try:
                if state_file.exists():
                    state_file.unlink()
            except Exception:
                pass
            if self.server is not None:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
        finally:
            self._shutdown_done.set()

    def _slot_status_entry(self, slot_name: str, worker: BridgeWorker | None) -> dict[str, Any]:
        alive = worker is not None and worker.is_alive()
        info = None
        busy = False
        if alive and worker is not None:
            slot_lock = self._get_slot_lock(slot_name)
            if slot_lock.acquire(blocking=False):
                try:
                    with self._lock:
                        still_active = self._workers.get(slot_name) is worker and worker.is_alive()
                    if still_active:
                        try:
                            info = worker.call("get_build_info", {})
                        except Exception:
                            info = None
                    else:
                        alive = False
                finally:
                    slot_lock.release()
            else:
                busy = True
        entry: dict[str, Any] = {
            "alive": alive,
            "buildInfo": info,
            "hasLoadedBuild": bool(worker and worker.last_good_code),
        }
        if busy:
            entry["busy"] = True
        if worker is not None:
            entry["undoDepth"] = len(worker.undo_stack)
            entry["redoDepth"] = len(worker.redo_stack)
            if slot_name == "main":
                entry["hello"] = worker.hello_info
            if worker.source_label:
                entry["sourceLabel"] = worker.source_label
        return entry

    def dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.last_active_ts = time.time()

        if action == "stop":
            self.shutdown_all()
            return {"stopped": True}

        if action == "game_data":
            from . import game_data

            with self._lock:
                pob_dir = self.pob_dir
                rt_dir = self.runtime_dir
            if params.get("rebuild"):
                game_data.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=rt_dir, force_rebuild=True)
            queries = params.get("queries")
            limit_val = int(params["limit"]) if params.get("limit") is not None else 25
            if isinstance(queries, list) and len(queries) > 1 and hasattr(game_data, "query_multi"):
                return _call_compat(
                    game_data.query_multi,
                    db=None,
                    kind=str(params.get("kind") or ""),
                    queries=[str(q) for q in queries],
                    pob_dir=pob_dir,
                    runtime_dir=rt_dir,
                    slot=params.get("slot"),
                    ilvl=int(params["ilvl"]) if params.get("ilvl") is not None else None,
                    limit=limit_val,
                )
            single_q = str(queries[0]) if isinstance(queries, list) and queries else str(params.get("query") or "")
            return game_data.query_local_data(
                kind=str(params.get("kind") or ""),
                query=single_q,
                pob_dir=pob_dir,
                runtime_dir=rt_dir,
                slot=params.get("slot"),
                ilvl=int(params["ilvl"]) if params.get("ilvl") is not None else None,
                limit=limit_val,
            )

        if action == "update":
            check_only = bool(params.get("check_only", params.get("checkOnly", False)))
            force = bool(params.get("force", False))
            proxy = params.get("proxy")
            mirror = params.get("mirror")
            offline = bool(params.get("offline", False))
            if check_only or offline:
                with self._update_check_lock:
                    new_state = check_and_apply_updates(
                        engine_dir=self._explicit_pob_dir,
                        runtime_dir=self.runtime_dir,
                        proxy=proxy if proxy is not None else self.proxy,
                        mirror=mirror if mirror is not None else self.mirror,
                        offline=offline,
                        check_only=check_only,
                        force=force,
                    )
                    with self._lock:
                        self.update_status = new_state
                return self._attach_warning_if_degraded(dict(new_state))
            updated = self._hot_reload_update(
                proxy=proxy,
                mirror=mirror,
                force=force,
                offline=False,
            )
            return self._attach_warning_if_degraded(dict(updated))

        if action in ("status", "load") and not params.get("skip_periodic_check"):
            self._maybe_periodic_update_check()

        if action == "status":
            with self._reader_barrier():
                with self._lock:
                    workers_snap = dict(self._workers)
                    pob_dir_str = str(self.pob_dir)
                    upd_copy = dict(self.update_status)
                slots_status: dict[str, dict[str, Any]] = {}
                for sname, w in workers_snap.items():
                    slots_status[sname] = self._slot_status_entry(sname, w)
                main_entry = slots_status.get("main") or self._slot_status_entry("main", None)
                ref_entry = slots_status.get("ref") or {
                    "alive": False,
                    "buildInfo": None,
                    "hasLoadedBuild": False,
                }
                res: dict[str, Any] = {
                    "pid": os.getpid(),
                    "uptimeSec": round(time.time() - self.started_at, 1),
                    "idleSec": round(time.time() - self.last_active_ts, 1),
                    "engineDir": pob_dir_str,
                    "updateStatus": upd_copy,
                    "main": main_entry,
                    "ref": ref_entry,
                    "slots": slots_status,
                }
                return self._attach_warning_if_degraded(res)

        if action in ("compare", "borrow"):
            main_slot = normalize_slot_name(params.get("main_target") or params.get("target") or "main")
            ref_slot = normalize_slot_name(params.get("ref_target") or "ref")
            with self._reader_barrier():
                lock_order = sorted({main_slot, ref_slot})
                locks = [self._get_slot_lock(s) for s in lock_order]
                with contextlib.ExitStack() as stack:
                    for lk in locks:
                        stack.enter_context(lk)
                    mw = self._get_worker_in_barrier(main_slot)
                    rw = self._get_worker_in_barrier(ref_slot)
                    return self._dispatch_dual_slot(action, params, mw, rw)

        target = normalize_slot_name(params.get("target"))
        with self._reader_barrier():
            slot_lock = self._get_slot_lock(target)
            with slot_lock:
                worker = self._get_worker_in_barrier(target)
                return self._dispatch_slot(action, params, worker)

    def _dispatch_dual_slot(
        self,
        action: str,
        params: dict[str, Any],
        mw: BridgeWorker,
        rw: BridgeWorker,
    ) -> dict[str, Any]:
        from .comparator import borrow_from_ref, compare_builds

        if action == "compare":
            return compare_builds(
                mw,
                rw,
                ref_source=params.get("ref_source"),
                ref_xml_code=params.get("ref_xml_code"),
            )

        if action == "borrow":
            if not rw.last_good_code:
                ref_slot = getattr(rw, "slot_name", None) or "ref"
                raise RuntimeError(
                    f"No reference build loaded in '{ref_slot}' slot. "
                    f"Run `pob load <source> --target {ref_slot}` or `pob compare <source>` first."
                )
            return borrow_from_ref(
                mw,
                rw,
                item_slots=params.get("item_slots"),
                skill_group=params.get("skill_group"),
                apply=bool(params.get("apply", False)),
            )

        raise ValueError(f"Unknown dual-slot daemon action '{action}'.")

    def _dispatch_slot(
        self,
        action: str,
        params: dict[str, Any],
        w: BridgeWorker,
    ) -> dict[str, Any]:
        """Execute a single-slot RPC action under its per-slot lock and reader barrier."""
        if action == "load":
            from .importers import load_build_into_worker

            try:
                load_res = load_build_into_worker(
                    w,
                    source=params.get("source"),
                    xml_code=params.get("xml_code"),
                )
            except Exception as exc:
                warn = self._current_update_warning()
                if warn:
                    guidance = (
                        f"\n[Engine Update Warning] Local PoB engine could not update due to GitHub network failure: {warn}\n"
                        "Guidance for AI Agent: Tell the user to configure a proxy (HTTPS_PROXY / POB_PROXY) and run "
                        "`pob update --proxy http://127.0.0.1:<port>` (or `pob bootstrap --from-dir <local_dir>`). "
                        "Do NOT assume the build code is corrupted, and NEVER manually parse Build.xml to compute stats (R5)."
                    )
                    if isinstance(exc, BridgeError):
                        raise BridgeError(f"{exc}{guidance}") from exc
                    raise RuntimeError(f"{exc}{guidance}") from exc
                raise
            return self._attach_warning_if_degraded(load_res)

        if action == "export":
            from .importers import share_code_to_xml

            with w.lock:
                code_res = w.call("export_code", {})
                code_str = code_res.get("code", "")
                xml_str = None
                if params.get("include_xml"):
                    xml_res = w.call("save_xml", {})
                    xml_str = xml_res.get("xml") or share_code_to_xml(code_str)
                return {"code": code_str, "xml": xml_str}

        if action == "undo":
            from .snapshot import undo_build

            return undo_build(w)

        if action == "redo":
            from .snapshot import redo_build

            return redo_build(w)

        if action == "summary":
            from . import analyzer

            fn = getattr(analyzer, "extract_summary", None) or getattr(analyzer, "get_summary")
            return self._attach_warning_if_degraded(fn(w))

        if action == "audit":
            from .analyzer import audit_build

            return audit_build(w)

        if action == "items":
            from .analyzer import inspect_items

            items_res = inspect_items(
                w,
                slot_name=params.get("slot"),
                raw=bool(params.get("raw", False)),
                all_raw=bool(params.get("allRaw", params.get("all_raw", False))),
            )
            return self._attach_warning_if_degraded(items_res)

        if action == "skills":
            from .analyzer import inspect_skills

            return inspect_skills(
                w,
                group_selector=params.get("group"),
            )

        if action == "tree":
            from .analyzer import inspect_tree

            return _call_compat(
                inspect_tree,
                w,
                search=params.get("search"),
                queries=params.get("queries"),
                node_types=params.get("node_types") or params.get("nodeTypes") or params.get("types"),
                allocated=params.get("allocated"),
                max_cost=(
                    int(params["max_cost"])
                    if params.get("max_cost") is not None
                    else (int(params["maxCost"]) if params.get("maxCost") is not None else None)
                ),
                show_path=bool(params.get("show_path", params.get("showPath", False))),
                masteries=bool(params.get("masteries", False)),
                socket_radius=(
                    params.get("socket_radius")
                    if params.get("socket_radius") is not None
                    else params.get("socketRadius")
                ),
            )

        if action == "config_list":
            from .analyzer import inspect_config

            show_all = bool(
                params.get(
                    "includeDefaults",
                    params.get("include_defaults", params.get("show_all", False)),
                )
            )
            return _call_compat(
                inspect_config,
                w,
                include_defaults=show_all,
                show_all=show_all,
                search=params.get("search"),
                with_options=bool(params.get("with_options", params.get("withOptions", False))),
            )

        if action == "calc":
            from . import analyzer

            fn = getattr(analyzer, "search_calcs_breakdown", None) or getattr(analyzer, "inspect_calcs")
            keywords = params.get("keywords")
            keyword = params.get("keyword")
            if keyword is None and isinstance(keywords, list) and keywords:
                keyword = str(keywords[0])
            limit_val = int(params["limit"]) if params.get("limit") is not None else 20
            return _call_compat(
                fn,
                w,
                keyword=str(keyword or ""),
                keywords=keywords,
                limit=limit_val,
                no_breakdown=bool(params.get("no_breakdown", params.get("noBreakdown", False))),
            )

        if action == "matrix":
            from .comparator import run_matrix

            return _call_compat(
                run_matrix,
                w,
                scenarios=params.get("scenarios"),
                full=bool(params.get("full", False)),
            )

        if action == "rank_nodes":
            from .comparator import rank_nodes

            return rank_nodes(
                w,
                stat=str(params.get("stat") or "CombinedDPS"),
                max_dist=int(params["max_dist"]) if params.get("max_dist") is not None else 5,
                top=int(params["top"]) if params.get("top") is not None else 15,
            )

        if action == "rank_gems":
            from .comparator import rank_gems

            return rank_gems(
                w,
                group_selector=str(params.get("group") or "main"),
                slot_idx=params.get("slot_idx"),
                add_link=bool(params.get("add_link", False)),
                field=str(params.get("field") or "CombinedDPS"),
                top=int(params["top"]) if params.get("top") is not None else 15,
            )

        # Mutator actions
        from . import mutators

        if action == "switch_main":
            return mutators.switch_main(
                w,
                spec_index=params.get("spec_index"),
                skill_group=params.get("skill_group"),
                main_skill_index=params.get("main_skill_index"),
                skill_part_index=params.get("skill_part_index"),
                item_set_id=params.get("item_set_id"),
                skill_set_id=params.get("skill_set_id"),
                config_set_id=params.get("config_set_id"),
                loadout_index=params.get("loadout_index"),
                apply=bool(params.get("apply", True)),
            )

        if action == "new_build":
            return _call_compat(
                mutators.create_new_build,
                w,
                class_name=params.get("class_name") or params.get("className"),
                ascendancy=(
                    params.get("ascendancy")
                    or params.get("ascendancy_name")
                    or params.get("ascendancyName")
                ),
                secondary_ascendancy=(
                    params.get("secondary_ascendancy")
                    or params.get("secondary_ascendancy_name")
                    or params.get("secondaryAscendancy")
                ),
                level=int(params["level"]) if params.get("level") is not None else None,
                apply=bool(params.get("apply", True)),
            )

        if action == "item_try":
            return mutators.try_item(
                w,
                raw_text=str(params.get("raw") or ""),
                slot_name=params.get("slot"),
                tooltip_only=bool(params.get("tooltip_only", False)),
                apply=bool(params.get("apply", False)),
            )

        if action == "item_edit":
            raw_reps = params.get("replace_mods") or params.get("replaceMods") or []
            rep_tuples = [(str(p[0]), str(p[1])) for p in raw_reps if isinstance(p, (list, tuple)) and len(p) == 2]
            return mutators.edit_item_mods(
                w,
                slot_name=str(params.get("slot") or params.get("slotName") or ""),
                replace_mods=rep_tuples or None,
                add_mods=params.get("add_mods") or params.get("addMods"),
                remove_mods=params.get("remove_mods") or params.get("removeMods"),
                apply=bool(params.get("apply", False)),
            )

        if action == "item_unequip":
            slot_val = str(params.get("slot") or params.get("slotName") or "")
            return _call_compat(
                mutators.unequip_item,
                w,
                slot=slot_val,
                slot_name=slot_val,
                apply=bool(params.get("apply", False)),
            )

        if action == "item_db":
            idx_val = params.get("index") if params.get("index") is not None else params.get("item_index")
            fn = getattr(mutators, "search_and_try_item_db", None) or getattr(mutators, "query_item_db")
            return _call_compat(
                fn,
                w,
                query=str(params.get("query") or ""),
                db_type=str(params.get("db_type") or params.get("dbType") or "UNIQUE"),
                slot_name=params.get("slot") or params.get("slotName"),
                item_index=int(idx_val) if idx_val is not None else None,
                equip=bool(params.get("equip", False)),
                apply=bool(params.get("apply", False)),
                limit=int(params["limit"]) if params.get("limit") is not None else 15,
                with_tooltips=bool(params.get("with_tooltips", params.get("withTooltips", False))),
                with_raw=bool(params.get("with_raw", params.get("withRaw", False))),
            )

        if action == "item_flask":
            return mutators.set_flask_active(
                w,
                flask_index=params["flask_index"],
                active=bool(params["active"]),
                apply=bool(params.get("apply", False)),
            )

        if action == "item_anoint":
            return mutators.anoint_item(
                w,
                slot_name=str(params.get("slot") or "Amulet"),
                notable_query=str(params.get("notable") or ""),
                anoint_index=int(params["index"]) if params.get("index") is not None else 1,
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_set":
            return mutators.modify_skill(
                w,
                group_selector=str(params.get("group") or "main"),
                gem_selector=params.get("gem"),
                new_gem=params.get("new_gem"),
                level=params.get("level"),
                quality=params.get("quality"),
                enabled=params.get("enabled"),
                count=params.get("count"),
                include_in_full_dps=params.get("include_in_full_dps"),
                group_enabled=params.get("group_enabled"),
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_add":
            return mutators.add_skill_gem(
                w,
                group_selector=str(params.get("group") or "main"),
                gem_name=str(params.get("gem") or ""),
                level=int(params["level"]) if params.get("level") is not None else 20,
                quality=int(params["quality"]) if params.get("quality") is not None else 20,
                enabled=bool(params.get("enabled", True)),
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_rm":
            return mutators.remove_skill_gem(
                w,
                group_selector=str(params.get("group") or "main"),
                gem_selector=params["gem"],
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_new_group":
            return _call_compat(
                mutators.add_skill_group,
                w,
                gems=params.get("gems"),
                label=params.get("label"),
                slot=params.get("slot"),
                enabled=bool(params.get("enabled", True)),
                include_in_full_dps=(
                    params.get("include_in_full_dps")
                    if params.get("include_in_full_dps") is not None
                    else params.get("includeInFullDps")
                ),
                make_main=bool(params.get("make_main", params.get("makeMain", False))),
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_replace_group":
            grp_val = params.get("group") if params.get("group") is not None else "main"
            return _call_compat(
                mutators.replace_skill_group,
                w,
                group=grp_val,
                group_selector=str(grp_val),
                gems=params.get("gems") or [],
                label=params.get("label"),
                slot=params.get("slot"),
                enabled=params.get("enabled"),
                include_in_full_dps=(
                    params.get("include_in_full_dps")
                    if params.get("include_in_full_dps") is not None
                    else params.get("includeInFullDps")
                ),
                apply=bool(params.get("apply", False)),
            )

        if action == "tree_change":
            return _call_compat(
                mutators.change_tree,
                w,
                allocate=params.get("allocate"),
                deallocate=params.get("deallocate"),
                apply=bool(params.get("apply", False)),
                allow_cascade=bool(params.get("allow_cascade", params.get("allowCascade", True))),
            )

        if action == "tree_set_class":
            return mutators.set_class_ascendancy(
                w,
                class_name=params.get("class_name") or params.get("className"),
                ascendancy_name=params.get("ascendancy_name") or params.get("ascendancyName"),
                secondary_ascendancy_name=(
                    params.get("secondary_ascendancy_name") or params.get("secondaryAscendancyName")
                ),
                apply=bool(params.get("apply", False)),
            )

        if action == "config_set":
            append_val = (
                params.get("appendCustomMods")
                if params.get("appendCustomMods") is not None
                else params.get("append_custom_mods")
            )
            return mutators.modify_config(
                w,
                assignments=params.get("assignments") or [],
                custom_mods=params.get("custom_mods"),
                append_custom_mods=append_val,
                apply=bool(params.get("apply", False)),
            )

        if action == "batch":
            return _call_compat(
                mutators.run_batch,
                w,
                ops=params.get("ops") or [],
                apply=bool(params.get("apply", False)),
                no_undo=bool(params.get("no_undo", params.get("noUndo", False))),
                return_sections=params.get("return_sections") or params.get("returnSections"),
            )

        if action == "sweep":
            return _call_compat(
                mutators.run_sweep,
                w,
                variants=params.get("variants") or [],
                base_ops=params.get("base_ops") or params.get("baseOps"),
                calc_keywords=params.get("calc_keywords") or params.get("calcKeywords"),
                scenario=params.get("scenario"),
            )

        if action == "trade_eval_items":
            from . import trade_eval

            return trade_eval.eval_trade_items(
                w,
                slot_name=params.get("slot") or params.get("slotName"),
                items=list(params.get("items") or []),
            )

        raise ValueError(f"Unknown daemon action '{action}'.")


def make_handler(manager: DaemonManager) -> type["BaseHTTPRequestHandler"]:
    import traceback
    from http.server import BaseHTTPRequestHandler

    class RpcHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress noisy stdout access logs
            pass

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def do_POST(self) -> None:
            token = self.headers.get("X-PoB-Token", "")
            if not secrets.compare_digest(token, manager.token):
                self._send_json(403, {"ok": False, "error": "Forbidden: invalid or missing X-PoB-Token"})
                return

            if self.path != "/rpc":
                self._send_json(404, {"ok": False, "error": "Not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError("negative Content-Length")
            except ValueError:
                self._send_json(400, {"ok": False, "error": "Invalid Content-Length header"})
                return
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                req = json.loads(raw_body) if raw_body else {}
                action = req.get("action")
                params = req.get("params") or {}
                result = manager.dispatch(action, params)
                self._send_json(200, {"ok": True, "result": result})
            except BridgeError as exc:
                self._send_json(400, {"ok": False, "error": str(exc), "type": "BridgeError"})
            except Exception as exc:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                        "type": exc.__class__.__name__,
                        "trace": traceback.format_exc(limit=4),
                    },
                )

    return RpcHandler


def _serve_daemon(
    pob_dir: Path | None = None,
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
    disable_global_idle: bool = False,
    announce_foreground: bool = False,
) -> None:
    """Start the local daemon HTTP server, write `<runtime_dir>/daemon.json`, and serve until stopped."""
    rt_dir = _get_runtime_dir(runtime_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    boot_err_file = _daemon_boot_error_file(rt_dir)
    state_file = _daemon_state_file(rt_dir)

    try:
        if boot_err_file.exists():
            boot_err_file.unlink()
    except OSError:
        pass

    token = secrets.token_hex(32)
    manager = DaemonManager(
        pob_dir=pob_dir,
        token=token,
        runtime_dir=rt_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=disable_global_idle,
    )
    try:
        manager.start()
    except Exception as exc:
        err_payload = {
            "error": str(exc),
            "type": exc.__class__.__name__,
            "state": (
                exc.update_status.get("state")
                if isinstance(exc, BootstrapMissingError)
                else "boot_error"
            ),
            "updateStatus": exc.update_status if isinstance(exc, BootstrapMissingError) else None,
            "ts": time.time(),
        }
        try:
            tmp_err = boot_err_file.with_suffix(f".{os.getpid()}.tmp")
            tmp_err.write_text(json.dumps(err_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_err, boot_err_file)
        except OSError:
            pass
        raise

    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(manager))
    manager.server = server
    actual_port = server.server_address[1]

    state_payload = {
        "pid": os.getpid(),
        "port": actual_port,
        "token": token,
        "started_at": manager.started_at,
        "pob_dir": str(manager.pob_dir),
    }
    tmp_state_file = state_file.with_suffix(f".{os.getpid()}.tmp")
    tmp_state_file.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")
    os.replace(tmp_state_file, state_file)

    if announce_foreground:
        sys.stderr.write(
            f"pob daemon listening on 127.0.0.1:{actual_port} (pid={os.getpid()}, runtime={rt_dir})\n"
        )
        sys.stderr.flush()

    def _watchdog() -> None:
        while not manager._stopping:
            time.sleep(15.0)
            if manager.check_idle():
                manager.shutdown_all()
                break

    threading.Thread(target=_watchdog, name="pob-daemon-watchdog", daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown_all()


def run_daemon_server(
    pob_dir: Path | None = None,
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
) -> None:
    """Start the background daemon server (auto-exits after 30m global idle)."""
    _serve_daemon(
        pob_dir=pob_dir,
        port=port,
        runtime_dir=runtime_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=False,
        announce_foreground=False,
    )


def run_daemon_forever(
    pob_dir: Path | None = None,
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
) -> None:
    """Run the PoB daemon persistently in the foreground (used by `pob serve`)."""
    _serve_daemon(
        pob_dir=pob_dir,
        port=port,
        runtime_dir=runtime_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=True,
        announce_foreground=True,
    )


# --- Loopback HTTP Client Helpers (stdlib http.client only, zero httpx import) ---


def read_daemon_state(runtime_dir: Path | None = None) -> dict[str, Any] | None:
    state_file = _daemon_state_file(runtime_dir)
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_boot_error(runtime_dir: Path | None = None) -> dict[str, Any] | None:
    err_file = _daemon_boot_error_file(runtime_dir)
    if not err_file.exists():
        return None
    try:
        data = json.loads(err_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _http_post_rpc(
    port: int,
    token: str,
    payload: dict[str, Any],
    *,
    timeout: float = 150.0,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        conn.request(
            "POST",
            "/rpc",
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
                "X-PoB-Token": token,
            },
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        status = resp.status
    finally:
        conn.close()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        if status in (403, 404):
            return status, {"ok": False, "error": f"HTTP {status}: {raw[:200]}"}
        raise RuntimeError(f"Invalid JSON response from PoB daemon (HTTP {status}): {raw[:200]}") from exc
    if not isinstance(data, dict):
        if status in (403, 404):
            return status, {"ok": False, "error": f"HTTP {status}"}
        raise RuntimeError(f"Unexpected response payload from PoB daemon (HTTP {status}).")
    return status, data


def _probe_daemon(state: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any] | None:
    """Return daemon status result if alive and reachable, else None."""
    pid = state.get("pid")
    if isinstance(pid, int) and not _is_pid_alive(pid):
        return None
    try:
        status_code, data = _http_post_rpc(
            int(state["port"]),
            str(state["token"]),
            {"action": "status", "params": {"skip_periodic_check": True}},
            timeout=timeout,
        )
        if status_code == 200 and data.get("ok"):
            return data.get("result")
    except (OSError, http.client.HTTPException, TimeoutError, ValueError, KeyError):
        return None
    return None


def stop_daemon(runtime_dir: Path | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    """Stop the running local PoB daemon cleanly if alive; unlink stale daemon.json if dead."""
    rt_dir = _get_runtime_dir(runtime_dir)
    state_file = _daemon_state_file(rt_dir)
    state = read_daemon_state(rt_dir)
    if state is None:
        return {"stopped": False, "alreadyStopped": True}

    pid = state.get("pid")
    if isinstance(pid, int) and not _is_pid_alive(pid):
        try:
            state_file.unlink()
        except OSError:
            pass
        return {"stopped": False, "staleCleaned": True}

    try:
        status_code, data = _http_post_rpc(
            int(state["port"]),
            str(state["token"]),
            {"action": "stop", "params": {}},
            timeout=timeout,
        )
        if status_code in (403, 404):
            try:
                state_file.unlink()
            except OSError:
                pass
            return {"stopped": False, "staleCleaned": True}
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Daemon stop error")
        return data.get("result") or {"stopped": True}
    except (OSError, http.client.HTTPException, TimeoutError):
        try:
            state_file.unlink()
        except OSError:
            pass
        return {"stopped": False, "staleCleaned": True}


def _spawn_daemon_process(cmd: list[str], rt_dir: Path, log_fh: Any) -> subprocess.Popen[Any]:
    """Spawn background daemon detached from current session/job (best-effort CREATE_BREAKAWAY_FROM_JOB on Windows)."""
    popen_kwargs: dict[str, Any] = {
        "cwd": str(rt_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh,
        "stderr": log_fh,
    }
    if sys.platform == "win32":
        base_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
        try:
            return subprocess.Popen(
                cmd,
                creationflags=base_flags | _CREATE_BREAKAWAY_FROM_JOB,
                **popen_kwargs,
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
                raise
            return subprocess.Popen(
                cmd,
                creationflags=base_flags,
                **popen_kwargs,
            )
    popen_kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **popen_kwargs)


def _ensure_daemon(
    rt_dir: Path,
    *,
    runtime_dir_arg: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Spawn (serialized by lockfile) and wait until a fresh local daemon responds to `status`."""
    lock_file = _daemon_lock_file(rt_dir)
    boot_err_file = _daemon_boot_error_file(rt_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    log_path = rt_dir / "daemon.log"

    acquired_lock = False
    if lock_file.exists():
        try:
            lock_age = time.time() - lock_file.stat().st_mtime
            stale_lock = lock_age > 70.0
            if not stale_lock:
                try:
                    raw_pid = lock_file.read_text(encoding="ascii", errors="ignore").strip()
                    if raw_pid.isdigit() and not _is_pid_alive(int(raw_pid)):
                        stale_lock = True
                except OSError:
                    pass
            if stale_lock:
                lock_file.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        acquired_lock = True
    except OSError:
        acquired_lock = False

    spawned_proc: subprocess.Popen[Any] | None = None
    ping_status_result: dict[str, Any] | None = None
    final_state: dict[str, Any] | None = None

    try:
        if acquired_lock:
            try:
                if boot_err_file.exists():
                    boot_err_file.unlink()
            except OSError:
                pass

            cmd = _build_daemon_spawn_cmd(rt_dir if runtime_dir_arg is not None else None)
            with open(log_path, "a", encoding="utf-8") as log_fh:
                spawned_proc = _spawn_daemon_process(cmd, rt_dir, log_fh)

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            boot_err = _read_boot_error(rt_dir)
            if boot_err is not None:
                err_msg = str(boot_err.get("error") or "PoB daemon failed to start.")
                if boot_err.get("state") == STATE_BOOTSTRAP_MISSING:
                    raise BootstrapMissingError(err_msg, update_status=boot_err.get("updateStatus"))
                raise RuntimeError(err_msg)

            st = read_daemon_state(rt_dir)
            if st is not None:
                probed = _probe_daemon(st, timeout=5.0)
                if probed is not None:
                    ping_status_result = probed
                    final_state = st
                    break

            if spawned_proc is not None and spawned_proc.poll() is not None:
                boot_err = _read_boot_error(rt_dir)
                if boot_err is not None:
                    err_msg = str(boot_err.get("error") or "PoB daemon exited during startup.")
                    if boot_err.get("state") == STATE_BOOTSTRAP_MISSING:
                        raise BootstrapMissingError(err_msg, update_status=boot_err.get("updateStatus"))
                    raise RuntimeError(err_msg)
                raise RuntimeError(
                    f"PoB daemon exited immediately with code {spawned_proc.returncode}. Check {log_path}."
                )

            time.sleep(0.05)
        else:
            tail = ""
            try:
                if log_path.is_file():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if lines:
                        tail = " | log tail: " + " ; ".join(lines[-8:])
            except OSError:
                pass
            raise RuntimeError(f"Timed out waiting for PoB daemon to start. Check {log_path}.{tail}")
    finally:
        if acquired_lock:
            try:
                lock_file.unlink()
            except OSError:
                pass

    if final_state is None:
        final_state = read_daemon_state(rt_dir)
    if not final_state:
        raise RuntimeError("PoB daemon state file disappeared after startup.")
    return final_state, ping_status_result


def call_daemon(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
    runtime_dir: Path | None = None,
    emit_cold_start_alert: bool = True,
) -> dict[str, Any]:
    """Send an authenticated RPC to the local PoB daemon, starting it automatically if not running."""
    rt_dir = _get_runtime_dir(runtime_dir)
    state_file = _daemon_state_file(rt_dir)

    state = read_daemon_state(rt_dir)
    if state is not None:
        pid = state.get("pid")
        if isinstance(pid, int) and not _is_pid_alive(pid):
            # Fast <1ms stale PID detection: unlink immediately without paying 2s loopback TCP timeout
            try:
                state_file.unlink()
            except OSError:
                pass
            state = None
        else:
            try:
                status_code, data = _http_post_rpc(
                    int(state["port"]),
                    str(state["token"]),
                    {"action": action, "params": params or {}},
                    timeout=timeout,
                )
            except TimeoutError:
                if isinstance(pid, int) and not isinstance(pid, bool) and _is_pid_alive(pid):
                    # Daemon process is still alive handling a heavy RPC; never unlink or spawn a duplicate!
                    raise
                try:
                    state_file.unlink()
                except OSError:
                    pass
                state = None
            except (ConnectionError, OSError, http.client.HTTPException):
                # Stale/unreachable daemon.json; clean up and fall through to auto_start
                try:
                    state_file.unlink()
                except OSError:
                    pass
                state = None
            else:
                if status_code in (403, 404):
                    # Port reused by a foreign process or stale token; unlink and fall through to respawn
                    try:
                        state_file.unlink()
                    except OSError:
                        pass
                    state = None
                else:
                    if not data.get("ok"):
                        raise RuntimeError(data.get("error") or "Daemon error")
                    result = data["result"]
                    if emit_cold_start_alert:
                        _maybe_emit_degraded_alert(result, rt_dir)
                    return result

    if not auto_start:
        raise RuntimeError("PoB daemon is not running.")

    st, ping_status_result = _ensure_daemon(rt_dir, runtime_dir_arg=runtime_dir)

    # Emit prominent Chinese stderr warning once on cold start when update state is degraded_*
    if emit_cold_start_alert:
        upd_state = (
            (ping_status_result or {}).get("updateStatus")
            or load_update_state(rt_dir)
        )
        if str((upd_state or {}).get("state") or "").startswith("degraded_"):
            emit_stderr_update_alert(upd_state)

    if action == "status" and ping_status_result is not None:
        return ping_status_result

    _, data = _http_post_rpc(
        int(st["port"]),
        str(st["token"]),
        {"action": action, "params": params or {}},
        timeout=timeout,
    )
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "Daemon error")
    result = data["result"]
    if emit_cold_start_alert:
        _maybe_emit_degraded_alert(result, rt_dir)
    return result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Headless PoB local HTTP daemon")
    parser.add_argument("--pob-dir", type=Path, default=None)
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--proxy", type=str, default=None)
    parser.add_argument("--mirror", type=str, default=None)
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args()
    runner = run_daemon_forever if args.foreground else run_daemon_server
    runner(
        pob_dir=args.pob_dir,
        port=args.port,
        runtime_dir=args.runtime_dir,
        offline=True if args.offline else None,
        proxy=args.proxy,
        mirror=args.mirror,
    )


if __name__ == "__main__":
    main()
