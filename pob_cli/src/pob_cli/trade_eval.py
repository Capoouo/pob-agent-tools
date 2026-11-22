"""Compare PoE1 trade candidates against an equipped item, verify via PoB, rank, and report.

No DPS or EHP is ever estimated in Python (R5). Template scoring only summarizes
numeric modifier deltas and asking price; real DPS/EHP comes exclusively from the
local headless PoB daemon (`trade_eval_items` batch RPC with `item_try` fallback)
and is rendered as `-` when unverified.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .formatters import _parse_numeric_or_none, fmt_delta, fmt_num, render_table
from .trade_item import (
    Conversion,
    Item,
    mods_by_template,
    normalize_template,
    sanitize_text,
)

DEFAULT_WEIGHTS: dict[str, float] = {
    "life": 1.0,
    "mana": 0.35,
    "energy_shield": 0.55,
    "armour": 0.35,
    "evasion": 0.35,
    "resist": 0.8,
    "attributes": 0.4,
    "damage": 1.0,
    "speed": 0.6,
    "crit": 0.5,
    "skill_level": 1.2,
    "accuracy": 0.2,
    "defense": 0.4,
    "rarity": 0.05,
    "penalty": -0.5,
    "other": 0.1,
}

PREFERRED_BONUS = 1.6
POB_EHP_WEIGHT = 0.25

_DASH_RE = re.compile(r"[\u2013\u2014\u2212]")
_BENCH_RANGE_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*\)")


# ---------------------------------------------------------------------------
# Numeric & JSON safety helpers (hardened against PoB 'inf' / '-inf' / 'nan')
# ---------------------------------------------------------------------------


def _safe_pct(before: Any, after: Any) -> float | None:
    """Compute rounded percentage change safely; returns None for non-finite or zero-base inputs."""
    if before is None or after is None:
        return None
    if isinstance(before, str) and not before.strip():
        return None
    if isinstance(after, str) and not after.strip():
        return None
    bf = _parse_numeric_or_none(before)
    af = _parse_numeric_or_none(after)
    if bf is None or af is None:
        return None
    if not (math.isfinite(bf) and math.isfinite(af)):
        return None
    if abs(bf) < 1e-9:
        return 0.0 if abs(af) < 1e-9 else None
    return round((af - bf) / abs(bf) * 100.0, 2)


def _finite_float(val: Any, default: float = 0.0) -> float:
    """Parse any value to a strictly finite float (never NaN/Inf)."""
    if val is None:
        return default
    if isinstance(val, str) and not val.strip():
        return default
    parsed = _parse_numeric_or_none(val)
    if parsed is None or not math.isfinite(parsed):
        return default
    return float(parsed)


def _json_safe_optional_float(val: Any) -> float | None:
    """Return a finite float or None (for RFC-8259 JSON fields like price_amount)."""
    if val is None:
        return None
    parsed = _parse_numeric_or_none(val)
    if parsed is None or not math.isfinite(parsed):
        return None
    return float(parsed)


def _json_safe_stat(val: Any) -> float | str:
    """Convert a PoB stat value into a finite float or sentinel string ('inf', '-inf', 'nan')."""
    if val is None:
        return 0.0
    if isinstance(val, bool):
        return float(int(val))
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else fmt_num(f)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0.0
        parsed = _parse_numeric_or_none(s)
        if parsed is None:
            return s
        return parsed if math.isfinite(parsed) else fmt_num(parsed)
    return str(val)


def _is_pos_inf(val: Any) -> bool:
    if val is None or (isinstance(val, str) and not val.strip()):
        return False
    parsed = _parse_numeric_or_none(val)
    return parsed is not None and math.isinf(parsed) and parsed > 0


def _finite_sort_key(val: float | None, default: float = 0.0) -> float:
    if val is None or not math.isfinite(val):
        return default
    return float(val)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ModDelta:
    template: str
    category: str
    current: float
    candidate: float
    delta: float
    contribution: float
    example: str
    kind: str

    @property
    def is_gain(self) -> bool:
        return self.contribution > 0


@dataclass
class PobEval:
    dps_key: str
    dps_before: float | str
    dps_after: float | str
    ehp_before: float | str
    ehp_after: float | str
    life_before: float
    life_after: float
    warnings: tuple[str, ...] = ()
    resolved_warnings: tuple[str, ...] = ()

    @property
    def dps_pct(self) -> float | None:
        return _safe_pct(self.dps_before, self.dps_after)

    @property
    def ehp_pct(self) -> float | None:
        return _safe_pct(self.ehp_before, self.ehp_after)

    @property
    def life_delta(self) -> float:
        bf = _parse_numeric_or_none(self.life_before)
        af = _parse_numeric_or_none(self.life_after)
        if bf is None or af is None or not (math.isfinite(bf) and math.isfinite(af)):
            return 0.0
        return round(af - bf, 4)

    @property
    def combined_pct(self) -> float:
        dps = self.dps_pct if (self.dps_pct is not None and math.isfinite(self.dps_pct)) else 0.0
        ehp = self.ehp_pct if (self.ehp_pct is not None and math.isfinite(self.ehp_pct)) else 0.0
        return round(dps + POB_EHP_WEIGHT * ehp, 4)

    @property
    def dps_pct_str(self) -> str:
        pct = self.dps_pct
        if pct is not None:
            return f"{pct:+.1f}%"
        if self.dps_before is None or self.dps_after is None:
            return "-"
        return fmt_delta(self.dps_before, self.dps_after)

    @property
    def ehp_pct_str(self) -> str:
        pct = self.ehp_pct
        if pct is not None:
            return f"{pct:+.1f}%"
        if _is_pos_inf(self.ehp_before) and _is_pos_inf(self.ehp_after):
            return "0% (inf)"
        if self.ehp_before is None or self.ehp_after is None:
            return "-"
        return fmt_delta(self.ehp_before, self.ehp_after)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dps_key": self.dps_key,
            "dps_before": _json_safe_stat(self.dps_before),
            "dps_after": _json_safe_stat(self.dps_after),
            "dps_pct": self.dps_pct,
            "ehp_before": _json_safe_stat(self.ehp_before),
            "ehp_after": _json_safe_stat(self.ehp_after),
            "ehp_pct": self.ehp_pct,
            "life_before": _json_safe_stat(self.life_before),
            "life_after": _json_safe_stat(self.life_after),
            "life_delta": self.life_delta,
            "combined_pct": self.combined_pct,
            "warnings": list(self.warnings),
            "resolved_warnings": list(self.resolved_warnings),
        }


@dataclass
class Candidate:
    raw: dict[str, Any]
    item: Item
    item_text: str
    price_amount: float | None = None
    price_currency: str | None = None
    score: float = 0.0
    deltas: list[ModDelta] = field(default_factory=list)
    conversions: list[Conversion] = field(default_factory=list)
    conversion_warnings: list[str] = field(default_factory=list)
    url: str = ""
    listing_id: str | None = None
    weighted_sum: float | None = None
    pob: PobEval | None = None
    pob_error: str | None = None

    @property
    def display_name(self) -> str:
        return self.item.display_name

    @property
    def price_text(self) -> str:
        if self.price_amount is None or not self.price_currency:
            return "no price"
        if not math.isfinite(self.price_amount):
            return "no price"
        return f"{self.price_amount:g} {self.price_currency}"

    @property
    def gains(self) -> list[ModDelta]:
        return sorted((d for d in self.deltas if d.contribution > 0), key=lambda d: -d.contribution)

    @property
    def losses(self) -> list[ModDelta]:
        return sorted((d for d in self.deltas if d.contribution < 0), key=lambda d: d.contribution)


# ---------------------------------------------------------------------------
# Weights & modifier comparison
# ---------------------------------------------------------------------------


def parse_weights(values: Iterable[str] | None) -> dict[str, float]:
    """Parse ``category=value`` CLI overrides on top of :data:`DEFAULT_WEIGHTS`."""
    weights = dict(DEFAULT_WEIGHTS)
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"--weight needs category=value: {value}")
        category, raw = value.split("=", 1)
        category = category.strip().lower()
        if category not in weights:
            raise ValueError(f"Unknown weight category: {category}")
        parsed = float(raw)
        if not math.isfinite(parsed):
            raise ValueError(f"Weight for {category} must be finite: {raw}")
        weights[category] = parsed
    return weights


def preferred_templates(values: Iterable[str] | None) -> set[str]:
    """Normalize ``--want`` preferences into lowercase templates/substrings."""
    result: set[str] = set()
    for value in values or []:
        if not value or not str(value).strip():
            continue
        norm = normalize_template(str(value))
        if norm:
            result.add(norm)
    return result


def _matches_preferred(template: str, preferred: set[str]) -> bool:
    if not preferred:
        return False
    if template in preferred:
        return True
    return any(pref and pref in template for pref in preferred)


def compare_items(
    current: Item | None,
    candidate: Item,
    *,
    weights: dict[str, float] | None = None,
    preferred: set[str] | None = None,
) -> list[ModDelta]:
    """Compare normalized modifier templates between ``current`` and ``candidate``."""
    weights = weights or DEFAULT_WEIGHTS
    norm_preferred = {
        normalize_template(p)
        for p in (preferred or ())
        if p and str(p).strip()
    }
    norm_preferred.discard("")

    current_totals = mods_by_template(current)
    candidate_totals = mods_by_template(candidate)
    current_examples = {mod.template: mod for mod in (current.mods if current else [])}
    candidate_examples = {mod.template: mod for mod in candidate.mods}

    deltas: list[ModDelta] = []
    for template in sorted(set(current_totals) | set(candidate_totals)):
        before = current_totals.get(template, 0.0)
        after = candidate_totals.get(template, 0.0)
        delta = after - before
        if abs(delta) < 1e-9:
            continue
        sample = candidate_examples.get(template) or current_examples.get(template)
        category = sample.category if sample else "other"
        contribution = delta * weights.get(category, weights.get("other", 0.1))
        if _matches_preferred(template, norm_preferred):
            contribution *= PREFERRED_BONUS
        deltas.append(
            ModDelta(
                template=template,
                category=category,
                current=before,
                candidate=after,
                delta=delta,
                contribution=contribution,
                example=sample.raw if sample else template,
                kind=sample.kind if sample else "explicit",
            )
        )
    deltas.sort(key=lambda item: item.contribution, reverse=True)
    return deltas


def score_deltas(deltas: list[ModDelta]) -> float:
    """Sum weighted template delta contributions into a finite rounded score."""
    total = sum(d.contribution for d in deltas if math.isfinite(d.contribution))
    return round(total, 2)


def deltas_for_example(current: Item | None, candidate: Item) -> list[ModDelta]:
    """Small helper kept for tests and callers that only need the delta list."""
    return compare_items(current, candidate)


# ---------------------------------------------------------------------------
# PoB verification (single RPC batch + legacy `item_try` fallback)
# ---------------------------------------------------------------------------


def _pob_eval_from_snapshot(
    before: dict[str, Any],
    after: dict[str, Any],
    diff: dict[str, Any],
) -> PobEval:
    """Build an `inf`/`NaN`-safe :class:`PobEval` from `trade_eval_items` or `item_try` dicts."""
    before = before or {}
    after = after or {}
    diff = diff or {}
    dps_key = str(after.get("primaryDpsKey") or before.get("primaryDpsKey") or "CombinedDPS")
    return PobEval(
        dps_key=dps_key,
        dps_before=_json_safe_stat(before.get("primaryDps")),
        dps_after=_json_safe_stat(after.get("primaryDps")),
        ehp_before=_json_safe_stat(before.get("totalEhp")),
        ehp_after=_json_safe_stat(after.get("totalEhp")),
        life_before=_finite_float(before.get("life")),
        life_after=_finite_float(after.get("life")),
        warnings=tuple(str(item) for item in (diff.get("newWarnings") or [])[:8]),
        resolved_warnings=tuple(str(item) for item in (diff.get("resolvedWarnings") or [])[:8]),
    )


def _default_daemon_call(action: str, params: dict[str, Any]) -> dict[str, Any]:
    from .daemon import call_daemon

    return call_daemon(action, params)


def verify_candidate(
    candidate: Candidate,
    *,
    slot: str,
    target: str = "main",
    daemon_call: Callable[..., dict[str, Any]] | None = None,
) -> PobEval:
    """Evaluate a single candidate via ``item_try`` (what-if, never applied)."""
    rpc = daemon_call or _default_daemon_call
    payload = rpc(
        "item_try",
        {
            "target": target,
            "slot": slot,
            "raw": candidate.item_text,
            "tooltip_only": False,
            "apply": False,
        },
    )
    mutation = payload.get("mutation") or {}
    return _pob_eval_from_snapshot(
        mutation.get("before") or {},
        mutation.get("after") or {},
        mutation.get("diff") or {},
    )


def verify_candidates_batch(
    candidates: list[Candidate],
    *,
    slot: str,
    target: str = "main",
    limit: int = 5,
    daemon_call: Callable[..., dict[str, Any]] | None = None,
) -> None:
    """Verify up to ``limit`` candidates in ONE ``trade_eval_items`` daemon RPC.

    Falls back sequentially to ``item_try`` if an older running daemon responds with
    ``Unknown daemon action``.
    """
    if limit <= 0 or not candidates:
        return
    from .daemon import BootstrapMissingError

    rpc = daemon_call or _default_daemon_call
    subset = candidates[:limit]
    items_payload = [{"id": idx, "raw": candidate.item_text} for idx, candidate in enumerate(subset)]

    try:
        resp = rpc(
            "trade_eval_items",
            {
                "target": target,
                "slot": slot,
                "items": items_payload,
            },
        )
    except BootstrapMissingError:
        raise
    except Exception as exc:
        msg = str(exc)
        if "unknown daemon action" in msg.lower():
            for candidate in subset:
                try:
                    candidate.pob = verify_candidate(
                        candidate,
                        slot=slot,
                        target=target,
                        daemon_call=rpc,
                    )
                    candidate.pob_error = None
                except BootstrapMissingError:
                    raise
                except Exception as item_exc:
                    candidate.pob = None
                    candidate.pob_error = str(item_exc)
            return
        for candidate in subset:
            candidate.pob = None
            candidate.pob_error = msg
        return

    default_before = resp.get("before") or {}
    seen: set[int] = set()
    for pos, entry in enumerate(resp.get("results") or []):
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        idx = raw_id if isinstance(raw_id, int) else pos
        if not (0 <= idx < len(subset)):
            continue
        seen.add(idx)
        candidate = subset[idx]
        if entry.get("ok"):
            candidate.pob = _pob_eval_from_snapshot(
                entry.get("before") or default_before,
                entry.get("after") or {},
                entry.get("diff") or {},
            )
            candidate.pob_error = None
        else:
            candidate.pob = None
            candidate.pob_error = str(entry.get("error") or "PoB evaluation failed.")

    for idx, candidate in enumerate(subset):
        if idx not in seen:
            candidate.pob = None
            candidate.pob_error = "Missing PoB evaluation result."


# ---------------------------------------------------------------------------
# Fast in-place worker batch evaluation (`eval_trade_items`)
# ---------------------------------------------------------------------------

_TWO_HANDED_ITEM_TYPES = frozenset({
    "Bow",
    "Staff",
    "Warstaff",
    "Two Handed Sword",
    "Two Handed Axe",
    "Two Handed Mace",
    "Fishing Rod",
})

_TWO_HANDED_RAW_RE = re.compile(
    r"(?:item\s+class\s*:\s*(?:bows?|staves|warstaves|two\s+hand|fishing\s+rods?)"
    r"|^\s*(?:two\s+handed|warstaff|staff|bow|fishing\s+rod)\b)",
    re.IGNORECASE | re.MULTILINE,
)

_COMPLEX_TRADE_RAW_RE = re.compile(
    r"(?:grants\s+level\s+\d+|grants\s+[^\n]*\bskill\b|triggers?\s+level\s+\d+|has\s+\d+\s+abyssal\s+socket"
    r"|\bexplode\b"
    r"|cluster\s+jewel|timeless\s+jewel|thread\s+of\s+hope|impossible\s+escape"
    r"|unnatural\s+instinct|intuitive\s+leap|militant\s+faith|lethal\s+pride"
    r"|brutal\s+restraint|elegant\s+hubris|glorious\s+vanity"
    r"|赋予|触发|深渊插槽|星团珠宝|永恒珠宝|希望之线|不可能的逃脱|超自然本能|直觉之跃|爆炸)",
    re.IGNORECASE,
)

_WEAPON_SLOTS = frozenset({"Weapon 1", "Weapon 2", "Weapon 1 Swap", "Weapon 2 Swap"})
_PARTNER_WEAPON_SLOT: dict[str, str] = {
    "Weapon 1": "Weapon 2",
    "Weapon 2": "Weapon 1",
    "Weapon 1 Swap": "Weapon 2 Swap",
    "Weapon 2 Swap": "Weapon 1 Swap",
}


def _is_complex_trade_item(raw_text: str, slot_name: str | None) -> bool:
    """Return True if raw item text could mutate 2H/offhand slots, granted skill groups, abyssal sockets, or tree radius."""
    if _COMPLEX_TRADE_RAW_RE.search(raw_text):
        return True
    if slot_name in _WEAPON_SLOTS and _TWO_HANDED_RAW_RE.search(raw_text):
        return True
    return False


def eval_trade_items(
    worker: Any,
    *,
    slot_name: str | None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Read-only batch what-if evaluation of candidate items against a single build snapshot.

    For standard single-item slots (non-2H weapon swap, no granted skills or dynamic sockets,
    and existing ``prev_item_id`` known), uses fast in-place swap:
    ``add_item`` -> pipelined ``capture_digest`` -> batched ``equip_item(prev_id)`` + ``delete_item(new_id)``
    instead of full ``import_code`` per candidate, while always keeping ``import_code(snap_before)``
    as fallback for complex/2H items and as the final zero-residue safety net.
    """
    if not items:
        return {"slotName": slot_name, "before": None, "results": []}

    from .analyzer import resolve_slot_name
    from .snapshot import _call_batch, capture_digest, diff_digest

    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        items_res, export_res, skills_res = _call_batch(
            worker,
            [
                ("list_items", {}),
                ("export_code", {}),
                ("list_skills", {}),
            ],
        )
        resolved_slot = resolve_slot_name(worker, slot_name, items_res=items_res) if slot_name else None
        snap_before = export_res["code"]
        digest_before = capture_digest(worker)
        before_summary = {
            "primaryDpsKey": digest_before["primaryDpsKey"],
            "primaryDps": digest_before["primaryDps"],
            "totalEhp": digest_before["totalEhp"],
            "life": digest_before["life"],
            "points": digest_before.get("buildInfo", {}).get("points", {}),
        }

        slots = items_res.get("slots", []) if isinstance(items_res, dict) else []
        items_by_id = {it["id"]: it for it in (items_res.get("items", []) if isinstance(items_res, dict) else []) if "id" in it}
        prev_slot = next((s for s in slots if s.get("name") == resolved_slot), None) if resolved_slot else None
        raw_prev_id = prev_slot.get("selItemId") if isinstance(prev_slot, dict) else None
        prev_item_id = int(raw_prev_id) if raw_prev_id else None
        prev_item = items_by_id.get(prev_item_id) if prev_item_id else None

        prev_item_type = str(prev_item.get("type") or "") if prev_item else ""
        prev_is_2h = bool(prev_item_type in _TWO_HANDED_ITEM_TYPES)
        prev_is_jewel_socket = bool(prev_slot and prev_slot.get("nodeId") is not None)
        prev_has_granted_skill = any(
            bool(g.get("source")) and g.get("slot") in (None, "", resolved_slot)
            for g in (skills_res.get("groups", []) if isinstance(skills_res, dict) else [])
        )
        prev_has_abyssal = any(
            not s.get("inactive")
            and str(s.get("name") or "").startswith(f"{resolved_slot} Abyssal Socket")
            for s in slots
        ) if resolved_slot else False
        partner_slot_name = _PARTNER_WEAPON_SLOT.get(resolved_slot or "")
        partner_slot = (
            next((s for s in slots if s.get("name") == partner_slot_name), None)
            if partner_slot_name
            else None
        )
        partner_weapon_equipped = bool(partner_slot and partner_slot.get("selItemId"))

        slot_fast_swap_eligible = (
            bool(resolved_slot)
            and prev_item_id is not None
            and not prev_is_2h
            and not prev_is_jewel_socket
            and not prev_has_granted_skill
            and not prev_has_abyssal
        )

        results: list[dict[str, Any]] = []
        needs_full_restore = False
        any_mutation = False
        total_items = len(items)

        try:
            for pos, entry in enumerate(items):
                item_id = entry.get("id")
                raw_clean = str(entry.get("raw") or "").strip()
                if not raw_clean:
                    results.append({"id": item_id, "ok": False, "error": "Empty item text."})
                    continue

                if needs_full_restore:
                    worker.call("import_code", {"code": snap_before, "mode": "replace"})
                    needs_full_restore = False

                use_fast_swap = slot_fast_swap_eligible and not _is_complex_trade_item(raw_clean, resolved_slot)

                try:
                    add_params: dict[str, Any] = {"raw": raw_clean, "equip": True}
                    if resolved_slot:
                        add_params["slotName"] = resolved_slot
                    any_mutation = True
                    add_res = worker.call("add_item", add_params)
                    new_item_obj = add_res.get("item") if isinstance(add_res, dict) else None
                    new_item_id = new_item_obj.get("id") if isinstance(new_item_obj, dict) else None
                    new_item_type = str(new_item_obj.get("type") or "") if isinstance(new_item_obj, dict) else ""
                    if new_item_type in _TWO_HANDED_ITEM_TYPES:
                        use_fast_swap = False
                    if partner_weapon_equipped and new_item_type != prev_item_type:
                        use_fast_swap = False

                    digest_after = capture_digest(worker)
                    diff = diff_digest(digest_before, digest_after)
                    results.append(
                        {
                            "id": item_id,
                            "ok": True,
                            "before": before_summary,
                            "after": {
                                "primaryDpsKey": digest_after["primaryDpsKey"],
                                "primaryDps": digest_after["primaryDps"],
                                "totalEhp": digest_after["totalEhp"],
                                "life": digest_after["life"],
                                "points": digest_after.get("buildInfo", {}).get("points", {}),
                            },
                            "diff": diff,
                        }
                    )

                    has_more = pos < total_items - 1
                    if has_more:
                        if use_fast_swap and prev_item_id is not None and new_item_id is not None and int(new_item_id) != prev_item_id:
                            try:
                                _call_batch(
                                    worker,
                                    [
                                        ("equip_item", {"id": prev_item_id, "slotName": resolved_slot}),
                                        ("delete_item", {"id": int(new_item_id)}),
                                    ],
                                )
                            except Exception:
                                needs_full_restore = True
                        else:
                            needs_full_restore = True
                except Exception as exc:
                    needs_full_restore = True
                    results.append({"id": item_id, "ok": False, "error": str(exc)})
        finally:
            if any_mutation:
                worker.call("import_code", {"code": snap_before, "mode": "replace"})
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good

        return {
            "slotName": resolved_slot or slot_name,
            "before": before_summary,
            "results": results,
        }


# ---------------------------------------------------------------------------
# Sorting, Markdown report, and RFC-8259 JSON serialization
# ---------------------------------------------------------------------------


def sort_candidates(candidates: list[Candidate], sort: str = "score") -> list[Candidate]:
    """Sort candidates by ``score``, ``price``, ``sum``, or ``pob`` (always NaN-safe)."""
    if sort == "sum":
        return sorted(
            candidates,
            key=lambda c: (
                c.weighted_sum is None or not math.isfinite(c.weighted_sum),
                -_finite_sort_key(c.weighted_sum, 0.0),
                c.price_amount is None or not math.isfinite(c.price_amount),
                _finite_sort_key(c.price_amount, 0.0),
                -_finite_sort_key(c.score, 0.0),
            ),
        )
    if sort == "price":
        return sorted(
            candidates,
            key=lambda c: (
                c.price_amount is None or not math.isfinite(c.price_amount),
                _finite_sort_key(c.price_amount, 0.0),
                -_finite_sort_key(c.score, 0.0),
            ),
        )
    if sort == "pob":
        return sorted(
            candidates,
            key=lambda c: (
                c.pob is None,
                -_finite_sort_key(c.pob.combined_pct if c.pob is not None else None, 0.0),
                -_finite_sort_key(c.score, 0.0),
                c.price_amount is None or not math.isfinite(c.price_amount),
                _finite_sort_key(c.price_amount, 0.0),
            ),
        )
    return sorted(
        candidates,
        key=lambda c: (
            -_finite_sort_key(c.score, 0.0),
            c.price_amount is None or not math.isfinite(c.price_amount),
            _finite_sort_key(c.price_amount, 0.0),
        ),
    )


def _escape(value: str) -> str:
    return sanitize_text(value).replace("|", "\\|")


def _format_delta(delta: ModDelta) -> str:
    return _escape(delta.example)


def _build_crafted_min_line(text: str, values: list[dict[str, Any]]) -> str:
    """Resolve a bench recipe text to its safe minimum-roll ``{crafted}`` PoB line."""
    clean = _DASH_RE.sub("-", sanitize_text(text))
    if not clean:
        return ""

    def _pick_min(match: re.Match[str]) -> str:
        s1, s2 = match.group(1), match.group(2)
        return s1 if float(s1) <= float(s2) else s2

    resolved = _BENCH_RANGE_RE.sub(_pick_min, clean)
    if "#" in resolved and values:
        for val in values:
            low = val.get("min")
            if low is None:
                continue
            low_str = str(int(low)) if float(low).is_integer() else f"{float(low):g}"
            resolved = resolved.replace("#", low_str, 1)
    if not resolved.startswith("{crafted}"):
        resolved = f"{{crafted}}{resolved}"
    return resolved


def format_report(
    *,
    build_label: str,
    slot: str,
    current: Item | None,
    candidates: list[Candidate],
    league: str,
    status: str = "",
    budget: float | None = None,
    currency: str = "chaos",
    search_url: str = "",
    total: int = 0,
    fetched: int = 0,
    conversion_note: str = "",
    open_affixes: dict[str, bool] | None = None,
    bench_options: list[dict[str, Any]] | None = None,
) -> str:
    """Render a high-SNR GitHub-flavored Markdown report for trade upgrade candidates."""
    lines: list[str] = ["# Gear upgrade candidates (PoE1 official trade)", ""]
    lines.append(f"- Build: {sanitize_text(build_label) or 'unknown'}")
    lines.append(f"- Slot: {sanitize_text(slot) or 'unknown'}")
    lines.append(f"- Current item: {sanitize_text(current.display_name) if current else 'empty'}")
    lines.append(f"- League: {sanitize_text(league) or 'unknown'}")
    if status:
        lines.append(f"- Status: {sanitize_text(status)}")
    budget_str = f"{budget:g} {sanitize_text(currency)}" if (budget is not None and budget > 0) else "unlimited"
    lines.append(f"- Budget: {budget_str}")
    lines.append(f"- Results: {total} matching, {fetched} fetched")
    if search_url:
        lines.append(f"- Trade link: {search_url}")
    if conversion_note:
        lines.append(f"- Conversion: {sanitize_text(conversion_note)}")
    if open_affixes and any(open_affixes.values()):
        wanted = [key for key, enabled in open_affixes.items() if enabled]
        lines.append(
            "- Open affixes: "
            + ", ".join(wanted)
            + " (bench-craft fallback; the craft needs an open "
            + "/".join(wanted)
            + ")"
        )
    lines.append("")

    if not candidates:
        lines.append("No analysable candidates.")
        return "\n".join(lines)

    has_pob = any(candidate.pob is not None for candidate in candidates)
    has_sum = any(
        candidate.weighted_sum is not None and math.isfinite(candidate.weighted_sum)
        for candidate in candidates
    )
    headers = ["#", "Item", "Price"]
    if has_sum:
        headers.append("Sum")
    headers.append("Score")
    if has_pob:
        headers.extend(["PoB DPS", "PoB EHP"])
    headers.extend(["Gains", "Losses", "Conversion"])

    rows: list[list[Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        gains = "; ".join(_format_delta(delta) for delta in candidate.gains[:2]) or "-"
        losses = "; ".join(_format_delta(delta) for delta in candidate.losses[:2]) or "-"
        conversion = (
            ", ".join(f"{conv.element}->{conv.target}" for conv in candidate.conversions)
            or "-"
        )
        score_str = f"{_finite_float(candidate.score):g}"
        row_cells: list[Any] = [
            index,
            _escape(candidate.display_name),
            _escape(candidate.price_text),
        ]
        if has_sum:
            sum_str = (
                f"{candidate.weighted_sum:g}"
                if (candidate.weighted_sum is not None and math.isfinite(candidate.weighted_sum))
                else "-"
            )
            row_cells.append(sum_str)
        row_cells.append(score_str)
        if has_pob:
            dps = candidate.pob.dps_pct_str if candidate.pob is not None else "-"
            ehp = candidate.pob.ehp_pct_str if candidate.pob is not None else "-"
            row_cells.extend([_escape(dps), _escape(ehp)])
        row_cells.extend([gains, losses, _escape(conversion)])
        rows.append(row_cells)

    lines.append(render_table(headers, rows))
    lines.append("")

    if bench_options:
        lines.append(f"## Crafting bench options for {sanitize_text(slot) or 'this slot'}")
        for recipe in bench_options:
            affix = sanitize_text(recipe.get("affix") or "unknown")
            text = sanitize_text(recipe.get("text") or "")
            text_zh = sanitize_text(recipe.get("textZh") or "")
            values = recipe.get("values") or []
            rolls: list[str] = []
            for value in values:
                if not isinstance(value, dict):
                    continue
                low = value.get("min")
                high = value.get("max")
                if low is None and high is None:
                    continue
                if low is None:
                    rolls.append(f"{float(high):g}")
                elif high is None or low == high:
                    rolls.append(f"{float(low):g}")
                else:
                    rolls.append(f"{float(low):g}-{float(high):g}")
            label = text + (f" / {text_zh}" if text_zh and text_zh != text else "")
            cost = sanitize_text(recipe.get("cost") or "-")

            raw_crafted = recipe.get("craftedLinesMin") or recipe.get("crafted_lines_min") or []
            if isinstance(raw_crafted, str):
                raw_crafted = [raw_crafted]
            crafted_lines = [
                line if str(line).startswith("{crafted}") else f"{{crafted}}{line}"
                for line in raw_crafted
                if str(line).strip()
            ]
            if not crafted_lines and text:
                fallback_line = _build_crafted_min_line(text, values)
                if fallback_line:
                    crafted_lines = [fallback_line]
            crafted_part = f" -> `{' / '.join(sanitize_text(c) for c in crafted_lines)}`" if crafted_lines else ""

            lines.append(
                f"- [{affix}] {label} ({', '.join(rolls) or '-'}; {cost}){crafted_part}"
            )
        lines.append("")
        lines.append(
            "> Bench modifiers roll below high-tier natural mods and need the matching "
            "open prefix/suffix. Preview with the `{crafted}` minimum-roll floor line in PoB "
            "before buying the base, then craft and re-check."
        )
        lines.append("")

    for index, candidate in enumerate(candidates[:3], start=1):
        lines.append(f"## {index}. {sanitize_text(candidate.display_name)} - {sanitize_text(candidate.price_text)}")
        if candidate.url:
            lines.append(f"[Trade listing]({candidate.url})")
        lines.append("")
        if candidate.pob is not None:
            pob = candidate.pob
            lines.append(
                f"PoB: {pob.dps_key} {fmt_num(pob.dps_before)} -> {fmt_num(pob.dps_after)} "
                f"({pob.dps_pct_str}), EHP {fmt_num(pob.ehp_before)} -> {fmt_num(pob.ehp_after)} "
                f"({pob.ehp_pct_str}), life {pob.life_delta:+g}"
            )
            if pob.warnings:
                lines.append("PoB warnings: " + "; ".join(_escape(warning) for warning in pob.warnings))
            if pob.resolved_warnings:
                lines.append(
                    "PoB resolved warnings: "
                    + "; ".join(_escape(warning) for warning in pob.resolved_warnings)
                )
            lines.append("")
        elif candidate.pob_error:
            lines.append(f"PoB verification failed: {_escape(candidate.pob_error)}")
            lines.append("")

        if candidate.conversions:
            lines.append("Required in-game conversion:")
            for conversion in candidate.conversions:
                tier_parts: list[str] = []
                if conversion.tier:
                    tier_parts.append(f"tier {conversion.tier}")
                if conversion.tier_range:
                    tier_parts.append(f"range {conversion.tier_range}")
                tier_prefix = ("; ".join(tier_parts) + "; ") if tier_parts else ""
                lines.append(
                    f"- {conversion.template} -> {conversion.converted_template} "
                    f"(cost: {conversion.cost}; {tier_prefix}{conversion.patch} rules; same tier, "
                    "roll re-rolled inside the tier - the listed value is a best case)"
                )
            for warning in candidate.conversion_warnings:
                lines.append(f"- Warning: {sanitize_text(warning)}")
            lines.append("")
        elif candidate.conversion_warnings:
            lines.append("Conversion warnings:")
            for warning in candidate.conversion_warnings:
                lines.append(f"- {sanitize_text(warning)}")
            lines.append("")

        if candidate.gains:
            lines.append("Gains:")
            for delta in candidate.gains[:6]:
                lines.append(f"- {_format_delta(delta)} ({delta.current:g} -> {delta.candidate:g})")
            lines.append("")
        if candidate.losses:
            lines.append("Losses:")
            for delta in candidate.losses[:6]:
                lines.append(f"- {_format_delta(delta)} ({delta.current:g} -> {delta.candidate:g})")
            lines.append("")

    lines.append(
        "> Mod score is a weighted template delta, not PoB DPS. `-` means the candidate "
        "was not verified, not 0 upgrade. PoB text is rebuilt from the listing (and Harvest "
        "conversions re-roll within the same tier) - re-check in PoB before buying."
    )
    return "\n".join(lines)


def report_json(candidates: list[Candidate]) -> list[dict[str, Any]]:
    """Serialize candidates to strictly RFC-8259 compliant JSON dicts (zero NaN/Inf)."""
    return [
        {
            "name": candidate.display_name,
            "base_type": candidate.item.base_type,
            "rarity": candidate.item.rarity,
            "listing_id": candidate.listing_id,
            "price": {
                "amount": _json_safe_optional_float(candidate.price_amount),
                "currency": candidate.price_currency,
            },
            "weighted_sum": _json_safe_optional_float(candidate.weighted_sum),
            "score": _finite_float(candidate.score),
            "url": candidate.url,
            "conversions": [conversion.as_dict() for conversion in candidate.conversions],
            "conversion_warnings": list(candidate.conversion_warnings),
            "pob": candidate.pob.as_dict() if candidate.pob is not None else None,
            "pob_error": candidate.pob_error,
            "gains": [
                {
                    "mod": delta.example,
                    "template": delta.template,
                    "category": delta.category,
                    "current": _finite_float(delta.current),
                    "candidate": _finite_float(delta.candidate),
                    "delta": _finite_float(delta.delta),
                    "contribution": _finite_float(delta.contribution),
                }
                for delta in candidate.gains
            ],
            "losses": [
                {
                    "mod": delta.example,
                    "template": delta.template,
                    "category": delta.category,
                    "current": _finite_float(delta.current),
                    "candidate": _finite_float(delta.candidate),
                    "delta": _finite_float(delta.delta),
                    "contribution": _finite_float(delta.contribution),
                }
                for delta in candidate.losses
            ],
            "item_text": candidate.item_text,
        }
        for candidate in candidates
    ]
