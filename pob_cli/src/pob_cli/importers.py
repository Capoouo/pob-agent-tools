"""Multi-source build importers (XML, Base64 share code, 8 share URLs, poe.ninja character URLs)."""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any

import httpx

from .bridge_client import RUNTIME_DIR, BridgeError, BridgeWorker

USER_AGENT = "pob-cli/0.1.0 (+https://github.com/PathOfBuildingCommunity/PathOfBuilding)"

POE_NINJA_CHAR_RE = re.compile(
    r"^https?://poe\.ninja/poe1/builds/(?P<league>[^/?#]+)/character/(?P<account>[^/?#]+)/(?P<name>[^/?#]+)",
    re.IGNORECASE,
)

SHARE_RAW_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^https?://pobb\.in/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://pobb.in/{0}/raw"),
    (re.compile(r"^https?://poedb\.tw/(?:[a-z]{2}/)?pob/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://poedb.tw/pob/{0}/raw"),
    (re.compile(r"^https?://poe\.ninja/pob/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://poe.ninja/pob/{0}/raw"),
    (re.compile(r"^https?://poe\.ninja/poe1/pob/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://poe.ninja/poe1/pob/{0}/raw"),
    (re.compile(r"^https?://rentry\.co/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://rentry.co/{0}/raw"),
    (re.compile(r"^https?://pastebin\.com/(?:raw/)?([A-Za-z0-9_-]+)(?:/.*)?$"), "https://pastebin.com/raw/{0}"),
    (re.compile(r"^https?://pastebinp\.com/(?:raw/)?([A-Za-z0-9_-]+)(?:/.*)?$"), "https://pastebinp.com/raw/{0}"),
    (re.compile(r"^https?://pob\.codes/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://pob.codes/{0}/raw"),
]

MAXROLL_POB_RE = re.compile(r"^https?://maxroll\.gg/poe/pob/([A-Za-z0-9_-]+)", re.IGNORECASE)


def xml_to_share_code(xml_text: str) -> str:
    """Compress raw PoB XML string into a URL-safe Base64 PoB share code."""
    compressed = zlib.compress(xml_text.encode("utf-8"), level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii")


def share_code_to_xml(code: str) -> str:
    """Decompress a URL-safe Base64 PoB share code into raw PoB XML text."""
    cleaned = code.strip().replace("-", "+").replace("_", "/")
    pad = (-len(cleaned)) % 4
    if pad:
        cleaned += "=" * pad
    raw_bytes = base64.b64decode(cleaned)
    try:
        decompressed = zlib.decompress(raw_bytes)
    except zlib.error:
        decompressed = zlib.decompress(raw_bytes, -15)
    return decompressed.decode("utf-8", errors="replace")


def _get_cache_dir() -> Path:
    cache_dir = RUNTIME_DIR / "cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir
    except OSError:
        fallback = Path.home() / ".cache" / "pob-cli"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _fetch_poe_ninja_index_state(ttl_sec: int = 900) -> dict[str, Any]:
    cache_file = _get_cache_dir() / "poe_ninja_index_state.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                return cached["data"]
        except Exception:
            pass

    url = "https://poe.ninja/poe1/api/data/index-state"
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()

    try:
        cache_file.write_text(json.dumps({"_ts": now, "data": data}), encoding="utf-8")
    except Exception:
        pass
    return data


def fetch_poe_ninja_character_code(url: str) -> str:
    """Resolve a poe.ninja character URL into a raw PoB share code."""
    m = POE_NINJA_CHAR_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a valid poe.ninja character URL: {url}")

    league_slug = urllib.parse.unquote(m.group("league"))
    raw_account = urllib.parse.unquote(m.group("account"))
    char_name = urllib.parse.unquote(m.group("name"))

    # Convert poe.ninja account discriminator suffix `-1234` back to `#1234`
    account = re.sub(r"-(\d{4})$", r"#\1", raw_account)

    parsed_url = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed_url.query)
    time_machine = qs.get("timemachine", [None])[0]

    index_state = _fetch_poe_ninja_index_state()
    snapshot_versions = index_state.get("snapshotVersions", [])

    # Determine snapshot type ("depthsolo" if league_slug ends with "delve" or similar, else "exp")
    candidates = [
        sv
        for sv in snapshot_versions
        if sv.get("url", "").lower() == league_slug.lower()
    ]
    if not candidates:
        raise RuntimeError(
            f"Could not find league slug '{league_slug}' in poe.ninja index-state snapshotVersions."
        )

    # Prefer type == "exp" first, then fallback to others (e.g. "depthsolo")
    candidates.sort(key=lambda sv: 0 if sv.get("type") == "exp" else 1)

    last_err: Exception | None = None
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for sv in candidates:
            overview = sv.get("snapshotName")
            snap_type = sv.get("type", "exp")
            params: dict[str, str] = {
                "overview": overview,
                "type": snap_type,
                "account": account,
                "name": char_name,
            }
            if time_machine:
                params["timeMachine"] = time_machine
            try:
                resp = client.get("https://poe.ninja/poe1/pob/raw/overview/code", params=params)
                if resp.status_code == 200 and resp.text.strip():
                    code = resp.text.strip().strip('"')
                    # Validate it decodes cleanly
                    share_code_to_xml(code)
                    return code
            except Exception as exc:
                last_err = exc

    raise RuntimeError(f"Failed to fetch PoB code for poe.ninja character '{account}/{char_name}': {last_err}")


def fetch_share_url_code_fallback(url: str) -> str:
    """Fallback Python httpx fetcher for share site URLs when Curl/Lua fails."""
    clean_url = url.strip()
    if POE_NINJA_CHAR_RE.match(clean_url):
        return fetch_poe_ninja_character_code(clean_url)

    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for pattern, template in SHARE_RAW_PATTERNS:
            m = pattern.match(clean_url)
            if m:
                raw_url = template.format(m.group(1))
                resp = client.get(raw_url)
                resp.raise_for_status()
                code = resp.text.strip()
                share_code_to_xml(code)
                return code

        m_maxroll = MAXROLL_POB_RE.match(clean_url)
        if m_maxroll:
            code_id = m_maxroll.group(1)
            resp = client.get(
                f"https://maxroll.gg/poe/api/pob/{code_id}",
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
            code = payload.get("build") or payload.get("code") or payload.get("data")
            if isinstance(code, str) and code.strip():
                share_code_to_xml(code.strip())
                return code.strip()

        # Generic raw GET attempt
        resp = client.get(clean_url)
        resp.raise_for_status()
        text = resp.text.strip()
        share_code_to_xml(text)
        return text


def load_build_into_worker(
    worker: BridgeWorker,
    *,
    source: str | None = None,
    xml_code: str | None = None,
) -> dict[str, Any]:
    """Load a build into `worker` from an encoded XML share code, URL, or raw Base64 code.

    Follows R11: local file I/O happens on the CLI side (passed as `xml_code`), never on the daemon.
    Always snapshots `export_code` into `worker.last_good_code` and clears undo/redo history on load.
    """
    with worker.lock:
        import_method = "code"
        if xml_code:
            res = worker.call("import_code", {"code": xml_code.strip(), "mode": "replace"})
            import_method = "xml_file"
        elif source:
            src = source.strip()
            if POE_NINJA_CHAR_RE.match(src):
                code = fetch_poe_ninja_character_code(src)
                res = worker.call("import_code", {"code": code, "mode": "replace"})
                import_method = "poe_ninja_character"
            elif src.startswith(("http://", "https://")):
                try:
                    url_res = worker.call("import_from_url", {"url": src})
                    code = (url_res.get("code") or "").strip()
                    if not code:
                        raise BridgeError("import_from_url returned empty code")
                    res = worker.call("import_code", {"code": code, "mode": "replace"})
                    import_method = "url_native"
                except BridgeError:
                    code = fetch_share_url_code_fallback(src)
                    res = worker.call("import_code", {"code": code, "mode": "replace"})
                    import_method = "url_fallback"
            else:
                # Raw Base64 share code
                res = worker.call("import_code", {"code": src, "mode": "replace"})
                import_method = "share_code"
        else:
            raise ValueError("Either 'source' or 'xml_code' must be provided.")

        # Snapshot current build state as last_good_code and reset undo/redo
        exp = worker.call("export_code", {})
        worker.last_good_code = exp.get("code")
        worker.source_label = source or "xml_file"
        worker.undo_stack.clear()
        worker.redo_stack.clear()
        worker.save_slot_state()

        info = worker.call("get_build_info", {})
        header = worker.call("get_build_header", {})
        return {
            "importMethod": import_method,
            "importResult": res,
            "buildInfo": info,
            "buildHeader": header,
        }
