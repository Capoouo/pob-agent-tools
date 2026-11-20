"""Client for the official Path of Exile 1 Trade API with dynamic GGG rate limiting.

Implements the endpoints needed for upgrade search and market lookup:

* ``GET  /api/trade/data/leagues``
* ``GET  /api/trade/data/stats`` (7-day atomic disk cache)
* ``POST /api/trade/search/{league}``
* ``GET  /api/trade/fetch/{ids}?query={query_id}``

Design contracts:
* Env-only cookie handling (``POE_COOKIE`` / ``POESESSID``); never read from CLI
  flags, never logged, and always redacted from errors.
* Dynamic GGG ``X-Rate-Limit-*`` header parsing (matching PoB's
  ``TradeQueryRateLimiter.lua``) with atomic cross-process persistence in
  ``<cache_dir>/trade_ratelimit.json`` and 0ms sleep on the first request.
* Full proxy support via ``importers._make_httpx_client`` / ``_resolve_proxy``
  (explicit ``proxy``, ``POB_PROXY``, ``HTTPS_PROXY``, ``HTTP_PROXY``,
  ``ALL_PROXY``, including SOCKS5).
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

import httpx

from .importers import USER_AGENT, _get_cache_dir, _make_httpx_client, _resolve_proxy

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
# Redaction & env-only cookie helpers
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
    """Atomically write JSON to ``path`` via a sibling temp file + ``os.replace``."""
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
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError:
        pass
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
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


# ---------------------------------------------------------------------------
# Dynamic GGG Header Rate Limiter
# ---------------------------------------------------------------------------


class TradeRateLimiter:
    """Dynamic GGG ``X-Rate-Limit-*`` rate limiter with atomic disk persistence.

    Follows Path of Building's ``TradeQueryRateLimiter.lua`` header parsing
    semantics while persisting state across short-lived CLI invocations in
    ``<cache_dir>/trade_ratelimit.json``.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        limit_margin: int = DEFAULT_LIMIT_MARGIN,
        max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else _get_cache_dir()
        self.limit_margin = max(0, int(limit_margin))
        self.max_wait_sec = max(0.0, float(max_wait_sec))
        self._sleep = sleep
        self._now = now
        self._state: dict[str, Any] = {"version": 1, "aliases": {}, "policies": {}}

    @property
    def state_path(self) -> Path:
        return self.cache_dir / "trade_ratelimit.json"

    @staticmethod
    def _canonical_policy(policy: str) -> str:
        return _POLICY_ALIASES.get(policy, policy)

    def _resolve_policy(self, state: dict[str, Any], policy: str) -> str:
        canonical = self._canonical_policy(policy)
        aliases = state.get("aliases")
        if isinstance(aliases, dict):
            mapped = aliases.get(canonical)
            if isinstance(mapped, str) and mapped:
                return mapped
        return canonical

    def _read_disk_state(self) -> dict[str, Any] | None:
        path = self.state_path
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    @staticmethod
    def _policy_freshness(entry: dict[str, Any]) -> float:
        return float(entry.get("updated_at") or 0.0)

    def _prune_state(self, state: dict[str, Any], now: float) -> dict[str, Any]:
        pruned_policies: dict[str, Any] = {}
        policies = state.get("policies")
        if isinstance(policies, dict):
            for policy_name, raw_entry in policies.items():
                if not isinstance(raw_entry, dict):
                    continue
                entry: dict[str, Any] = {}
                retry_after = raw_entry.get("retry_after")
                if isinstance(retry_after, (int, float)) and float(retry_after) > now:
                    entry["retry_after"] = float(retry_after)

                max_period = 0
                pruned_rules: dict[str, dict[str, dict[str, Any]]] = {}
                raw_rules = raw_entry.get("rules")
                if isinstance(raw_rules, dict):
                    for rule_name, raw_buckets in raw_rules.items():
                        if not isinstance(raw_buckets, dict):
                            continue
                        kept_buckets: dict[str, dict[str, Any]] = {}
                        for period_key, bucket in raw_buckets.items():
                            if not isinstance(bucket, dict):
                                continue
                            try:
                                period_sec = int(bucket.get("period_sec") or int(period_key))
                                max_hits = int(bucket.get("max_hits") or 0)
                                penalty_sec = int(bucket.get("penalty_sec") or 0)
                                cur_hits = int(bucket.get("cur_hits") or 0)
                                active_timeout_sec = int(bucket.get("active_timeout_sec") or 0)
                                updated_at = float(bucket.get("updated_at") or 0.0)
                            except (TypeError, ValueError):
                                continue
                            if period_sec <= 0:
                                continue
                            window_active = (updated_at + period_sec) > now
                            timeout_active = (
                                active_timeout_sec > 0
                                and (updated_at + active_timeout_sec) > now
                            )
                            if not window_active and not timeout_active:
                                continue
                            max_period = max(max_period, period_sec)
                            kept_buckets[str(period_sec)] = {
                                "max_hits": max_hits,
                                "period_sec": period_sec,
                                "penalty_sec": penalty_sec,
                                "cur_hits": cur_hits,
                                "active_timeout_sec": active_timeout_sec,
                                "updated_at": updated_at,
                            }
                        if kept_buckets:
                            pruned_rules[str(rule_name)] = kept_buckets

                if pruned_rules:
                    entry["rules"] = pruned_rules

                raw_ts = raw_entry.get("timestamps")
                if isinstance(raw_ts, list) and max_period > 0:
                    cutoff = now - max_period
                    kept_ts = sorted(
                        float(t)
                        for t in raw_ts
                        if isinstance(t, (int, float)) and float(t) > cutoff and float(t) <= now + 5.0
                    )[-_MAX_TRACKED_TIMESTAMPS:]
                    if kept_ts:
                        entry["timestamps"] = kept_ts

                if entry:
                    entry["updated_at"] = float(raw_entry.get("updated_at") or 0.0)
                    pruned_policies[str(policy_name)] = entry

        aliases = state.get("aliases")
        kept_aliases = (
            {str(k): str(v) for k, v in aliases.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(aliases, dict)
            else {}
        )
        return {"version": 1, "aliases": kept_aliases, "policies": pruned_policies}

    def _load_state(self, now: float) -> dict[str, Any]:
        disk = self._read_disk_state()
        merged_aliases: dict[str, str] = {}
        merged_policies: dict[str, Any] = {}

        mem_aliases = self._state.get("aliases")
        if isinstance(mem_aliases, dict):
            merged_aliases.update(mem_aliases)
        mem_policies = self._state.get("policies")
        if isinstance(mem_policies, dict):
            for key, val in mem_policies.items():
                if isinstance(val, dict):
                    merged_policies[key] = val

        if disk is not None:
            disk_aliases = disk.get("aliases")
            if isinstance(disk_aliases, dict):
                merged_aliases.update(disk_aliases)
            disk_policies = disk.get("policies")
            if isinstance(disk_policies, dict):
                for key, disk_val in disk_policies.items():
                    if not isinstance(disk_val, dict):
                        continue
                    mem_val = merged_policies.get(key)
                    if mem_val is None or self._policy_freshness(disk_val) >= self._policy_freshness(mem_val):
                        merged_policies[key] = disk_val

        state = self._prune_state(
            {"version": 1, "aliases": merged_aliases, "policies": merged_policies},
            now,
        )
        self._state = state
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state = state
        _atomic_write_json(self.state_path, state)

    def before_request(self, policy: str) -> float:
        """Enforce GGG rate limits before issuing a request.

        Returns the number of seconds slept (``0.0`` when immediate).
        Raises ``RateLimited`` when an active penalty timeout is in effect or
        the required wait exceeds ``self.max_wait_sec``.
        """
        now = float(self._now())
        state = self._load_state(now)
        policy_key = self._resolve_policy(state, policy)
        entry = state["policies"].get(policy_key)
        if not entry:
            return 0.0

        # 1. Check active penalty timeouts first (never sleep out a penalty).
        penalty_wait = 0.0
        rules = entry.get("rules") or {}
        for buckets in rules.values():
            for bucket in buckets.values():
                active_timeout = int(bucket.get("active_timeout_sec") or 0)
                updated_at = float(bucket.get("updated_at") or 0.0)
                if active_timeout > 0:
                    remaining = (updated_at + active_timeout) - now
                    if remaining > 0:
                        penalty_wait = max(penalty_wait, remaining)
        if penalty_wait > 0:
            raise RateLimited(max(1, math.ceil(penalty_wait)))

        # 2. Compute required wait across Retry-After and saturated buckets.
        required_wait = 0.0
        retry_after = entry.get("retry_after")
        if isinstance(retry_after, (int, float)) and float(retry_after) > now:
            required_wait = max(required_wait, float(retry_after) - now)

        timestamps = [
            float(t)
            for t in (entry.get("timestamps") or [])
            if isinstance(t, (int, float))
        ]

        for buckets in rules.values():
            for bucket in buckets.values():
                max_hits = int(bucket.get("max_hits") or 0)
                period_sec = int(bucket.get("period_sec") or 0)
                cur_hits = int(bucket.get("cur_hits") or 0)
                updated_at = float(bucket.get("updated_at") or 0.0)
                if max_hits <= 0 or period_sec <= 0:
                    continue
                effective_limit = max(1, max_hits - self.limit_margin)
                if cur_hits >= effective_limit and (updated_at + period_sec) > now:
                    window_ts = [t for t in timestamps if t > (now - period_sec)]
                    if len(window_ts) >= cur_hits:
                        recent = window_ts[-cur_hits:]
                        need_expire = cur_hits - effective_limit + 1
                        idx = min(need_expire - 1, len(recent) - 1)
                        bucket_wait = (recent[idx] + period_sec) - now
                    else:
                        bucket_wait = (updated_at + period_sec) - now
                    if bucket_wait > 0:
                        required_wait = max(required_wait, bucket_wait)

        if required_wait <= 0:
            return 0.0
        if required_wait > self.max_wait_sec:
            raise RateLimited(max(1, math.ceil(required_wait)))
        self._sleep(required_wait)
        return required_wait

    def _apply_headers(
        self,
        policy: str,
        headers: Mapping[str, str],
        *,
        is_429: bool = False,
        default_retry_after: int = DEFAULT_RETRY_AFTER_SEC,
    ) -> int | None:
        now = float(self._now())
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}

        canonical = self._canonical_policy(policy)
        header_policy = lowered.get("x-rate-limit-policy", "").strip()
        target_policy = header_policy or canonical

        # Identify rules from X-Rate-Limit-Rules (or infer from *-state headers).
        rules_header = lowered.get("x-rate-limit-rules", "").strip()
        rule_names: list[str] = []
        if rules_header:
            for item in rules_header.split(","):
                name = item.strip().lower()
                if name and name not in rule_names:
                    rule_names.append(name)
        else:
            for key in lowered:
                match = _RULE_STATE_HEADER_RE.match(key)
                if match:
                    name = match.group(1)
                    if name not in rule_names:
                        rule_names.append(name)

        parsed_rules: dict[str, dict[str, dict[str, Any]]] = {}
        for rule_name in rule_names:
            limits_map = _parse_buckets(lowered.get(f"x-rate-limit-{rule_name}"))
            state_map = _parse_buckets(lowered.get(f"x-rate-limit-{rule_name}-state"))
            if not limits_map and not state_map:
                continue
            buckets: dict[str, dict[str, Any]] = {}
            all_periods = set(limits_map) | set(state_map)
            for period_sec in sorted(all_periods):
                max_hits, _, penalty_sec = limits_map.get(period_sec, (0, period_sec, 0))
                cur_hits, _, active_timeout_sec = state_map.get(period_sec, (0, period_sec, 0))
                buckets[str(period_sec)] = {
                    "max_hits": max_hits,
                    "period_sec": period_sec,
                    "penalty_sec": penalty_sec,
                    "cur_hits": cur_hits,
                    "active_timeout_sec": active_timeout_sec,
                    "updated_at": now,
                }
            if buckets:
                parsed_rules[rule_name] = buckets

        retry_after_sec = _parse_retry_after(
            lowered.get("retry-after"),
            now,
            default=default_retry_after if is_429 else None,
        )

        if not parsed_rules and retry_after_sec is None:
            return None

        state = self._load_state(now)
        if header_policy and canonical != header_policy:
            state["aliases"][canonical] = header_policy

        entry = dict(state["policies"].get(target_policy) or {})
        if parsed_rules:
            existing_rules = dict(entry.get("rules") or {})
            for rule_name, buckets in parsed_rules.items():
                existing_rules[rule_name] = buckets
            entry["rules"] = existing_rules

        if retry_after_sec is not None:
            prev_retry = float(entry.get("retry_after") or 0.0)
            entry["retry_after"] = max(prev_retry, now + retry_after_sec)

        timestamps = [
            float(t)
            for t in (entry.get("timestamps") or [])
            if isinstance(t, (int, float))
        ]
        timestamps.append(now)
        entry["timestamps"] = sorted(timestamps)[-_MAX_TRACKED_TIMESTAMPS:]
        entry["updated_at"] = now

        state["policies"][target_policy] = entry
        state = self._prune_state(state, now)
        self._save_state(state)
        return retry_after_sec

    def record_response(self, policy: str, headers: Mapping[str, str]) -> None:
        """Update and persist rate-limit state from a non-429 HTTP response."""
        self._apply_headers(policy, headers, is_429=False)

    def record_429(
        self,
        policy: str,
        headers: Mapping[str, str],
        default_retry_after: int = DEFAULT_RETRY_AFTER_SEC,
    ) -> int:
        """Record a 429 response's ``Retry-After`` and buckets before raising."""
        retry_after_sec = self._apply_headers(
            policy,
            headers,
            is_429=True,
            default_retry_after=default_retry_after,
        )
        return retry_after_sec if retry_after_sec is not None else default_retry_after


# ---------------------------------------------------------------------------
# TradeClient
# ---------------------------------------------------------------------------


class TradeClient:
    """Official Path of Exile 1 Trade API client with proxy & GGG rate-limit support."""

    def __init__(
        self,
        cookie: str | None = None,
        proxy: str | None = None,
        user_agent: str | None = None,
        timeout: float = 30.0,
        max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
        cache_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        logger: Callable[[str], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cookie = _normalize_cookie(cookie) if cookie is not None else _env_cookie()
        self.proxy = _resolve_proxy(proxy)
        self.user_agent = user_agent or os.environ.get("POE_TRADE_USER_AGENT") or USER_AGENT
        self.timeout = float(timeout)
        self.max_wait_sec = float(max_wait_sec)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else _get_cache_dir()
        self._sleep = sleep
        self._now = now
        self.logger = logger
        self.transport = transport
        self.rate_limiter = TradeRateLimiter(
            cache_dir=self.cache_dir,
            limit_margin=DEFAULT_LIMIT_MARGIN,
            max_wait_sec=self.max_wait_sec,
            sleep=self._sleep,
            now=self._now,
        )

    # -- low-level HTTP -------------------------------------------------

    def _headers(self, *, has_body: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _open_client(self, req_headers: dict[str, str]) -> httpx.Client:
        if self.transport is None:
            return _make_httpx_client(
                timeout=self.timeout,
                proxy=self.proxy,
                headers=req_headers,
            )
        return httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers=req_headers,
            transport=self.transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        policy: str = "data",
    ) -> Any:
        url = BASE_URL + path
        if params:
            url += "?" + urlencode(params)

        self.rate_limiter.before_request(policy)
        if self.logger:
            self.logger(f"[trade] {method} {path}")

        req_headers = self._headers(has_body=body is not None)
        try:
            with self._open_client(req_headers) as client:
                if body is not None:
                    response = client.request(method, url, json=body)
                else:
                    response = client.request(method, url)
        except httpx.TimeoutException:
            raise TradeError("Trade API request timed out; check the network or proxy.") from None
        except httpx.HTTPError as error:
            raise TradeError(f"Could not reach the trade API: {redact(str(error))}") from None

        status = response.status_code
        if status == 429:
            retry_after_sec = self.rate_limiter.record_429(policy, response.headers)
            raise RateLimited(retry_after_sec) from None

        self.rate_limiter.record_response(policy, response.headers)

        if status in (401, 403):
            raise CookieRequired(status, _snippet(response)) from None
        if status == 400:
            raise _api_error_from_response(response) from None
        if status >= 400:
            raise TradeError(f"Trade API failed: HTTP {status} {_snippet(response)}") from None

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise TradeError(f"Trade API returned non-JSON (HTTP {status})") from error

        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            if isinstance(err, dict):
                raise ApiError(
                    _as_int(err.get("code"), 0),
                    redact(str(err.get("message") or "unknown error")),
                )
            raise ApiError(0, redact(str(err)))
        return payload

    # -- reference data -------------------------------------------------

    def leagues(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/trade/data/leagues", policy="data")
        return list(payload.get("result") or [])

    def default_league(self) -> str:
        try:
            leagues = self.leagues()
        except TradeError:
            return "Standard"
        for league in leagues:
            league_id = str(league.get("id") or "").strip()
            if str(league.get("realm") or "pc").lower() != "pc":
                continue
            if not league_id or league_id.lower() in {"standard", "hardcore"}:
                continue
            if _SKIP_LEAGUE_RE.search(league_id):
                continue
            return league_id
        return "Standard"

    def stats(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        cache_path = self.cache_dir / "trade_stats.json"
        now = float(self._now())
        if not force_refresh:
            cached = _read_stats_cache(cache_path, now)
            if cached is not None:
                return cached

        payload = self._request("GET", "/api/trade/data/stats", policy="data")
        groups = list(payload.get("result") or [])
        _atomic_write_json(cache_path, {"fetchedAt": now, "result": groups})
        return groups

    # -- search / fetch -------------------------------------------------

    def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
        path = f"/api/trade/search/{_quote(league)}"
        payload = self._request("POST", path, body=query, policy="search")
        if not payload.get("result"):
            raise NoResults()
        return payload

    def fetch(self, ids: list[str], query_id: str) -> list[dict[str, Any]]:
        if not ids:
            return []
        if len(ids) > MAX_FETCH_IDS:
            raise ValueError(f"fetch accepts at most {MAX_FETCH_IDS} ids at once")
        joined = ",".join(_quote(item_id) for item_id in ids)
        path = f"/api/trade/fetch/{joined}"
        payload = self._request("GET", path, params={"query": query_id}, policy="fetch")
        return [row for row in (payload.get("result") or []) if row]

    def fetch_many(
        self,
        ids: list[str],
        query_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not ids or limit <= 0:
            return []
        cap = min(int(limit), MAX_FETCH_MANY)
        target_ids = list(ids[:cap])
        results: list[dict[str, Any]] = []
        for start in range(0, len(target_ids), MAX_FETCH_IDS):
            chunk = target_ids[start : start + MAX_FETCH_IDS]
            results.extend(self.fetch(chunk, query_id))
        return results

    def search_url(self, league: str, query_id: str) -> str:
        return f"{BASE_URL}/trade/search/{_quote(league)}/{_quote(query_id)}"


def _api_error_from_response(response: httpx.Response) -> ApiError:
    try:
        payload = response.json()
        error = payload.get("error") or {}
        if isinstance(error, dict):
            return ApiError(
                _as_int(error.get("code"), 0),
                redact(str(error.get("message") or "bad request")),
            )
        return ApiError(0, redact(str(error)))
    except (ValueError, TypeError, AttributeError):
        return ApiError(0, _snippet(response))


def _read_stats_cache(path: Path, now: float) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(payload.get("fetchedAt") or 0.0)
        if now - fetched_at > STATS_TTL_SECONDS:
            return None
        result = payload.get("result")
        return list(result) if isinstance(result, list) and result else None
    except (OSError, ValueError, TypeError):
        return None
