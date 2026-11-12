"""Local HTTP daemon managing persistent `main` and lazy `ref` headless PoB workers."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from .analyzer import (
    audit_build,
    extract_summary,
    inspect_items,
    inspect_skills,
    inspect_tree,
    search_calcs_breakdown,
)
from .bridge_client import (
    DEFAULT_POB_DIR,
    DEFAULT_PROJECT_ROOT,
    RUNTIME_DIR,
    BridgeError,
    BridgeWorker,
)
from .comparator import borrow_from_ref, compare_builds, rank_gems, rank_nodes
from .importers import load_build_into_worker, share_code_to_xml
from .mutators import (
    add_skill_gem,
    anoint_item,
    change_tree,
    modify_config,
    modify_skill,
    remove_skill_gem,
    run_batch,
    set_flask_active,
    switch_main,
    try_item,
)
from .snapshot import redo_build, undo_build

DAEMON_STATE_FILE = RUNTIME_DIR / "daemon.json"
DAEMON_LOCK_FILE = RUNTIME_DIR / "daemon.start.lock"
REF_IDLE_TIMEOUT_SEC = 300.0     # 5 minutes
GLOBAL_IDLE_TIMEOUT_SEC = 1800.0  # 30 minutes


class DaemonManager:
    """Holds `main` and lazy `ref` BridgeWorkers with idle recycling."""

    def __init__(self, pob_dir: Path, token: str) -> None:
        self.pob_dir = pob_dir
        self.token = token
        self.started_at = time.time()
        self.last_active_ts = time.time()
        self._lock = threading.RLock()
        self.main_worker = BridgeWorker(slot_name="main", pob_dir=pob_dir)
        self.ref_worker: BridgeWorker | None = None
        self.server: ThreadingHTTPServer | None = None
        self._stopping = False

    def start(self) -> None:
        self.main_worker.start()

    def get_worker(self, target: str = "main") -> BridgeWorker:
        self.last_active_ts = time.time()
        if target == "ref":
            with self._lock:
                if self.ref_worker is None:
                    self.ref_worker = BridgeWorker(slot_name="ref", pob_dir=self.pob_dir)
                self.ref_worker.ensure_alive()
                return self.ref_worker
        self.main_worker.ensure_alive()
        return self.main_worker

    def check_idle(self) -> bool:
        """Recycle `ref` if idle > 5m; return True if global daemon should shut down (> 30m idle)."""
        now = time.time()
        with self._lock:
            if (
                self.ref_worker is not None
                and self.ref_worker.is_alive()
                and (now - self.ref_worker.last_active_ts) > REF_IDLE_TIMEOUT_SEC
            ):
                try:
                    self.ref_worker.stop()
                except Exception:
                    pass
                self.ref_worker = None
        return (now - self.last_active_ts) > GLOBAL_IDLE_TIMEOUT_SEC

    def shutdown_all(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        with self._lock:
            try:
                self.main_worker.stop()
            except Exception:
                pass
            if self.ref_worker is not None:
                try:
                    self.ref_worker.stop()
                except Exception:
                    pass
                self.ref_worker = None
        try:
            if DAEMON_STATE_FILE.exists():
                DAEMON_STATE_FILE.unlink()
        except Exception:
            pass
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.last_active_ts = time.time()
        target = str(params.get("target") or "main").lower()

        if action == "status":
            main_alive = self.main_worker.is_alive()
            main_info = None
            if main_alive:
                try:
                    main_info = self.main_worker.call("get_build_info", {})
                except Exception:
                    pass
            ref_alive = self.ref_worker is not None and self.ref_worker.is_alive()
            ref_info = None
            if ref_alive and self.ref_worker is not None:
                try:
                    ref_info = self.ref_worker.call("get_build_info", {})
                except Exception:
                    pass
            return {
                "pid": os.getpid(),
                "uptimeSec": round(time.time() - self.started_at, 1),
                "idleSec": round(time.time() - self.last_active_ts, 1),
                "main": {
                    "alive": main_alive,
                    "hello": self.main_worker.hello_info,
                    "buildInfo": main_info,
                    "undoDepth": len(self.main_worker.undo_stack),
                    "redoDepth": len(self.main_worker.redo_stack),
                    "hasLoadedBuild": bool(self.main_worker.last_good_code),
                },
                "ref": {
                    "alive": ref_alive,
                    "buildInfo": ref_info,
                    "hasLoadedBuild": bool(self.ref_worker and self.ref_worker.last_good_code),
                },
            }

        if action == "stop":
            self.shutdown_all()
            return {"stopped": True}

        if action == "load":
            w = self.get_worker(target)
            return load_build_into_worker(
                w,
                source=params.get("source"),
                xml_code=params.get("xml_code"),
            )

        if action == "export":
            w = self.get_worker(target)
            with w.lock:
                code_res = w.call("export_code", {})
                code_str = code_res.get("code", "")
                xml_str = None
                if params.get("include_xml"):
                    xml_res = w.call("save_xml", {})
                    xml_str = xml_res.get("xml") or share_code_to_xml(code_str)
                return {"code": code_str, "xml": xml_str}

        if action == "switch_main":
            w = self.get_worker(target)
            return switch_main(
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

        if action == "undo":
            return undo_build(self.get_worker(target))

        if action == "redo":
            return redo_build(self.get_worker(target))

        if action == "summary":
            return extract_summary(self.get_worker(target))

        if action == "audit":
            return audit_build(self.get_worker(target))

        if action == "items":
            return inspect_items(
                self.get_worker(target),
                slot_name=params.get("slot"),
                raw=bool(params.get("raw", False)),
            )

        if action == "skills":
            return inspect_skills(
                self.get_worker(target),
                group_selector=params.get("group"),
            )

        if action == "tree":
            return inspect_tree(
                self.get_worker(target),
                search=params.get("search"),
            )

        if action == "calc":
            return search_calcs_breakdown(
                self.get_worker(target),
                keyword=str(params.get("keyword") or ""),
            )

        if action == "item_try":
            return try_item(
                self.get_worker(target),
                raw_text=str(params.get("raw") or ""),
                slot_name=params.get("slot"),
                tooltip_only=bool(params.get("tooltip_only", False)),
                apply=bool(params.get("apply", False)),
            )

        if action == "item_flask":
            return set_flask_active(
                self.get_worker(target),
                flask_index=params["flask_index"],
                active=bool(params["active"]),
                apply=bool(params.get("apply", False)),
            )

        if action == "item_anoint":
            return anoint_item(
                self.get_worker(target),
                slot_name=str(params.get("slot") or "Amulet"),
                notable_query=str(params.get("notable") or ""),
                anoint_index=int(params.get("index") or 1),
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_set":
            return modify_skill(
                self.get_worker(target),
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
            return add_skill_gem(
                self.get_worker(target),
                group_selector=str(params.get("group") or "main"),
                gem_name=str(params.get("gem") or ""),
                level=int(params.get("level") or 20),
                quality=int(params.get("quality") or 20),
                enabled=bool(params.get("enabled", True)),
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_rm":
            return remove_skill_gem(
                self.get_worker(target),
                group_selector=str(params.get("group") or "main"),
                gem_selector=params["gem"],
                apply=bool(params.get("apply", False)),
            )

        if action == "tree_change":
            return change_tree(
                self.get_worker(target),
                allocate=params.get("allocate"),
                deallocate=params.get("deallocate"),
                apply=bool(params.get("apply", False)),
            )

        if action == "config_set":
            return modify_config(
                self.get_worker(target),
                assignments=params.get("assignments") or [],
                custom_mods=params.get("custom_mods"),
                apply=bool(params.get("apply", False)),
            )

        if action == "batch":
            return run_batch(
                self.get_worker(target),
                ops=params.get("ops") or [],
                apply=bool(params.get("apply", False)),
            )

        if action == "rank_nodes":
            return rank_nodes(
                self.get_worker(target),
                stat=str(params.get("stat") or "CombinedDPS"),
                max_dist=int(params.get("max_dist") or 5),
                top=int(params.get("top") or 15),
            )

        if action == "rank_gems":
            return rank_gems(
                self.get_worker(target),
                group_selector=str(params.get("group") or "main"),
                slot_idx=params.get("slot_idx"),
                add_link=bool(params.get("add_link", False)),
                field=str(params.get("field") or "CombinedDPS"),
                top=int(params.get("top") or 15),
            )

        if action == "compare":
            mw = self.get_worker("main")
            rw = self.get_worker("ref")
            return compare_builds(
                mw,
                rw,
                ref_source=params.get("ref_source"),
                ref_xml_code=params.get("ref_xml_code"),
            )

        if action == "borrow":
            mw = self.get_worker("main")
            rw = self.get_worker("ref")
            if not rw.last_good_code:
                raise RuntimeError("No reference build loaded in 'ref' slot. Run `pob load <source> --target ref` or `pob compare <source>` first.")
            return borrow_from_ref(
                mw,
                rw,
                item_slots=params.get("item_slots"),
                skill_group=params.get("skill_group"),
                apply=bool(params.get("apply", False)),
            )

        raise ValueError(f"Unknown daemon action '{action}'.")


def make_handler(manager: DaemonManager) -> type[BaseHTTPRequestHandler]:
    class RpcHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress noisy stdout access logs
            pass

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            token = self.headers.get("X-PoB-Token", "")
            if not secrets.compare_digest(token, manager.token):
                self._send_json(403, {"ok": False, "error": "Forbidden: invalid or missing X-PoB-Token"})
                return

            if self.path != "/rpc":
                self._send_json(404, {"ok": False, "error": "Not found"})
                return

            length = int(self.headers.get("Content-Length", "0"))
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


def run_daemon_server(pob_dir: Path = DEFAULT_POB_DIR, port: int = 0) -> None:
    """Start the local daemon server, write `.runtime/daemon.json`, and serve until stopped or idle."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    manager = DaemonManager(pob_dir=pob_dir, token=token)
    manager.start()

    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(manager))
    manager.server = server
    actual_port = server.server_address[1]

    state_payload = {
        "pid": os.getpid(),
        "port": actual_port,
        "token": token,
        "started_at": manager.started_at,
        "pob_dir": str(pob_dir),
    }
    tmp_state_file = DAEMON_STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
    tmp_state_file.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")
    os.replace(tmp_state_file, DAEMON_STATE_FILE)

    def _watchdog() -> None:
        while not manager._stopping:
            time.sleep(15.0)
            if manager.check_idle():
                manager.shutdown_all()
                break

    threading.Thread(target=_watchdog, name="pob-daemon-watchdog", daemon=True).start()

    try:
        server.serve_forever()
    finally:
        manager.shutdown_all()


# --- Client helper used by CLI ---


def read_daemon_state() -> dict[str, Any] | None:
    if not DAEMON_STATE_FILE.exists():
        return None
    try:
        return json.loads(DAEMON_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def call_daemon(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Send an authenticated RPC to the local PoB daemon, starting it automatically if not running."""
    state = read_daemon_state()
    if state is not None:
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    f"http://127.0.0.1:{state['port']}/rpc",
                    headers={"X-PoB-Token": state["token"]},
                    json={"action": action, "params": params or {}},
                )
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(data.get("error") or "Daemon error")
                return data["result"]
        except httpx.RequestError:
            # Stale daemon.json; clean up and fall through to auto_start
            try:
                DAEMON_STATE_FILE.unlink()
            except Exception:
                pass
            state = None

    if not auto_start:
        raise RuntimeError("PoB daemon is not running.")

    # Spawn background daemon process serialized via startup lockfile (P1-8)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNTIME_DIR / "daemon.log"

    acquired_lock = False
    if DAEMON_LOCK_FILE.exists():
        try:
            if time.time() - DAEMON_LOCK_FILE.stat().st_mtime > 30.0:
                DAEMON_LOCK_FILE.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(DAEMON_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        acquired_lock = True
    except OSError:
        acquired_lock = False

    try:
        if acquired_lock:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                )

            with open(log_path, "a", encoding="utf-8") as log_fh:
                subprocess.Popen(
                    [sys.executable, "-m", "pob_cli.daemon"],
                    cwd=str(DEFAULT_PROJECT_ROOT),
                    stdout=log_fh,
                    stderr=log_fh,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                )

        # Wait up to 25 seconds for daemon.json to appear and respond
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            st = read_daemon_state()
            if st is not None:
                try:
                    with httpx.Client(timeout=5.0) as client:
                        ping = client.post(
                            f"http://127.0.0.1:{st['port']}/rpc",
                            headers={"X-PoB-Token": st["token"]},
                            json={"action": "status", "params": {}},
                        )
                        if ping.status_code == 200:
                            break
                except Exception:
                    pass
            time.sleep(0.25)
        else:
            raise RuntimeError(f"Timed out waiting for PoB daemon to start. Check {log_path}.")
    finally:
        if acquired_lock:
            try:
                DAEMON_LOCK_FILE.unlink()
            except OSError:
                pass

    st = read_daemon_state()
    if not st:
        raise RuntimeError("PoB daemon state file disappeared after startup.")

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"http://127.0.0.1:{st['port']}/rpc",
            headers={"X-PoB-Token": st["token"]},
            json={"action": action, "params": params or {}},
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Daemon error")
        return data["result"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless PoB local HTTP daemon")
    parser.add_argument("--pob-dir", type=Path, default=DEFAULT_POB_DIR)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    run_daemon_server(pob_dir=args.pob_dir, port=args.port)


if __name__ == "__main__":
    main()
