"""Shared constants, exceptions, redaction, atomic JSON I/O, and lockfile helpers for trade_client."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://www.pathofexile.com"
STATS_TTL_SECONDS = 7 * 24 * 3600
MAX_FETCH_IDS = 10
MAX_FETCH_MANY = 20
DEFAULT_LIMIT_MARGIN = 1
DEFAULT_MAX_WAIT_SEC = 15.0
DEFAULT_RETRY_AFTER_SEC = 60
_MAX_TRACKED_TIMESTAMPS = 500

_POLICY_ALIASES: dict[str, str] = {
    "search": "trade-search-request-limit",
    "fetch": "trade-fetch-request-limit",
    "data": "trade-data-request-limit",
}

_COOKIE_SECRET_RE = re.compile(r"\b(?:POESESSID|cf_clearance)=[^;\s]+", re.IGNORECASE)
_HEX_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_RULE_STATE_HEADER_RE = re.compile(r"^x-rate-limit-([a-z0-9_-]+)-state$")

_SKIP_LEAGUE_RE = re.compile(
    r"\b(?:hardcore|hc|ruthless|ssf|private)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TradeError(RuntimeError):
    """Base class for every trade API failure (message is safe to show)."""


class CookieRequired(TradeError):
    """Raised on HTTP 401/403 when a PoE session cookie / CF clearance is needed."""

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = int(status)
        self.detail = redact(detail) if detail else ""
        message = (
            f"Trade API returned HTTP {self.status}: a browser session / Cloudflare "
            "check is required.\n"
            "Set POESESSID (or a full POE_COOKIE) in the environment, then retry. "
            "Never pass the cookie on the command line or paste it into a report."
        )
        if self.detail:
            message += f"\nServer response: {self.detail[:300]}"
        super().__init__(message)


class RateLimited(TradeError):
    """Raised when GGG rate-limit wait exceeds max_wait_sec or HTTP 429 is returned."""

    def __init__(self, seconds: int) -> None:
        self.seconds = int(seconds)
        super().__init__(
            f"Trade API rate limit hit. Wait about {self.seconds}s before retrying; "
            "do not hammer the endpoint."
        )


class ApiError(TradeError):
    """Raised on HTTP 400 or JSON payload containing an ``error`` object."""

    def __init__(self, code: int, message: str) -> None:
        self.code = int(code)
        self.message = str(message)
        super().__init__(f"Trade API error {self.code}: {self.message}")


class NoResults(TradeError):
    """Raised when a trade search succeeds with zero matching listings."""

    def __init__(self) -> None:
        super().__init__("No matching online listings; loosen the budget or the stat filters.")


# ---------------------------------------------------------------------------
# Redaction, env-only cookie, atomic write, and cross-process lockfile helpers
# ---------------------------------------------------------------------------


def redact(text: str) -> str:
    """Remove cookie secrets and >=16-char hex tokens from user-facing text."""
    if not text:
        return ""
    cleaned = _COOKIE_SECRET_RE.sub("[redacted]", text)
    return _HEX_TOKEN_RE.sub("[redacted]", cleaned)


def _normalize_cookie(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if "=" in value:
        return value
    return f"POESESSID={value}"


def _env_cookie() -> str | None:
    """Read session cookie strictly from ``POE_COOKIE`` or ``POESESSID`` env vars."""
    for env_key in ("POE_COOKIE", "POESESSID"):
        normalized = _normalize_cookie(os.environ.get(env_key))
        if normalized:
            return normalized
    return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_retry_after(value: str | None, now: float, default: int | None = None) -> int | None:
    if value is None:
        return default
    raw = str(value).strip()
    if not raw:
        return default
    try:
        return max(1, math.ceil(float(raw)))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        delta = dt.timestamp() - now
        return max(1, math.ceil(delta))
    except (TypeError, ValueError, OverflowError):
        return default


def _snippet(response: httpx.Response, limit: int = 300) -> str:
    return redact(" ".join(response.text[:limit].split()))


def _quote(value: str) -> str:
    return quote(str(value), safe="")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically write JSON to ``path`` via a sibling temp file + ``os.replace`` with Windows retry."""
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        for attempt in range(3):
            try:
                os.replace(tmp_path, path)
                tmp_path = None
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.02 * (attempt + 1))
    except OSError:
        pass
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        if pid > 0xFFFFFFFF:
            return False
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            open_process.restype = ctypes.c_void_p
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            get_exit_code.restype = ctypes.c_int
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int

            handle = open_process(0x1000, 0, int(pid))
            if not handle:
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED -> alive
            try:
                exit_code = ctypes.c_ulong(0)
                if get_exit_code(handle, ctypes.byref(exit_code)):
                    return int(exit_code.value) == 259  # STILL_ACTIVE
                return False
            finally:
                close_handle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def _is_lockfile_stale(lock_path: Path, *, stale_sec: float = 15.0) -> bool:
    try:
        st = lock_path.stat()
    except (OSError, OverflowError):
        return False
    if (time.time() - st.st_mtime) > stale_sec:
        return True
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            ts = payload.get("ts")
            if isinstance(ts, (int, float)) and (time.time() - float(ts)) > stale_sec:
                return True
            pid = int(payload.get("pid") or 0)
            if pid > 0 and pid != os.getpid() and not _is_pid_alive(pid):
                return True
    except Exception:
        pass
    return False


def _try_break_stale_lockfile(lock_path: Path) -> bool:
    trash = lock_path.parent / f".{lock_path.name}.stale.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    try:
        os.replace(lock_path, trash)
    except (OSError, OverflowError):
        return False
    try:
        trash.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _release_lockfile(lock_path: Path, owner_token: str) -> None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("token") != owner_token:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _parse_buckets(header_value: str | None) -> dict[int, tuple[int, int, int]]:
    """Parse ``hits:period_sec:timeout_sec,...`` into ``{period_sec: (hits, period, timeout)}``."""
    buckets: dict[int, tuple[int, int, int]] = {}
    if not header_value:
        return buckets
    for part in header_value.split(","):
        piece = part.strip()
        if not piece:
            continue
        fields = [f.strip() for f in piece.split(":")]
        if len(fields) != 3:
            continue
        try:
            hits = int(float(fields[0]))
            period = int(float(fields[1]))
            timeout = int(float(fields[2]))
        except ValueError:
            continue
        if period <= 0:
            continue
        buckets[period] = (hits, period, timeout)
    return buckets
