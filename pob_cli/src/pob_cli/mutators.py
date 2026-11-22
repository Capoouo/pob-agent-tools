"""Build mutation operations (items, flasks, anoints, skills, passive tree, config, batch, sweep)."""

from __future__ import annotations

import inspect
import math
import re
import unicodedata
from typing import Any, Iterable

from .analyzer import resolve_skill_group_index, resolve_slot_name
from .bridge_client import BridgeWorker
from .errors import CrossGameHallucinationError, DaemonRequestError
from .formatters import _parse_numeric_or_none, fmt_num, strip_pob_escapes
from .guardrails import (
    GuardrailScope,
    check_input,
    check_input_item_raw,
    check_poe2_class_or_ascendancy,
    normalize_equipment_slot,
    validate_url,
)
from .snapshot import capture_digest, diff_digest, run_mutation

# ---------------------------------------------------------------------------
# Internal batching, text-folding, and per-batch tab-caching helpers
# ---------------------------------------------------------------------------

_APOSTROPHE_TRANS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u0060": "'",
        "\u00b4": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)
_WS_COLLAPSE_RE = re.compile(r"\s+")
_CASCADE_WARN_THRESHOLD = 3


def _is_batch_error(res: Any) -> bool:
    """Return True if a `call_batch(raise_on_error=False)` result represents an RPC error."""
    if not isinstance(res, dict):
        return True
    return res.get("ok") is False or bool(res.get("error")) or bool(res.get("_error"))


def _call_batch(
    worker: BridgeWorker,
    calls: list[tuple[str, dict[str, Any] | None]],
    *,
    timeout: float = 30.0,
    raise_on_error: bool = True,
) -> list[dict[str, Any]]:
    """Pipeline multiple RPCs via `worker.call_batch` when available, falling back to sequential `call`."""
    if not calls:
        return []
    batch_fn = getattr(worker, "call_batch", None)
    if callable(batch_fn):
        return batch_fn(calls, timeout=timeout, raise_on_error=raise_on_error)
    results: list[dict[str, Any]] = []
    for method, params in calls:
        if raise_on_error:
            results.append(worker.call(method, params or {}))
        else:
            try:
                results.append(worker.call(method, params or {}))
            except Exception as exc:  # noqa: BLE001
                results.append({"error": str(exc)})
    return results


def _fold_search_text(text: str | None) -> str:
    """Fold Unicode diacritics (NFKD), normalize apostrophes/dashes, lowercase, and collapse spaces."""
    if not text:
        return ""
    cleaned = strip_pob_escapes(str(text)).translate(_APOSTROPHE_TRANS)
    decomposed = unicodedata.normalize("NFKD", cleaned)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WS_COLLAPSE_RE.sub(" ", stripped).strip().lower()


def _fold_no_apos(text: str | None) -> str:
    return _fold_search_text(text).replace("'", "")


def _resolve_slot_from_items(items_res: dict[str, Any], slot_query: str) -> str:
    """Resolve slot query against an already-fetched `list_items` response (avoids duplicate RPCs)."""
    q = normalize_equipment_slot(slot_query) or slot_query.strip()
    slots = items_res.get("slots", [])

    m_jewel = re.match(r"^#?(\d+)$", q)
    if m_jewel:
        idx = int(m_jewel.group(1))
        for s in slots:
            if s.get("socketIndex") == idx:
                return s["name"]
        raise ValueError(f"Jewel socket #{idx} not found in build slots.")

    q_lower = q.lower()
    for s in slots:
        if (
            s.get("name", "").lower() == q_lower
            or (s.get("label") or "").lower() == q_lower
            or (s.get("labelZh") or "").lower() == q_lower
        ):
            return s["name"]

    for s in slots:
        if q_lower in s.get("name", "").lower() or q_lower in (s.get("labelZh") or "").lower():
            return s["name"]

    raise ValueError(f"Equipment slot '{slot_query}' not found.")


def _call_compat(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call `fn` forwarding only keyword arguments accepted by its signature."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(*args, **filtered)


def _normalize_keep_list(keep: Iterable[str] | str | None) -> list[str]:
    if keep is None:
        return []
    if isinstance(keep, str):
        return [x.strip() for x in keep.split(",") if x.strip()]
    out: list[str] = []
    for item in keep:
        if item is None:
            continue
        for part in str(item).split(","):
            s = part.strip()
            if s:
                out.append(s)
    return out


class _BatchCache:
    """Tab-level read cache scoped to a single `run_batch` / `run_sweep` execution."""

    def __init__(self, worker: BridgeWorker) -> None:
        self.worker = worker
        self.config: dict[str, Any] | None = None
        self.known_config_vars: dict[str, str] | None = None
        self.known_config_items: dict[str, dict[str, Any]] | None = None
        self.items: dict[str, Any] | None = None
        self.skills: dict[str, Any] | None = None
        self.tree: dict[str, Any] | None = None
        self.classes: dict[str, Any] | None = None
        self.specs: dict[str, Any] | None = None

    def get_config(self) -> dict[str, Any]:
        if self.config is None:
            self.config = self.worker.call("list_config", {})
            if self.known_config_items is None:
                self._index_config_vars(self.config)
        return self.config

    def _index_config_vars(self, cfg_res: dict[str, Any]) -> None:
        known: dict[str, str] = {}
        items: dict[str, dict[str, Any]] = {}
        for sec in cfg_res.get("sections", []):
            for item in sec.get("items", []):
                vname = item.get("var")
                if vname:
                    key = str(vname).lower()
                    known[key] = str(vname)
                    items[key] = item
        self.known_config_vars = known
        self.known_config_items = items

    def get_known_config_vars(self) -> dict[str, str]:
        if self.known_config_vars is None:
            self.get_config()
        return self.known_config_vars or {}

    def get_known_config_items(self) -> dict[str, dict[str, Any]]:
        if self.known_config_items is None:
            self.get_config()
        return self.known_config_items or {}

    def get_items(self) -> dict[str, Any]:
        if self.items is None:
            self.items = self.worker.call("list_items", {})
        return self.items

    def get_skills(self) -> dict[str, Any]:
        if self.skills is None:
            self.skills = self.worker.call("list_skills", {})
        return self.skills

    def get_tree(self) -> dict[str, Any]:
        if self.tree is None:
            self.tree = self.worker.call("get_tree_state", {})
        return self.tree

    def get_classes(self) -> dict[str, Any]:
        if self.classes is None:
            self.classes = self.worker.call("list_classes", {})
        return self.classes

    def get_specs(self) -> dict[str, Any]:
        if self.specs is None:
            self.specs = self.worker.call("list_specs", {})
        return self.specs

    def invalidate(self, *tabs: str) -> None:
        for tab in tabs:
            if tab == "config":
                self.config = None
            elif tab == "config_schema":
                self.config = None
                self.known_config_vars = None
                self.known_config_items = None
            elif tab == "items":
                self.items = None
            elif tab == "skills":
                self.skills = None
            elif tab == "tree":
                self.tree = None
            elif tab == "classes":
                self.classes = None
            elif tab == "specs":
                self.specs = None

    def invalidate_all(self) -> None:
        self.config = None
        self.known_config_vars = None
        self.known_config_items = None
        self.items = None
        self.skills = None
        self.tree = None
        self.classes = None
        self.specs = None


def _is_jewel_slot_name(slot_name: str | None) -> bool:
    if not slot_name:
        return False
    s = slot_name.strip().lower()
    return "jewel" in s or s.startswith("#") or s.isdigit()


# ---------------------------------------------------------------------------
# Items, Flasks, Anoints, Unequip
# ---------------------------------------------------------------------------


def try_item(
    worker: BridgeWorker,
    *,
    raw_text: str,
    slot_name: str | None = None,
    tooltip_only: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Evaluate or equip a pasted raw PoE item text.

    - Always runs PoB's native `item_tooltip{raw, slotName, compare=True}`.
    - Unless `tooltip_only=True`, also runs a snapshot `add_item{raw, equip=True}` to catch secondary
      effects (attribute shortages, uncapped resistances, lost item-granted skills) or persists when `apply=True`.
    - When `slot_name` is omitted and the item fits multiple slots (e.g. Ring 1 vs Ring 2, Weapon 1 vs Weapon 2),
      includes the comparison for all candidate slots in `tooltip`.
    """
    raw_clean = raw_text.strip()
    if not raw_clean:
        raise ValueError("Item raw text cannot be empty.")
    if slot_name:
        check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input_item_raw(raw_clean, expected_game="poe1")

    with worker.lock:
        resolved_slot = resolve_slot_name(worker, slot_name) if slot_name else None
        tip_params: dict[str, Any] = {"raw": raw_clean, "compare": True}
        if resolved_slot:
            tip_params["slotName"] = resolved_slot
        native_tip = worker.call("item_tooltip", tip_params)

        # Build multi-slot comparisons when slot_name is omitted and item fits multiple slots (P1-1)
        summary = native_tip.get("summary") or {}
        primary_slot = resolved_slot or native_tip.get("slotName") or summary.get("primarySlot") or ""
        item_type = str(summary.get("type") or "")

        if resolved_slot:
            candidate_slots = [resolved_slot]
        elif primary_slot in ("Ring 1", "Ring 2") or "Ring" in item_type:
            candidate_slots = ["Ring 1", "Ring 2"]
        elif primary_slot in ("Weapon 1", "Weapon 2"):
            candidate_slots = ["Weapon 1", "Weapon 2"]
        elif primary_slot.startswith("Flask") or "Flask" in item_type:
            candidate_slots = [f"Flask {i}" for i in range(1, 6)]
        elif "Jewel" in primary_slot or "Jewel" in item_type:
            items_res, tree_state = _call_batch(worker, [("list_items", {}), ("get_tree_state", {})])
            alloc_set = {int(x) for x in tree_state.get("allocatedNodes", [])}
            candidate_slots = [
                s["name"]
                for s in items_res.get("slots", [])
                if s.get("nodeId") and int(s["nodeId"]) in alloc_set
            ][:8]
            if not candidate_slots and primary_slot:
                candidate_slots = [primary_slot]
        else:
            candidate_slots = [primary_slot] if primary_slot else []

        native_slot_key = resolved_slot or native_tip.get("slotName")
        extra_slots = [c for c in candidate_slots if c != native_slot_key]
        extra_tips_by_slot: dict[str, dict[str, Any]] = {}
        if extra_slots:
            extra_calls = [
                ("item_tooltip", {"raw": raw_clean, "slotName": c_slot, "compare": True})
                for c_slot in extra_slots
            ]
            extra_results = _call_batch(worker, extra_calls, raise_on_error=False)
            for c_slot, c_res in zip(extra_slots, extra_results):
                if not _is_batch_error(c_res):
                    extra_tips_by_slot[c_slot] = c_res

        slot_tooltips: list[dict[str, Any]] = []
        for c_slot in candidate_slots:
            if c_slot == native_slot_key:
                c_tip = native_tip
            else:
                c_tip = extra_tips_by_slot.get(c_slot)
                if c_tip is None:
                    continue
            slot_tooltips.append(
                {
                    "slotName": c_slot,
                    "lines": c_tip.get("lines", []),
                    "summary": c_tip.get("summary"),
                }
            )

        if tooltip_only and not apply:
            return {
                "applied": False,
                "mode": "TOOLTIP-ONLY (fast preview)",
                "slotName": primary_slot or None,
                "tooltip": native_tip,
                "slotTooltips": slot_tooltips,
                "mutation": None,
            }

        def _do_equip(w: BridgeWorker) -> dict[str, Any]:
            add_params: dict[str, Any] = {"raw": raw_clean, "equip": True}
            if resolved_slot:
                add_params["slotName"] = resolved_slot
            res = w.call("add_item", add_params)
            return {
                "addedItem": res.get("item"),
                "slotName": resolved_slot or res.get("item", {}).get("primarySlot"),
            }

        mutation_res = run_mutation(worker, _do_equip, apply=apply)
        return {
            "applied": apply,
            "mode": mutation_res["mode"],
            "slotName": resolved_slot or mutation_res["detail"].get("slotName") or primary_slot,
            "tooltip": native_tip,
            "slotTooltips": slot_tooltips,
            "mutation": mutation_res,
        }


def unequip_item(
    worker: BridgeWorker,
    slot: str | None = None,
    *,
    slot_name: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Unequip whatever item is currently in `slot` (0-residue what-if or `--apply`)."""
    target_slot = (slot if slot is not None else slot_name or "").strip()
    if not target_slot:
        raise ValueError("Specify a slot name to unequip.")
    check_input(target_slot, scope=GuardrailScope.SLOT, expected_game="poe1")

    def _do_unequip(w: BridgeWorker) -> dict[str, Any]:
        items_res = w.call("list_items", {})
        resolved_slot = _resolve_slot_from_items(items_res, target_slot)
        w.call("unequip_slot", {"slotName": resolved_slot})
        return {"slotName": resolved_slot, "unequipped": True}

    return run_mutation(worker, _do_unequip, apply=apply)


def _parse_flask_slots(flask_index: int | str) -> list[str]:
    idx_str = str(flask_index).strip().lower()
    if idx_str == "all":
        return [f"Flask {i}" for i in range(1, 6)]
    m_flask = re.match(r"^(?:flask\s*)?(\d+)$", idx_str)
    if m_flask:
        idx = int(m_flask.group(1))
        if 1 <= idx <= 5:
            return [f"Flask {idx}"]
    raise ValueError("Flask selector must be 1..5, 'Flask 1'..'Flask 5', or 'all'.")


def set_flask_active(
    worker: BridgeWorker,
    *,
    flask_index: int | str,
    active: bool,
    apply: bool = False,
) -> dict[str, Any]:
    """Toggle a flask slot (`Flask 1`..`Flask 5` or `all`) active or inactive."""
    slot_names = _parse_flask_slots(flask_index)

    def _do_flask(w: BridgeWorker) -> dict[str, Any]:
        calls = [("set_slot_active", {"slotName": s_name, "active": active}) for s_name in slot_names]
        results = _call_batch(w, calls)
        toggled = [
            {"slotName": s_name, "active": res.get("active", active)}
            for s_name, res in zip(slot_names, results)
        ]
        label = "Flask 1..5 (all)" if len(slot_names) > 1 else slot_names[0]
        return {"slotName": label, "active": active, "slots": toggled}

    return run_mutation(worker, _do_flask, apply=apply)


def _apply_anoint_in_place(
    w: BridgeWorker,
    *,
    slot_name: str = "Amulet",
    notable_query: str,
    anoint_index: int = 1,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    if anoint_index < 1 or anoint_index > 4:
        raise ValueError("anoint_index must be between 1 and 4.")
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(notable_query, scope=GuardrailScope.NOTABLE_OR_OIL, expected_game="poe1")
    action_key = "anoint" if anoint_index == 1 else f"anoint{anoint_index}"

    items_res = cache.get_items() if cache is not None else w.call("list_items", {})
    resolved_slot = _resolve_slot_from_items(items_res, slot_name)
    slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
    if not slot_obj or not slot_obj.get("selItemId"):
        raise DaemonRequestError(f"Slot '{resolved_slot}' has no equipped item to anoint.")

    item_id = int(slot_obj["selItemId"])
    edit_state = w.call("item_edit_begin", {"id": item_id})
    try:
        actions = edit_state.get("actions", {})
        if not actions.get(action_key):
            raise DaemonRequestError(
                f"Item '{edit_state.get('name')}' in slot '{resolved_slot}' does not support '{action_key}' "
                f"(may be Corrupted, Mirrored, or not an anointable base)."
            )

        pop = w.call(
            "item_edit_popup",
            {"kind": "anoint", "action": "open", "slot": anoint_index},
        )
        notable_ctl = next(
            (c for c in pop.get("controls", []) if c.get("name") == "notableDB"),
            None,
        )
        if not notable_ctl:
            raise RuntimeError("Anoint popup did not expose 'notableDB' control.")

        options = notable_ctl.get("options", [])
        q = notable_query.strip().lower()
        q_stripped = q.removeprefix("allocate ").removeprefix("配置 ").strip()
        q_folded = _fold_search_text(q_stripped)

        matched_opt: dict[str, Any] | None = None
        for opt in options:
            lbl = strip_pob_escapes(opt.get("label") or "").lower().strip()
            lbl_zh = strip_pob_escapes(opt.get("labelZh") or "").lower().strip()
            lbl_s = lbl.removeprefix("allocate ").strip()
            lbl_zh_s = lbl_zh.removeprefix("配置 ").strip()
            if (
                q in (lbl, lbl_zh, lbl_s, lbl_zh_s)
                or q_stripped in (lbl, lbl_zh, lbl_s, lbl_zh_s)
                or (q_folded and q_folded in (_fold_search_text(lbl_s), _fold_search_text(lbl_zh_s)))
            ):
                matched_opt = opt
                break

        if not matched_opt:
            sub_matches = [
                opt
                for opt in options
                if q_stripped in strip_pob_escapes(opt.get("label") or "").lower()
                or q_stripped in strip_pob_escapes(opt.get("labelZh") or "").lower()
                or (q_folded and q_folded in _fold_search_text(opt.get("label") or ""))
            ]
            if len(sub_matches) == 1:
                matched_opt = sub_matches[0]
            elif len(sub_matches) > 1:
                samples = ", ".join(
                    f"{strip_pob_escapes(o.get('labelZh') or o.get('label'))}"
                    for o in sub_matches[:5]
                )
                raise ValueError(
                    f"Ambiguous notable query '{notable_query}' matched {len(sub_matches)} notables: {samples}"
                )

        if not matched_opt:
            raise ValueError(f"Notable '{notable_query}' not found in anointable notableDB options.")

        _call_batch(
            w,
            [
                (
                    "item_edit_popup",
                    {
                        "kind": "anoint",
                        "action": "pick",
                        "name": "notableDB",
                        "value": matched_opt["id"],
                    },
                ),
                (
                    "item_edit_popup",
                    {"kind": "anoint", "action": "apply", "button": "save"},
                ),
            ],
        )
        commit_res = w.call("item_edit_commit", {"equip": True})
        if cache is not None:
            cache.invalidate("items", "skills")
        return {
            "slotName": resolved_slot,
            "anointIndex": anoint_index,
            "notable": matched_opt,
            "item": commit_res.get("item"),
        }
    except Exception:
        try:
            w.call("item_edit_cancel", {})
        except Exception:
            pass
        raise


def anoint_item(
    worker: BridgeWorker,
    *,
    slot_name: str = "Amulet",
    notable_query: str,
    anoint_index: int = 1,
    apply: bool = False,
) -> dict[str, Any]:
    """Anoint an equipped item (default Amulet) using PoB's native `item_edit_*` popup workflow."""
    if anoint_index < 1 or anoint_index > 4:
        raise ValueError("anoint_index must be between 1 and 4.")
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(notable_query, scope=GuardrailScope.NOTABLE_OR_OIL, expected_game="poe1")

    def _do_anoint(w: BridgeWorker) -> dict[str, Any]:
        return _apply_anoint_in_place(
            w,
            slot_name=slot_name,
            notable_query=notable_query,
            anoint_index=anoint_index,
        )

    return run_mutation(worker, _do_anoint, apply=apply)


# ---------------------------------------------------------------------------
# Skills & Skill Groups
# ---------------------------------------------------------------------------


def _resolve_gem_in_group(group: dict[str, Any], gem_selector: str | int) -> dict[str, Any]:
    gems = group.get("gems", [])
    s = str(gem_selector).strip()
    target_gem = None
    if s.isdigit():
        idx = int(s)
        target_gem = next((gm for gm in gems if gm["index"] == idx), None)
    else:
        s_low = s.lower()
        s_fold = _fold_search_text(s)
        for gm in gems:
            names = [
                str(gm.get("nameSpec") or ""),
                str(gm.get("name") or ""),
                str(gm.get("nameZh") or ""),
            ]
            if any(s_low in n.lower() or (s_fold and s_fold in _fold_search_text(n)) for n in names):
                target_gem = gm
                break
    if not target_gem:
        raise ValueError(f"Gem '{gem_selector}' not found in skill group #{group.get('index')}.")
    return target_gem


def _gem_search_fallback_query(gem_name: str) -> str | None:
    """Strip common 'Support' / '(辅)' / '辅助' suffixes that PoB omits in its internal gem DB."""
    s = gem_name.strip()
    s_low = s.lower()
    if s_low.endswith(" support") and len(s) > len(" support"):
        return s[:-8].strip()
    for zh_suf in ("(辅)", "（辅）", "辅助"):
        if s.endswith(zh_suf) and len(s) > len(zh_suf):
            return s[: -len(zh_suf)].strip()
    return None


def _pick_best_gem_candidate(candidates: list[dict[str, Any]], gem_name: str) -> dict[str, Any] | None:
    if not candidates:
        return None
    q_low = gem_name.strip().lower()
    q_fold = _fold_search_text(gem_name)
    fb = _gem_search_fallback_query(gem_name)
    fb_low = fb.lower() if fb else ""
    for c in candidates:
        n_en = str(c.get("name") or "").lower()
        n_zh = str(c.get("nameZh") or "").lower()
        if (
            q_low in (n_en, n_zh)
            or (fb_low and fb_low in (n_en, n_zh))
            or (q_fold and q_fold == _fold_search_text(c.get("name")))
        ):
            return c
    return candidates[0]


def _resolve_gem_candidate(w: BridgeWorker, g_idx: int, gem_name: str) -> dict[str, Any] | None:
    search_res = w.call(
        "gem_search",
        {
            "group": g_idx,
            "query": gem_name.strip(),
            "byDps": False,
            "supportableOnly": False,
            "limit": 20,
        },
    )
    cand = _pick_best_gem_candidate(search_res.get("gems", []), gem_name)
    if cand is not None:
        return cand
    fb_query = _gem_search_fallback_query(gem_name)
    if fb_query:
        fb_res = w.call(
            "gem_search",
            {
                "group": g_idx,
                "query": fb_query,
                "byDps": False,
                "supportableOnly": False,
                "limit": 20,
            },
        )
        return _pick_best_gem_candidate(fb_res.get("gems", []), gem_name)
    return None


def _normalize_gem_spec(entry: str | dict[str, Any]) -> dict[str, Any]:
    """Normalize a gem spec (`dict` or `'Name[:level[:quality]]'` string) into a canonical dict."""
    if isinstance(entry, str):
        raw = entry.strip()
        if not raw:
            raise ValueError("Gem specification string cannot be empty.")
        level = 20
        quality = 20
        parts = raw.rsplit(":", 2)
        if len(parts) == 3 and parts[1].strip().isdigit() and parts[2].strip().isdigit():
            name = parts[0].strip()
            level = int(parts[1].strip())
            quality = int(parts[2].strip())
        else:
            parts2 = raw.rsplit(":", 1)
            if len(parts2) == 2 and parts2[1].strip().isdigit():
                name = parts2[0].strip()
                level = int(parts2[1].strip())
            else:
                name = raw
        if not name:
            raise ValueError(f"Invalid gem specification '{entry}'.")
        return {
            "name": name,
            "gemId": None,
            "level": level,
            "quality": quality,
            "enabled": True,
            "count": 1,
        }

    if isinstance(entry, dict):
        name = str(entry.get("name") or entry.get("nameSpec") or entry.get("gem") or "").strip()
        gem_id = entry.get("gemId")
        if not name and not gem_id:
            raise ValueError("Gem spec dict must include 'name', 'nameSpec', 'gem', or 'gemId'.")
        return {
            "name": name,
            "gemId": str(gem_id) if gem_id else None,
            "level": int(entry["level"]) if entry.get("level") is not None else 20,
            "quality": int(entry["quality"]) if entry.get("quality") is not None else 20,
            "enabled": bool(entry.get("enabled", True)),
            "count": int(entry["count"]) if entry.get("count") is not None else 1,
        }

    raise ValueError(f"Unsupported gem spec type: {type(entry).__name__}")


def _resolve_gem_specs_batch(
    w: BridgeWorker,
    g_idx: int,
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve `gemId`/`nameSpec` for a list of normalized gem specs in ONE batched `gem_search`."""
    search_indices: list[int] = []
    search_calls: list[tuple[str, dict[str, Any]]] = []
    for i, sp in enumerate(specs):
        if not sp.get("gemId") and sp.get("name"):
            search_indices.append(i)
            search_calls.append(
                (
                    "gem_search",
                    {
                        "group": g_idx,
                        "query": sp["name"],
                        "byDps": False,
                        "supportableOnly": False,
                        "limit": 20,
                    },
                )
            )

    resolved: list[dict[str, Any]] = [dict(sp) for sp in specs]
    if search_calls:
        search_results = _call_batch(w, search_calls, raise_on_error=False)
        retry_indices: list[int] = []
        retry_calls: list[tuple[str, dict[str, Any]]] = []
        for i, res in zip(search_indices, search_results):
            cand = None
            if not _is_batch_error(res):
                cand = _pick_best_gem_candidate(res.get("gems", []), specs[i]["name"])
            if cand:
                if cand.get("gemId"):
                    resolved[i]["gemId"] = cand["gemId"]
                if cand.get("name"):
                    resolved[i]["name"] = cand["name"]
                resolved[i]["resolvedCandidate"] = cand
            else:
                fb_q = _gem_search_fallback_query(specs[i]["name"])
                if fb_q:
                    retry_indices.append(i)
                    retry_calls.append(
                        (
                            "gem_search",
                            {
                                "group": g_idx,
                                "query": fb_q,
                                "byDps": False,
                                "supportableOnly": False,
                                "limit": 20,
                            },
                        )
                    )
        if retry_calls:
            retry_results = _call_batch(w, retry_calls, raise_on_error=False)
            for i, res in zip(retry_indices, retry_results):
                if not _is_batch_error(res):
                    cand = _pick_best_gem_candidate(res.get("gems", []), specs[i]["name"])
                    if cand:
                        if cand.get("gemId"):
                            resolved[i]["gemId"] = cand["gemId"]
                        if cand.get("name"):
                            resolved[i]["name"] = cand["name"]
                        resolved[i]["resolvedCandidate"] = cand
    return resolved


def _build_add_gem_params(g_idx: int, spec: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "group": g_idx,
        "level": int(spec.get("level", 20)),
        "quality": int(spec.get("quality", 20)),
        "enabled": bool(spec.get("enabled", True)),
        "count": int(spec.get("count", 1)),
    }
    if spec.get("gemId"):
        params["gemId"] = spec["gemId"]
    if spec.get("name"):
        params["nameSpec"] = spec["name"]
    return params


def _is_vaal_gem_entry(gem: dict[str, Any] | None, fallback_name: str = "") -> bool:
    """Return True if `gem` or `fallback_name` refers to a Vaal skill gem.

    In PoB (`Data/Gems.lua` & `CalcSetup.lua:1831`), Vaal gems map `grantedEffectList[1]`
    (`enableGlobal1`) to the VAAL burst skill and `grantedEffectList[2]` (`enableGlobal2`)
    to the BASE skill, whereas non-Vaal gems keep their active/aura skill on `enableGlobal1`.
    """
    g = gem if isinstance(gem, dict) else {}
    if g.get("support"):
        return False
    skill_id = str(g.get("skillId") or "").strip()
    if skill_id.startswith("Vaal") and skill_id != "Vaal":
        return True
    for raw in (
        g.get("name"),
        g.get("nameSpec"),
        g.get("nameZh"),
        fallback_name,
    ):
        if not raw:
            continue
        cleaned = strip_pob_escapes(str(raw)).strip()
        low = cleaned.lower()
        if low.startswith("vaal ") or " vaal " in low or "瓦尔" in cleaned:
            return True
    return False


def _add_gem_with_default_skill_flags(w: BridgeWorker, params: dict[str, Any]) -> dict[str, Any]:
    """Add a gem and, for Vaal gems only, default to base skill ON (`enableGlobal2=True`) and Vaal burst OFF (`enableGlobal1=False`)."""
    added = w.call("add_gem", params)
    gem = added.get("gem") if isinstance(added, dict) else None
    fallback_name = str(params.get("nameSpec") or "")
    if not _is_vaal_gem_entry(gem, fallback_name):
        return added
    gem_index = gem.get("index") if isinstance(gem, dict) else None
    if gem_index is None:
        raise RuntimeError("add_gem did not return the added gem index")
    updated = w.call(
        "set_gem",
        {
            "group": params["group"],
            "index": gem_index,
            "enableGlobal1": False,
            "enableGlobal2": True,
        },
    )
    if isinstance(added, dict):
        merged = dict(added)
        if isinstance(updated, dict) and "gem" in updated:
            merged["gem"] = updated["gem"]
        return merged
    return updated


def _apply_vaal_defaults_batch(
    w: BridgeWorker,
    g_idx: int,
    resolved_specs: list[dict[str, Any]],
    gem_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build `added_gems` records and batch `set_gem(enableGlobal1=False, enableGlobal2=True)` ONLY for Vaal gems."""
    added_gems: list[dict[str, Any]] = []
    vaal_fix_positions: list[int] = []
    vaal_fix_calls: list[tuple[str, dict[str, Any]]] = []
    for pos, (sp, res) in enumerate(zip(resolved_specs, gem_results)):
        gem_obj = res.get("gem") if isinstance(res, dict) else None
        if _is_vaal_gem_entry(gem_obj, str(sp.get("name") or "")):
            gem_idx = gem_obj.get("index") if isinstance(gem_obj, dict) else None
            if gem_idx is None:
                raise RuntimeError("add_gem did not return the added gem index")
            vaal_fix_positions.append(pos)
            vaal_fix_calls.append(
                (
                    "set_gem",
                    {
                        "group": g_idx,
                        "index": gem_idx,
                        "enableGlobal1": False,
                        "enableGlobal2": True,
                    },
                )
            )
        added_gems.append(
            {
                "name": sp.get("name"),
                "gemId": sp.get("gemId"),
                "level": sp.get("level"),
                "quality": sp.get("quality"),
                "enabled": sp.get("enabled"),
                "count": sp.get("count"),
                "gem": gem_obj,
            }
        )
    if vaal_fix_calls:
        fix_results = _call_batch(w, vaal_fix_calls)
        for pos, fix_res in zip(vaal_fix_positions, fix_results):
            if isinstance(fix_res, dict) and "gem" in fix_res:
                added_gems[pos]["gem"] = fix_res["gem"]
    return added_gems


def _apply_add_group_in_place(
    w: BridgeWorker,
    *,
    gems: list[str | dict[str, Any]] | None = None,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool = True,
    include_in_full_dps: bool | None = None,
    make_main: bool = False,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    resolved_slot: str | None = None
    if slot is not None and str(slot).strip():
        check_input(str(slot), scope=GuardrailScope.SLOT, expected_game="poe1")
        items_res = cache.get_items() if cache is not None else w.call("list_items", {})
        resolved_slot = _resolve_slot_from_items(items_res, str(slot))
    if gems:
        for g_entry in gems:
            g_name = g_entry if isinstance(g_entry, str) else str(g_entry.get("name") or g_entry.get("nameSpec") or g_entry.get("gem") or "")
            if g_name:
                check_input(g_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)

    add_params: dict[str, Any] = {}
    if label is not None:
        add_params["label"] = str(label)
    if resolved_slot:
        add_params["slot"] = resolved_slot

    add_res = w.call("add_group", add_params)
    g_idx = int(add_res["index"])

    group_updates: dict[str, Any] = {"index": g_idx}
    if enabled is False:
        group_updates["enabled"] = False
    if include_in_full_dps is not None:
        group_updates["includeInFullDPS"] = bool(include_in_full_dps)
    if len(group_updates) > 1:
        w.call("set_group", group_updates)

    added_gems: list[dict[str, Any]] = []
    if gems:
        norm_specs = [_normalize_gem_spec(g) for g in gems]
        resolved_specs = _resolve_gem_specs_batch(w, g_idx, norm_specs)
        add_calls = [("add_gem", _build_add_gem_params(g_idx, sp)) for sp in resolved_specs]
        gem_results = _call_batch(w, add_calls)
        added_gems = _apply_vaal_defaults_batch(w, g_idx, resolved_specs, gem_results)

    if make_main:
        w.call("set_build_field", {"field": "mainSocketGroup", "value": g_idx})

    if cache is not None:
        cache.invalidate("skills")

    return {
        "groupIndex": g_idx,
        "label": str(label) if label is not None else "",
        "slot": resolved_slot,
        "enabled": bool(enabled),
        "includeInFullDPS": include_in_full_dps,
        "makeMain": bool(make_main),
        "gemsAdded": added_gems,
    }


def add_skill_group(
    worker: BridgeWorker,
    *,
    gems: list[str | dict[str, Any]] | None = None,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool = True,
    include_in_full_dps: bool | None = None,
    make_main: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Create a new skill socket group with optional gems, slot, label, enabled, and FullDPS flag."""
    def _do_add_group(w: BridgeWorker) -> dict[str, Any]:
        return _apply_add_group_in_place(
            w,
            gems=gems,
            label=label,
            slot=slot,
            enabled=enabled,
            include_in_full_dps=include_in_full_dps,
            make_main=make_main,
        )

    return run_mutation(worker, _do_add_group, apply=apply)


def _guard_source_skill_group(grp: dict[str, Any], *, action: str) -> None:
    source = grp.get("source")
    if not source:
        return
    slot = grp.get("slot") or ""
    slot_hint = f"assigned to slot='{slot}'" if slot else "assigned to the granting item's slot"
    label = grp.get("displayLabel") or grp.get("label") or f"group #{grp.get('index')}"
    raise ValueError(
        f"Cannot {action} on granted skill group '{label}' (source='{source}'). "
        f"PoB deletes and recreates granted groups when gem #1 is removed, shifting group indices. "
        f"Place support gems in a regular skill group {slot_hint} instead."
    )


def _apply_replace_group_in_place(
    w: BridgeWorker,
    group_selector: str | int,
    gems: list[str | dict[str, Any]],
    *,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool | None = None,
    include_in_full_dps: bool | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    skills_res = cache.get_skills() if cache is not None else w.call("list_skills", {})
    g_idx = resolve_skill_group_index(skills_res, group_selector)
    grp = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
    if grp is None:
        raise ValueError(f"Skill group #{g_idx} not found in build.")
    _guard_source_skill_group(grp, action="replace gems")

    if slot is not None and str(slot).strip():
        check_input(str(slot), scope=GuardrailScope.SLOT, expected_game="poe1")
    norm_specs = [_normalize_gem_spec(g) for g in (gems or [])]
    for sp in norm_specs:
        if sp.get("name"):
            check_input(str(sp["name"]), scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    resolved_specs = _resolve_gem_specs_batch(w, g_idx, norm_specs)

    old_gems = list(grp.get("gems", []))
    if old_gems:
        # Delete in reverse order so 1-based indices stay valid even when pipelined in one batch
        del_calls = [
            ("delete_gem", {"group": g_idx, "index": int(gm["index"])})
            for gm in sorted(old_gems, key=lambda x: int(x["index"]), reverse=True)
        ]
        _call_batch(w, del_calls)

    group_updates: dict[str, Any] = {"index": g_idx}
    if label is not None:
        group_updates["label"] = str(label)
    if slot is not None:
        s_clean = str(slot).strip()
        if s_clean:
            items_res = cache.get_items() if cache is not None else w.call("list_items", {})
            group_updates["slot"] = _resolve_slot_from_items(items_res, s_clean)
        else:
            group_updates["slot"] = ""
    if enabled is not None:
        group_updates["enabled"] = bool(enabled)
    if include_in_full_dps is not None:
        group_updates["includeInFullDPS"] = bool(include_in_full_dps)
    if len(group_updates) > 1:
        w.call("set_group", group_updates)

    added_gems: list[dict[str, Any]] = []
    if resolved_specs:
        add_calls = [("add_gem", _build_add_gem_params(g_idx, sp)) for sp in resolved_specs]
        gem_results = _call_batch(w, add_calls)
        added_gems = _apply_vaal_defaults_batch(w, g_idx, resolved_specs, gem_results)

    if cache is not None:
        cache.invalidate("skills")

    return {
        "groupIndex": g_idx,
        "removedCount": len(old_gems),
        "groupUpdates": group_updates,
        "gemsAdded": added_gems,
    }


def replace_skill_group(
    worker: BridgeWorker,
    group: str | int = "main",
    gems: list[str | dict[str, Any]] | None = None,
    *,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool | None = None,
    include_in_full_dps: bool | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Atomically clear all existing gems in `group` and replace them with `gems` in one call."""
    gem_list = list(gems or [])

    def _do_replace(w: BridgeWorker) -> dict[str, Any]:
        return _apply_replace_group_in_place(
            w,
            group,
            gem_list,
            label=label,
            slot=slot,
            enabled=enabled,
            include_in_full_dps=include_in_full_dps,
        )

    return run_mutation(worker, _do_replace, apply=apply)


def modify_skill(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_selector: str | int | None = None,
    new_gem: str | None = None,
    level: int | None = None,
    quality: int | None = None,
    enabled: bool | None = None,
    count: int | None = None,
    include_in_full_dps: bool | None = None,
    group_enabled: bool | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Modify a skill group or a specific gem within a skill group."""
    if new_gem:
        check_input(new_gem, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)

    def _do_skill_set(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
        if group is None:
            raise ValueError(f"Skill group #{g_idx} not found in build.")

        group_updates: dict[str, Any] = {"index": g_idx}
        if include_in_full_dps is not None:
            group_updates["includeInFullDPS"] = include_in_full_dps
        if group_enabled is not None:
            group_updates["enabled"] = group_enabled
        if gem_selector is None and enabled is not None:
            group_updates["enabled"] = enabled

        if len(group_updates) > 1:
            w.call("set_group", group_updates)

        gem_result = None
        if gem_selector is not None:
            target_gem = _resolve_gem_in_group(group, gem_selector)
            gem_params: dict[str, Any] = {"group": g_idx, "index": target_gem["index"]}
            if new_gem:
                cand = _resolve_gem_candidate(w, g_idx, new_gem)
                if cand and cand.get("gemId"):
                    gem_params["gemId"] = cand["gemId"]
                    gem_params["nameSpec"] = cand.get("name") or new_gem.strip()
                else:
                    gem_params["nameSpec"] = new_gem.strip()
            if level is not None:
                gem_params["level"] = level
            if quality is not None:
                gem_params["quality"] = quality
            if enabled is not None:
                gem_params["enabled"] = enabled
            if count is not None:
                gem_params["count"] = count
            gem_result = w.call("set_gem", gem_params)

        return {"groupIndex": g_idx, "groupUpdates": group_updates, "gemResult": gem_result}

    return run_mutation(worker, _do_skill_set, apply=apply)


def add_skill_gem(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_name: str,
    level: int = 20,
    quality: int = 20,
    enabled: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    """Add a gem (resolved via `gem_search` if needed) to a skill group."""
    check_input(gem_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)

    def _do_add(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        chosen = _resolve_gem_candidate(w, g_idx, gem_name)

        add_params: dict[str, Any] = {
            "group": g_idx,
            "level": level,
            "quality": quality,
            "enabled": enabled,
        }
        if chosen and chosen.get("gemId"):
            add_params["gemId"] = chosen["gemId"]
            add_params["nameSpec"] = chosen.get("name") or gem_name.strip()
        else:
            add_params["nameSpec"] = gem_name.strip()

        res = _add_gem_with_default_skill_flags(w, add_params)
        return {"groupIndex": g_idx, "addedGem": res, "resolvedCandidate": chosen}

    return run_mutation(worker, _do_add, apply=apply)


def remove_skill_gem(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_selector: str | int,
    apply: bool = False,
) -> dict[str, Any]:
    """Remove a gem from a skill group by 1-based index or gem name."""
    def _do_rm(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
        if group is None:
            raise ValueError(f"Skill group #{g_idx} not found in build.")
        target_gem = _resolve_gem_in_group(group, gem_selector)
        if group.get("source") and int(target_gem.get("index", 0)) == 1:
            _guard_source_skill_group(group, action="remove gem #1")
        res = w.call("delete_gem", {"group": g_idx, "index": target_gem["index"]})
        return {"groupIndex": g_idx, "removedGem": target_gem, "result": res}

    return run_mutation(worker, _do_rm, apply=apply)


# ---------------------------------------------------------------------------
# Passive Tree (R12 + Cascade Guard)
# ---------------------------------------------------------------------------


def _resolve_node_token(
    worker: BridgeWorker,
    token: str,
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
) -> tuple[int, int | None]:
    """Resolve a node token (`12345`, `12345:67890` mastery, or node name) into `(node_id, effect_id)`."""
    t = token.strip()
    effect_id: int | None = None
    if ":" in t:
        left, right = t.split(":", 1)
        t = left.strip()
        if right.strip().isdigit():
            effect_id = int(right.strip())
        else:
            effect_query = right.strip().lower()
            effect_fold = _fold_search_text(right)
            nid, _ = _resolve_node_token(worker, t, nodes_db, dynamic_map)
            node_meta = nodes_db.get(str(nid)) or dynamic_map.get(str(nid)) or {}
            for eff in node_meta.get("masteryEffects", []):
                text = " ".join(str(s) for s in (eff.get("stats") or []) + (eff.get("statsZh") or []))
                if effect_query in text.lower() or (effect_fold and effect_fold in _fold_search_text(text)):
                    return nid, int(eff["effect"])
            raise ValueError(f"Mastery effect matching '{right}' not found on node {nid}.")

    if t.isdigit():
        nid = int(t)
        if str(nid) not in nodes_db and str(nid) not in dynamic_map:
            raise ValueError(f"Passive tree node ID {nid} does not exist.")
        return nid, effect_id

    t_low = t.lower()
    t_fold = _fold_no_apos(t)
    exact_matches: list[int] = []
    sub_matches: list[int] = []
    for sid, meta in {**nodes_db, **dynamic_map}.items():
        name_en = str(meta.get("name") or "").lower()
        name_zh = str(meta.get("nameZh") or "").lower()
        fold_en = _fold_no_apos(meta.get("name"))
        if t_low in (name_en, name_zh) or (t_fold and t_fold == fold_en):
            exact_matches.append(int(sid))
        elif t_low in name_en or t_low in name_zh or (t_fold and t_fold in fold_en):
            sub_matches.append(int(sid))

    candidates = exact_matches or sub_matches
    if not candidates:
        raise ValueError(f"No passive node found matching '{t}'.")
    if len(candidates) == 1:
        return candidates[0], effect_id

    # Prefer allocated or shortest path distance (pipelined in ONE batch!)
    sample_ids = candidates[:15]
    hovers = _call_batch(
        worker,
        [("node_hover", {"id": cid}) for cid in sample_ids],
        raise_on_error=False,
    )
    best_id = sample_ids[0]
    best_dist = 999999
    for cid, hover in zip(sample_ids, hovers):
        if _is_batch_error(hover):
            continue
        cost = hover.get("cost")
        dist = 0 if hover.get("allocated") else (int(cost) if cost is not None else 9999)
        if dist < best_dist:
            best_dist = dist
            best_id = cid
    return best_id, effect_id


def _apply_tree_changes_in_place(
    w: BridgeWorker,
    resolved_alloc: list[tuple[int, int | None]],
    resolved_dealloc: list[tuple[int, int | None]],
    *,
    initial_tree_state: dict[str, Any] | None = None,
    allow_cascade: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[int], int, str | None]:
    """Apply tree allocations/deallocations reusing `tree_click`'s returned `get_tree_state` payload.

    Enforces R12 (`allocatedNodes` checked before every click) AND cascade deallocation guard
    (`orphanedNodes > 3` raises ValueError when `allow_cascade=False`, allowing transactional rollback).
    """
    actions_taken: list[dict[str, Any]] = []
    cur_state = initial_tree_state if (initial_tree_state and "allocatedNodes" in initial_tree_state) else w.call("get_tree_state", {})

    initial_alloc = {int(x) for x in cur_state.get("allocatedNodes", [])}
    initial_used = int((cur_state.get("points") or {}).get("used") or 0)

    for nid, eff_id in resolved_alloc:
        alloc_set = {int(x) for x in cur_state.get("allocatedNodes", [])}
        if nid in alloc_set:
            if eff_id is not None:
                res = w.call("select_mastery", {"id": nid, "effect": eff_id})
                if isinstance(res, dict) and "allocatedNodes" in res:
                    cur_state = res
                else:
                    cur_state = w.call("get_tree_state", {})
                actions_taken.append({"action": "select_mastery", "id": nid, "effect": eff_id})
            else:
                actions_taken.append({"action": "skip_already_allocated", "id": nid})
            continue

        click_params: dict[str, Any] = {"id": nid}
        if eff_id is not None:
            click_params["effect"] = eff_id
        res = w.call("tree_click", click_params)
        if res.get("needsMastery"):
            effects = res.get("effects", [])
            opts_str = "; ".join(
                f"{e['effect']}: {' / '.join(e.get('statsZh') or e.get('stats') or [])}"
                for e in effects[:6]
            )
            raise ValueError(
                f"Node {nid} is a Mastery node and requires `<nodeId>:<effectId>`. Available effects: {opts_str}"
            )
        if res.get("needsConfirm"):
            raise ValueError(f"Node {nid} requires confirmation ({res.get('needsConfirm')}).")
        if isinstance(res, dict) and "allocatedNodes" in res:
            cur_state = res
        else:
            cur_state = w.call("get_tree_state", {})
        actions_taken.append({"action": "allocated", "id": nid, "effect": eff_id})

    for nid, _ in resolved_dealloc:
        alloc_set = {int(x) for x in cur_state.get("allocatedNodes", [])}
        if nid not in alloc_set:
            actions_taken.append({"action": "skip_already_unallocated", "id": nid})
            continue
        res = w.call("tree_click", {"id": nid})
        if res.get("needsConfirm"):
            raise ValueError(f"Node {nid} deallocation requires confirmation ({res.get('needsConfirm')}).")
        if isinstance(res, dict) and "allocatedNodes" in res:
            cur_state = res
        else:
            cur_state = w.call("get_tree_state", {})
        actions_taken.append({"action": "deallocated", "id": nid})

    final_alloc = {int(x) for x in cur_state.get("allocatedNodes", [])}
    requested_dealloc_ids = {nid for nid, _ in resolved_dealloc}
    orphaned_nodes = sorted((initial_alloc - final_alloc) - requested_dealloc_ids)

    final_points = cur_state.get("points") or {}
    if "used" in final_points and "used" in (initial_tree_state.get("points") or {} if initial_tree_state else {}):
        points_delta = int(final_points.get("used") or 0) - initial_used
    elif "used" in final_points:
        points_delta = int(final_points.get("used") or 0) - initial_used
    else:
        points_delta = len(final_alloc) - len(initial_alloc)

    cascade_warning: str | None = None
    if len(orphaned_nodes) > _CASCADE_WARN_THRESHOLD:
        cascade_warning = (
            f"Cascade deallocation dropped {len(orphaned_nodes)} additional orphaned passive nodes "
            f"beyond the requested deallocate set (pointsDelta={points_delta})."
        )
        if not allow_cascade:
            sample = orphaned_nodes[:10]
            raise ValueError(
                f"Cascade deallocation blocked: {len(orphaned_nodes)} orphaned passive nodes would be lost "
                f"(exceeds threshold {_CASCADE_WARN_THRESHOLD}; sample IDs: {sample}). "
                "Re-run with allow_cascade=True / --allow-cascade if intentional."
            )

    return actions_taken, cur_state, orphaned_nodes, points_delta, cascade_warning


def change_tree(
    worker: BridgeWorker,
    *,
    allocate: list[str] | None = None,
    deallocate: list[str] | None = None,
    apply: bool = False,
    allow_cascade: bool = True,
) -> dict[str, Any]:
    """Allocate and/or deallocate passive tree nodes.

    Follows R12:
    - Reuses `tree_click`'s returned `get_tree_state` payload (`allocatedNodes` checked before every click)
      to avoid redundant `get_tree_state` RPCs.
    - Tracks `orphanedNodes` and `pointsDelta`, attaches `cascadeWarning` when >3 extra nodes drop,
      and rolls back with an explicit error when `allow_cascade=False` and `len(orphanedNodes) > 3`.
    """
    alloc_tokens = [x for x in (allocate or []) if str(x).strip()]
    dealloc_tokens = [x for x in (deallocate or []) if str(x).strip()]
    if not alloc_tokens and not dealloc_tokens:
        raise ValueError("Specify at least one node in --allocate or --deallocate.")

    with worker.lock:
        tree_state = worker.call("get_tree_state", {})
        nodes_db = worker.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
        dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}

        resolved_alloc = [_resolve_node_token(worker, str(tok), nodes_db, dynamic_map) for tok in alloc_tokens]
        resolved_dealloc = [_resolve_node_token(worker, str(tok), nodes_db, dynamic_map) for tok in dealloc_tokens]

        # Capture fast node_info + node_hover preview when operating on a single node (pipelined!)
        single_preview = None
        if len(resolved_alloc) + len(resolved_dealloc) == 1:
            single_nid = (resolved_alloc or resolved_dealloc)[0][0]
            try:
                n_info, n_hover = _call_batch(
                    worker,
                    [
                        ("node_info", {"id": single_nid, "diff": True}),
                        ("node_hover", {"id": single_nid}),
                    ],
                )
                single_preview = {"nodeInfo": n_info, "nodeHover": n_hover}
            except Exception:
                pass

        def _do_tree_changes(w: BridgeWorker) -> dict[str, Any]:
            actions_taken, final_state, orphaned_nodes, points_delta, cascade_warning = _apply_tree_changes_in_place(
                w,
                resolved_alloc,
                resolved_dealloc,
                initial_tree_state=tree_state,
                allow_cascade=allow_cascade,
            )
            detail: dict[str, Any] = {
                "actions": actions_taken,
                "points": final_state.get("points"),
                "pointsDelta": points_delta,
                "orphanedNodes": orphaned_nodes,
                "singlePreview": single_preview,
            }
            if cascade_warning:
                detail["cascadeWarning"] = cascade_warning
            return detail

        res = run_mutation(worker, _do_tree_changes, apply=apply)
        detail = res.get("detail") or {}
        res["orphanedNodes"] = detail.get("orphanedNodes", [])
        res["pointsDelta"] = detail.get("pointsDelta", 0)
        if detail.get("cascadeWarning"):
            res["cascadeWarning"] = detail["cascadeWarning"]
        return res


# ---------------------------------------------------------------------------
# Raw Item Mod Editing
# ---------------------------------------------------------------------------

_MOD_TAG_RE = re.compile(r"\{[^}]*\}")
_IMPLICITS_HDR_RE = re.compile(r"^Implicits:\s*(\d+)\s*$", re.IGNORECASE)
_ITEM_TRAILER_RE = re.compile(
    r"^(?:Split|Mirrored|Fractured Item|Corrupted|Unidentified|Synthesised Item|Foil Unique(?:\s*\(.*\))?)$",
    re.IGNORECASE,
)
_ITEM_HEADER_KEY_RE = re.compile(
    r"^(?:Rarity|Armour|Evasion|Energy Shield|Ward|ArmourBasePercentile|EvasionBasePercentile|"
    r"EnergyShieldBasePercentile|WardBasePercentile|Intangibility|Unique ID|League|Unreleased|"
    r"Crafted|Prefix|Suffix|Catalyst|CatalystQuality|Cluster Jewel Skill|Cluster Jewel Node Count|"
    r"Talisman Tier|Item Level|Memory Strands|Version|Selected Version|Variant|Selected Variant|"
    r"Selected Variant Group|Has Alt Variant.*|Selected Alt Variant.*|Allow Duplicate Variants|"
    r"Quality|Sockets|LevelReq|Radius|Limited to|Requires Class|Shaper Item|Elder Item|"
    r"Crusader Item|Redeemer Item|Hunter Item|Warlord Item|Searing Exarch Item|Eater of Worlds Item|"
    r"Synthesised Item)\b",
    re.IGNORECASE,
)


def _mod_line_matches(line: str, pattern: str) -> bool:
    pat = pattern.strip()
    if not pat:
        return False
    clean = strip_pob_escapes(_MOD_TAG_RE.sub("", line)).strip()
    pat_low = pat.lower()
    if pat_low in clean.lower() or pat_low in line.lower():
        return True
    try:
        if re.search(pat, clean, re.IGNORECASE) or re.search(pat, line, re.IGNORECASE):
            return True
    except re.error:
        return False
    return False


def _edit_raw_item_mods(
    raw_text: str,
    *,
    replace_mods: list[tuple[str, str]] | None = None,
    add_mods: list[str] | None = None,
    remove_mods: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Safely edit only the mod lines of a PoB item raw string without mangling headers or trailers."""
    rep_list = list(replace_mods or [])
    add_list = [m.strip() for m in (add_mods or []) if str(m).strip()]
    rem_list = [p.strip() for p in (remove_mods or []) if str(p).strip()]
    if not rep_list and not add_list and not rem_list:
        raise ValueError("Specify at least one of replace_mods, add_mods, or remove_mods.")

    lines = raw_text.splitlines()
    has_implicits_hdr = False
    header_lines: list[str] = []
    implicit_lines: list[str] = []
    after_implicit: list[str] = []

    imp_idx: int | None = None
    imp_count = 0
    for idx, line in enumerate(lines):
        m = _IMPLICITS_HDR_RE.match(line.strip())
        if m:
            imp_idx = idx
            imp_count = int(m.group(1))
            break

    if imp_idx is not None:
        has_implicits_hdr = True
        header_lines = lines[:imp_idx]
        implicit_lines = lines[imp_idx + 1 : imp_idx + 1 + imp_count]
        after_implicit = lines[imp_idx + 1 + imp_count :]
    else:
        sep_indices = [i for i, l in enumerate(lines) if l.strip() == "--------"]
        if sep_indices:
            last_sep = sep_indices[-1]
            header_lines = lines[: last_sep + 1]
            after_implicit = lines[last_sep + 1 :]
        else:
            split_at = 0
            if lines and lines[0].strip().lower().startswith("rarity:"):
                rarity_val = lines[0].split(":", 1)[1].strip().upper()
                split_at = 3 if rarity_val in ("RARE", "UNIQUE") else 2
            while split_at < len(lines) and _ITEM_HEADER_KEY_RE.match(lines[split_at].strip()):
                split_at += 1
            header_lines = lines[:split_at]
            after_implicit = lines[split_at:]

    trailer_rev: list[str] = []
    explicit_lines = list(after_implicit)
    while explicit_lines:
        tail = explicit_lines[-1].strip()
        if not tail or _ITEM_TRAILER_RE.match(tail):
            trailer_rev.append(explicit_lines.pop())
        else:
            break
    trailer_lines = list(reversed(trailer_rev))

    changes: list[dict[str, Any]] = []

    for pattern, new_line in rep_list:
        pat = str(pattern).strip()
        repl = str(new_line).strip()
        if not pat:
            raise ValueError("replace_mods pattern cannot be empty.")
        matched = False
        for i, line in enumerate(explicit_lines):
            if _mod_line_matches(line, pat):
                changes.append({"action": "replace", "section": "explicit", "before": line, "after": repl})
                explicit_lines[i] = repl
                matched = True
        if not matched:
            for i, line in enumerate(implicit_lines):
                if _mod_line_matches(line, pat):
                    changes.append({"action": "replace", "section": "implicit", "before": line, "after": repl})
                    implicit_lines[i] = repl
                    matched = True
        if not matched:
            raise ValueError(f"Pattern '{pat}' did not match any mod line on the item.")

    for pat in rem_list:
        kept_explicit: list[str] = []
        removed_any = False
        for line in explicit_lines:
            if _mod_line_matches(line, pat):
                changes.append({"action": "remove", "section": "explicit", "before": line})
                removed_any = True
            else:
                kept_explicit.append(line)
        explicit_lines = kept_explicit

        if not removed_any:
            kept_implicit: list[str] = []
            for line in implicit_lines:
                if _mod_line_matches(line, pat):
                    changes.append({"action": "remove", "section": "implicit", "before": line})
                    removed_any = True
                else:
                    kept_implicit.append(line)
            implicit_lines = kept_implicit

        if not removed_any:
            raise ValueError(f"Pattern '{pat}' did not match any mod line to remove.")

    for new_mod in add_list:
        for sub_line in new_mod.splitlines():
            s = sub_line.strip()
            if s:
                explicit_lines.append(s)
                changes.append({"action": "add", "section": "explicit", "after": s})

    out_lines: list[str] = list(header_lines)
    if has_implicits_hdr:
        out_lines.append(f"Implicits: {len(implicit_lines)}")
    out_lines.extend(implicit_lines)
    out_lines.extend(explicit_lines)
    out_lines.extend(trailer_lines)

    return "\n".join(out_lines), changes


def _apply_edit_item_mods_in_place(
    w: BridgeWorker,
    *,
    slot_name: str,
    replace_mods: list[tuple[str, str]] | None = None,
    add_mods: list[str] | None = None,
    remove_mods: list[str] | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    for _, new_line in replace_mods or []:
        check_input(str(new_line), scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    for new_mod in add_mods or []:
        check_input(str(new_mod), scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    items_res = cache.get_items() if cache is not None else w.call("list_items", {})
    resolved_slot = _resolve_slot_from_items(items_res, slot_name)
    slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
    if not slot_obj or not slot_obj.get("selItemId"):
        raise DaemonRequestError(f"Slot '{resolved_slot}' has no equipped item to edit.")

    item_id = int(slot_obj["selItemId"])
    before_raw = w.call("item_raw", {"id": item_id}).get("raw") or ""
    after_raw, changes = _edit_raw_item_mods(
        before_raw,
        replace_mods=replace_mods,
        add_mods=add_mods,
        remove_mods=remove_mods,
    )
    res = w.call("add_item", {"raw": after_raw, "slotName": resolved_slot, "equip": True})
    if cache is not None:
        cache.invalidate("items", "skills")
        if _is_jewel_slot_name(resolved_slot) or slot_obj.get("nodeId"):
            cache.invalidate("tree")
    return {
        "slotName": resolved_slot,
        "item": res.get("item"),
        "beforeRaw": before_raw,
        "afterRaw": after_raw,
        "changes": changes,
    }


def edit_item_mods(
    worker: BridgeWorker,
    *,
    slot_name: str,
    replace_mods: list[tuple[str, str]] | None = None,
    add_mods: list[str] | None = None,
    remove_mods: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Edit mod lines on an equipped item in place (replace/add/remove) with 0-residue what-if or --apply."""
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    for _, new_line in replace_mods or []:
        check_input(str(new_line), scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    for new_mod in add_mods or []:
        check_input(str(new_mod), scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    with worker.lock:
        items_res = worker.call("list_items", {})
        resolved_slot = _resolve_slot_from_items(items_res, slot_name)
        slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
        if not slot_obj or not slot_obj.get("selItemId"):
            raise DaemonRequestError(f"Slot '{resolved_slot}' has no equipped item to edit.")

        item_id = int(slot_obj["selItemId"])
        before_raw = worker.call("item_raw", {"id": item_id}).get("raw") or ""
        after_raw, changes = _edit_raw_item_mods(
            before_raw,
            replace_mods=replace_mods,
            add_mods=add_mods,
            remove_mods=remove_mods,
        )

        def _do_edit(w: BridgeWorker) -> dict[str, Any]:
            res = w.call("add_item", {"raw": after_raw, "slotName": resolved_slot, "equip": True})
            return {
                "slotName": resolved_slot,
                "item": res.get("item"),
                "changes": changes,
            }

        mutation_res = run_mutation(worker, _do_edit, apply=apply)
        return {
            "applied": apply,
            "mode": mutation_res["mode"],
            "slotName": resolved_slot,
            "beforeRaw": before_raw,
            "afterRaw": after_raw,
            "changes": changes,
            "mutation": mutation_res,
        }


# ---------------------------------------------------------------------------
# Item DB Search & Equip
# ---------------------------------------------------------------------------


def _search_item_db_items(
    worker: BridgeWorker,
    *,
    query: str,
    db_type: str = "UNIQUE",
    limit: int = 15,
) -> tuple[str, list[dict[str, Any]], int]:
    """Search PoB's Unique/Rare DB with NFKD diacritic/apostrophe folding fallback and exact-title ranking."""
    q = (query or "").strip()
    if not q:
        raise ValueError("Item DB search query cannot be empty.")
    check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    kind = "rare" if str(db_type or "UNIQUE").strip().lower() == "rare" else "unique"
    limit = max(1, min(200, int(limit)))

    res = worker.call(
        "item_db",
        {"kind": kind, "query": q, "size": min(200, max(limit, 50)), "page": 1},
        timeout=30.0,
    )
    items = list(res.get("items", []))
    total_hits = int(res.get("total", len(items)))

    # Fallback client-side scan with NFKD diacritic & apostrophe folding (e.g. Doppelganger -> Doppelgänger)
    if not items:
        q_low = q.lower()
        q_fold = _fold_search_text(q)
        q_no_apos = _fold_no_apos(q)
        fallback_hits: list[dict[str, Any]] = []
        page = 1
        while page <= 12:
            page_res = worker.call("item_db", {"kind": kind, "query": "", "size": 200, "page": page}, timeout=30.0)
            page_items = page_res.get("items", [])
            if not page_items:
                break
            for it in page_items:
                hay_raw = " ".join(
                    str(it.get(k) or "")
                    for k in ("title", "titleZh", "name", "nameZh", "baseName", "baseNameZh", "type", "raw")
                )
                hay_low = hay_raw.lower()
                if q_low in hay_low:
                    fallback_hits.append(it)
                    continue
                hay_fold = _fold_search_text(hay_raw)
                if (q_fold and q_fold in hay_fold) or (q_no_apos and q_no_apos in hay_fold.replace("'", "")):
                    fallback_hits.append(it)
            if len(page_items) < 200 or len(fallback_hits) >= max(limit, 50):
                break
            page += 1
        items = fallback_hits
        total_hits = len(fallback_hits)

    # Rank exact EN/ZH/folded title/name matches first
    q_low = q.lower()
    q_fold = _fold_no_apos(q)

    def _rank_db_item(it: dict[str, Any]) -> tuple[int, str]:
        titles = [
            strip_pob_escapes(str(it.get(k) or "")).strip().lower()
            for k in ("title", "titleZh", "name", "nameZh")
            if it.get(k)
        ]
        folded_titles = [_fold_no_apos(t) for t in titles]
        if any(t == q_low for t in titles) or (q_fold and any(ft == q_fold for ft in folded_titles)):
            return (0, titles[0] if titles else "")
        if any(t.startswith(q_low) for t in titles) or (q_fold and any(ft.startswith(q_fold) for ft in folded_titles)):
            return (1, titles[0] if titles else "")
        if any(q_low in t for t in titles) or (q_fold and any(q_fold in ft for ft in folded_titles)):
            return (2, titles[0] if titles else "")
        return (3, titles[0] if titles else "")

    items.sort(key=_rank_db_item)
    return kind, items, total_hits


def search_and_try_item_db(
    worker: BridgeWorker,
    *,
    query: str,
    db_type: str = "UNIQUE",
    slot_name: str | None = None,
    item_index: int | None = None,
    equip: bool = False,
    apply: bool = False,
    limit: int = 15,
    with_tooltips: bool = False,
    with_raw: bool = False,
) -> dict[str, Any]:
    """Search PoB's built-in Unique/Rare item DB (EN or ZH) and optionally try/equip a match.

    Fast by default: skips `item_tooltip` loops unless `with_tooltips=True` (pipelined via `call_batch`)
    and strips bulky `raw` from listing entries unless `with_raw=True`.
    """
    limit = max(1, min(200, int(limit)))
    if slot_name:
        check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    if query:
        check_input(query, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    with worker.lock:
        kind, items, total_hits = _search_item_db_items(
            worker,
            query=query,
            db_type=db_type,
            limit=limit,
        )

        # Build indexed listing entries; strip bulky `raw` unless with_raw=True
        raw_by_idx: dict[int, str] = {}
        indexed_items: list[dict[str, Any]] = []
        for idx, it in enumerate(items[:limit], start=1):
            raw_txt = str(it.get("raw") or "")
            raw_by_idx[idx] = raw_txt
            entry = {k: v for k, v in it.items() if (with_raw or k != "raw")}
            entry["index"] = idx
            indexed_items.append(entry)

        if not equip and not apply:
            if with_tooltips:
                tip_targets = [
                    (entry, raw_by_idx.get(entry["index"], ""))
                    for entry in indexed_items[: min(limit, 10)]
                    if raw_by_idx.get(entry["index"], "")
                ]
                if tip_targets:
                    tip_calls = [
                        ("item_tooltip", {"raw": raw_txt, "compare": False})
                        for _, raw_txt in tip_targets
                    ]
                    tip_results = _call_batch(worker, tip_calls, raise_on_error=False)
                    for (entry, _), tip in zip(tip_targets, tip_results):
                        if not _is_batch_error(tip):
                            entry["tooltipLines"] = tip.get("lines", [])
            return {
                "query": query,
                "dbType": kind.upper(),
                "total": total_hits,
                "matches": indexed_items,
                "equipped": False,
                "applied": False,
                "selectedItem": None,
                "tooltip": None,
                "mutation": None,
            }

        if not items:
            raise ValueError(f"No {kind} database items matched query '{query}'.")

        if item_index is not None:
            if item_index < 1 or item_index > len(items):
                raise ValueError(f"item_index {item_index} out of range (1..{len(items)}).")
            chosen = {**items[item_index - 1], "index": item_index}
        else:
            chosen = {**items[0], "index": 1}

        chosen_raw = str(chosen.get("raw") or "").strip()
        if not chosen_raw:
            raise RuntimeError(f"Selected DB item '{chosen.get('title') or chosen.get('name')}' has no raw text.")

        resolved_slot = resolve_slot_name(worker, slot_name) if slot_name else None
        tip_params: dict[str, Any] = {"raw": chosen_raw, "compare": True}
        if resolved_slot:
            tip_params["slotName"] = resolved_slot
        native_tip = worker.call("item_tooltip", tip_params)

        def _do_equip_db(w: BridgeWorker) -> dict[str, Any]:
            add_params: dict[str, Any] = {"raw": chosen_raw, "equip": True}
            if resolved_slot:
                add_params["slotName"] = resolved_slot
            add_res = w.call("add_item", add_params)
            return {
                "addedItem": add_res.get("item"),
                "slotName": resolved_slot or (add_res.get("item") or {}).get("primarySlot") or chosen.get("primarySlot"),
            }

        mutation_res = run_mutation(worker, _do_equip_db, apply=apply)
        return {
            "query": query,
            "dbType": kind.upper(),
            "total": total_hits,
            "matches": indexed_items,
            "equipped": True,
            "applied": apply,
            "mode": mutation_res["mode"],
            "slotName": resolved_slot or mutation_res["detail"].get("slotName") or chosen.get("primarySlot"),
            "selectedItem": chosen if with_raw else {k: v for k, v in chosen.items() if k != "raw"},
            "tooltip": native_tip,
            "mutation": mutation_res,
        }


# Alias for callers using `query_item_db`
query_item_db = search_and_try_item_db


# ---------------------------------------------------------------------------
# Class / Ascendancy & New Build Creation
# ---------------------------------------------------------------------------


def _match_named_option(
    options: list[dict[str, Any]],
    query: str,
    entity_label: str,
) -> dict[str, Any]:
    q = query.strip()
    q_low = q.lower()
    if q_low in ("none", "无", "clear", "0"):
        for opt in options:
            if opt.get("id") == 0 or str(opt.get("name") or "").lower() == "none":
                return opt

    for opt in options:
        n_en = strip_pob_escapes(str(opt.get("name") or "")).strip().lower()
        n_zh = strip_pob_escapes(str(opt.get("nameZh") or "")).strip().lower()
        if q_low in (n_en, n_zh) or (q.isdigit() and int(q) == opt.get("id")):
            return opt

    sub_matches = [
        opt
        for opt in options
        if q_low in strip_pob_escapes(str(opt.get("name") or "")).strip().lower()
        or q_low in strip_pob_escapes(str(opt.get("nameZh") or "")).strip().lower()
    ]
    if len(sub_matches) == 1:
        return sub_matches[0]
    if len(sub_matches) > 1:
        samples = ", ".join(f"{o.get('name')} ({o.get('nameZh')})" for o in sub_matches[:6])
        raise ValueError(f"Ambiguous {entity_label} '{query}' matched multiple options: {samples}")

    available = ", ".join(f"{o.get('name')}/{o.get('nameZh')}" for o in options[:12])
    raise ValueError(f"Unknown {entity_label} '{query}'. Available: {available}")


def _apply_class_ascendancy_in_place(
    w: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy_name: str | None = None,
    secondary_ascendancy_name: str | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy_name, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_ascendancy_name, field="secondary_ascendancy")

    lc = cache.get_classes() if cache is not None else w.call("list_classes", {})
    classes = lc.get("classes", [])
    secondary = lc.get("secondary", [])
    before = dict(lc.get("current") or {})

    target_class_obj: dict[str, Any] | None = None
    target_asc_obj: dict[str, Any] | None = None
    target_sec_obj: dict[str, Any] | None = None
    changes: list[str] = []
    class_change_mode: str | None = None

    if class_name is not None and str(class_name).strip():
        target_class_obj = _match_named_option(classes, str(class_name), "class")
    else:
        cur_cid = before.get("classId")
        target_class_obj = next((c for c in classes if c.get("id") == cur_cid), None)

    if ascendancy_name is not None and str(ascendancy_name).strip():
        asc_q = str(ascendancy_name).strip()
        if class_name is not None and str(class_name).strip():
            if target_class_obj is None:
                raise ValueError(f"Unable to resolve class '{class_name}' for ascendancy '{asc_q}'.")
            target_asc_obj = _match_named_option(
                target_class_obj.get("ascendancies", []),
                asc_q,
                f"ascendancy for {target_class_obj.get('name')}",
            )
        else:
            try:
                if target_class_obj is None:
                    raise ValueError(f"Current character class is unknown for ascendancy '{asc_q}'.")
                target_asc_obj = _match_named_option(
                    target_class_obj.get("ascendancies", []),
                    asc_q,
                    f"ascendancy for {target_class_obj.get('name')}",
                )
            except ValueError:
                found_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
                q_low = asc_q.lower()
                for cls in classes:
                    for asc in cls.get("ascendancies", []):
                        if asc.get("id") == 0:
                            continue
                        n_en = strip_pob_escapes(str(asc.get("name") or "")).strip().lower()
                        n_zh = strip_pob_escapes(str(asc.get("nameZh") or "")).strip().lower()
                        if q_low in (n_en, n_zh) or q_low in n_en or q_low in n_zh:
                            found_pairs.append((cls, asc))
                exact_pairs = [
                    p
                    for p in found_pairs
                    if q_low
                    in (
                        strip_pob_escapes(str(p[1].get("name") or "")).strip().lower(),
                        strip_pob_escapes(str(p[1].get("nameZh") or "")).strip().lower(),
                    )
                ]
                chosen_pairs = exact_pairs or found_pairs
                if len(chosen_pairs) == 1:
                    target_class_obj, target_asc_obj = chosen_pairs[0]
                elif len(chosen_pairs) > 1:
                    samples = ", ".join(f"{c['name']}/{a['name']}" for c, a in chosen_pairs[:6])
                    raise ValueError(f"Ambiguous ascendancy '{asc_q}': {samples}")
                else:
                    raise

    if secondary_ascendancy_name is not None and str(secondary_ascendancy_name).strip():
        target_sec_obj = _match_named_option(secondary, str(secondary_ascendancy_name), "secondary ascendancy")

    if target_class_obj is not None and target_class_obj.get("id") != before.get("classId"):
        cid = int(target_class_obj["id"])
        res = w.call("set_class", {"classId": cid})
        if res.get("needsConfirm") == "class_change":
            res = w.call("set_class", {"classId": cid, "confirm": "connect"})
            if res.get("connectFailed") or res.get("needsConfirm") == "class_change":
                res = w.call("set_class", {"classId": cid, "confirm": "reset"})
                class_change_mode = "reset"
            else:
                class_change_mode = "connect"
        else:
            class_change_mode = "direct"
        changes.append(
            f"class: {before.get('className')} -> {target_class_obj.get('name')} ({class_change_mode})"
        )

    if target_asc_obj is not None:
        aid = int(target_asc_obj["id"])
        w.call("set_ascendancy", {"ascendClassId": aid})
        changes.append(f"ascendancy: {before.get('ascendClassName')} -> {target_asc_obj.get('name')}")

    if target_sec_obj is not None:
        sid = int(target_sec_obj["id"])
        w.call("set_secondary_ascendancy", {"ascendClassId": sid})
        changes.append(f"secondaryAscendancy: {target_sec_obj.get('name')} (id={sid})")

    after_lc = w.call("list_classes", {})
    if cache is not None:
        cache.classes = after_lc
        cache.invalidate("tree", "items", "skills", "specs")
    after = dict(after_lc.get("current") or {})
    return {
        "before": before,
        "after": after,
        "changes": changes,
        "classChangeMode": class_change_mode,
    }


def set_class_ascendancy(
    worker: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy_name: str | None = None,
    secondary_ascendancy_name: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Change character class, primary ascendancy, and/or secondary ascendancy (Bloodline) in a snapshot transaction."""
    if not any(x is not None and str(x).strip() != "" for x in (class_name, ascendancy_name, secondary_ascendancy_name)):
        raise ValueError("Specify at least one of class_name, ascendancy_name, or secondary_ascendancy_name.")
    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy_name, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_ascendancy_name, field="secondary_ascendancy")

    def _do_set_class(w: BridgeWorker) -> dict[str, Any]:
        return _apply_class_ascendancy_in_place(
            w,
            class_name=class_name,
            ascendancy_name=ascendancy_name,
            secondary_ascendancy_name=secondary_ascendancy_name,
        )

    return run_mutation(worker, _do_set_class, apply=apply)


def _apply_new_build_in_place(
    w: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy: str | None = None,
    secondary_ascendancy: str | None = None,
    level: int | None = None,
    name: str | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_ascendancy, field="secondary_ascendancy")

    nb_params: dict[str, Any] = {}
    if name and str(name).strip():
        nb_params["name"] = str(name).strip()
    w.call("new_build", nb_params)
    if cache is not None:
        cache.invalidate_all()

    changes: list[str] = ["new_build"]
    cls_detail = None
    if any(x is not None and str(x).strip() != "" for x in (class_name, ascendancy, secondary_ascendancy)):
        cls_detail = _apply_class_ascendancy_in_place(
            w,
            class_name=class_name,
            ascendancy_name=ascendancy,
            secondary_ascendancy_name=secondary_ascendancy,
            cache=cache,
        )
        changes.extend(cls_detail.get("changes", []))

    if level is not None:
        lv = int(level)
        if lv < 1 or lv > 100:
            raise ValueError(f"Character level must be between 1 and 100 (got {lv}).")
        w.call("set_build_field", {"field": "level", "value": lv})
        changes.append(f"level={lv}")

    info = w.call("get_build_info", {})
    return {
        "name": nb_params.get("name"),
        "buildInfo": info,
        "class": (cls_detail or {}).get("after") or {
            "className": info.get("className"),
            "ascendClassName": info.get("ascendClassName"),
        },
        "level": info.get("level"),
        "changes": changes,
    }


def create_new_build(
    worker: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy: str | None = None,
    ascendancy_name: str | None = None,
    secondary_ascendancy: str | None = None,
    secondary_ascendancy_name: str | None = None,
    level: int | None = None,
    name: str | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Create a fresh blank PoB build with optional class, ascendancy, bloodline, and character level."""
    eff_asc = ascendancy if ascendancy is not None else ascendancy_name
    eff_sec = secondary_ascendancy if secondary_ascendancy is not None else secondary_ascendancy_name
    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(eff_asc, field="ascendancy")
    check_poe2_class_or_ascendancy(eff_sec, field="secondary_ascendancy")

    def _do_new_build(w: BridgeWorker) -> dict[str, Any]:
        return _apply_new_build_in_place(
            w,
            class_name=class_name,
            ascendancy=eff_asc,
            secondary_ascendancy=eff_sec,
            level=level,
            name=name,
        )

    res = run_mutation(worker, _do_new_build, apply=apply)
    res["buildInfo"] = (res.get("detail") or {}).get("buildInfo")
    return res


# ---------------------------------------------------------------------------
# Config & Custom Mods
# ---------------------------------------------------------------------------


def _coerce_config_value(raw_val: Any) -> Any:
    if isinstance(raw_val, (bool, int, float)):
        return raw_val
    s = str(raw_val).strip()
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _split_custom_mod_lines(raw_text: str) -> list[str]:
    """Split multiline or semicolon-separated custom mod text into clean non-empty lines."""
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        for part in raw_line.split(";"):
            s = part.strip()
            if s:
                lines.append(s)
    return lines


_ENEMY_IS_BOSS_ALIASES: dict[str, str] = {
    "shaper": "Pinnacle",
    "sirus": "Pinnacle",
    "uber atziri": "Boss",
    "atziri": "Boss",
}


def _apply_config_in_place(
    w: BridgeWorker,
    assignments: list[tuple[str, Any]],
    custom_mods: str | None = None,
    append_custom_mods: str | None = None,
    *,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    applied_vars: list[dict[str, Any]] = []
    touched_boss = False

    for raw_var, _ in assignments:
        check_input(str(raw_var), scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    if custom_mods is not None and custom_mods.strip().lower() not in ("clear", "reset", "none", "disable", "disabled", ""):
        check_input(custom_mods, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    if append_custom_mods is not None:
        check_input(append_custom_mods, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")

    known_items: dict[str, dict[str, Any]] = {}
    if assignments:
        if cache is not None:
            known_items = cache.get_known_config_items()
        else:
            cfg_meta = w.call("list_config", {})
            for sec in cfg_meta.get("sections", []):
                for item in sec.get("items", []):
                    vname = item.get("var")
                    if vname:
                        known_items[str(vname).lower()] = item

    if assignments:
        rpc_calls: list[tuple[str, dict[str, Any]]] = []
        meta_rows: list[tuple[str, str, Any]] = []
        for raw_var, raw_val in assignments:
            item = known_items.get(str(raw_var).strip().lower(), {})
            var = str(item.get("var") or str(raw_var).strip())
            is_list = item.get("type") == "list"
            list_options = item.get("list") or []
            none_is_option = is_list and any(
                (opt.get("val") is not None and str(opt.get("val")).strip().lower() == "none")
                or strip_pob_escapes(str(opt.get("label") or "")).strip().lower() == "none"
                for opt in list_options
                if isinstance(opt, dict)
            )
            v_str = "" if raw_val is None else str(raw_val).strip()
            v_low = v_str.lower()
            if (
                raw_val is None
                or v_low in ("reset", "null", "nil", "default", "__unset__")
                or (v_low == "none" and not none_is_option)
            ):
                rpc_calls.append(("reset_config", {"var": var}))
                meta_rows.append((var, "reset", None))
            else:
                matched_list_val = False
                coerced: Any = None
                if is_list and not isinstance(raw_val, bool):
                    lookup_str = _ENEMY_IS_BOSS_ALIASES.get(v_low, v_str) if var == "enemyIsBoss" else v_str
                    lookup_low = lookup_str.strip().lower()
                    for opt in list_options:
                        if not isinstance(opt, dict) or "val" not in opt:
                            continue
                        opt_val_str = str(opt.get("val")).strip().lower()
                        opt_lbl = strip_pob_escapes(str(opt.get("label") or "")).strip().lower()
                        opt_lbl_zh = strip_pob_escapes(str(opt.get("labelZh") or "")).strip().lower()
                        if (
                            lookup_low == opt_val_str
                            or (opt_lbl and lookup_low == opt_lbl)
                            or (opt_lbl_zh and lookup_low == opt_lbl_zh)
                        ):
                            coerced = opt["val"]
                            matched_list_val = True
                            break
                    if not matched_list_val and var == "enemyIsBoss" and v_low in _ENEMY_IS_BOSS_ALIASES:
                        coerced = _ENEMY_IS_BOSS_ALIASES[v_low]
                        matched_list_val = True
                if not matched_list_val:
                    coerced = _coerce_config_value(raw_val)
                    if var == "enemyIsBoss" and isinstance(coerced, str):
                        coerced = _ENEMY_IS_BOSS_ALIASES.get(coerced.lower(), coerced)
                if item.get("type") in ("count", "countAllowZero", "integer", "float") and isinstance(coerced, bool):
                    if coerced:
                        coerced = 1
                        rpc_calls.append(("set_config", {"var": var, "value": coerced}))
                        meta_rows.append((var, "set", coerced))
                    else:
                        rpc_calls.append(("reset_config", {"var": var}))
                        meta_rows.append((var, "reset", None))
                else:
                    rpc_calls.append(("set_config", {"var": var, "value": coerced}))
                    meta_rows.append((var, "set", coerced))
            if var in ("enemyIsBoss", "presetBossSkills"):
                touched_boss = True

        results = _call_batch(w, rpc_calls) if len(rpc_calls) > 1 else [w.call(rpc_calls[0][0], rpc_calls[0][1])]
        for (var, act, val), res in zip(meta_rows, results):
            if act == "reset":
                applied_vars.append({"var": var, "action": "reset", "result": res})
            else:
                applied_vars.append({"var": var, "action": "set", "value": val, "result": res})
        if cache is not None:
            cache.invalidate("config")

    if touched_boss:
        cfg = w.call("list_config", {})
        if cache is not None:
            cache.config = cfg
            cache._index_config_vars(cfg)
        for sec in cfg.get("sections", []):
            for item in sec.get("items", []):
                if item.get("var") == "presetBossSkills" and item.get("value") not in (None, "", "None"):
                    w.call("set_config", {"var": "presetBossSkills", "value": item["value"]})
                    if cache is not None:
                        cache.invalidate("config")
                    break

    if custom_mods is not None:
        cm_clean = custom_mods.strip()
        if cm_clean.lower() in ("clear", "reset", "none", "disable", "disabled", ""):
            w.call(
                "set_custom_mods",
                {"list": [{"title": "Default", "text": "# disabled", "enabled": True}]},
            )
            applied_vars.append({"var": "customMods", "action": "clear"})
        else:
            w.call(
                "set_custom_mods",
                {"list": [{"title": "CLI Custom Mods", "text": cm_clean, "enabled": True}]},
            )
            applied_vars.append({"var": "customMods", "action": "set", "value": cm_clean})
        if cache is not None:
            cache.invalidate("config", "skills")

    if append_custom_mods is not None:
        new_lines = _split_custom_mod_lines(append_custom_mods)
        if not new_lines:
            raise ValueError("append_custom_mods cannot be empty.")

        cfg_cur = cache.get_config() if cache is not None else w.call("list_config", {})
        existing_blocks = list(cfg_cur.get("customMods") or [])
        updated_blocks: list[dict[str, Any]] = []
        target_idx: int | None = None

        for blk in existing_blocks:
            title = blk.get("title") or "Default"
            enabled = blk.get("enabled", True) is not False
            raw_blk_text = str(blk.get("text") or "")
            cleaned_lines = [
                ln
                for ln in raw_blk_text.splitlines()
                if ln.strip().lower() != "# disabled"
            ]
            cleaned_text = "\n".join(cleaned_lines).strip()
            updated_blocks.append({"title": title, "text": cleaned_text, "enabled": enabled})
            if enabled:
                target_idx = len(updated_blocks) - 1

        append_str = "\n".join(new_lines)
        if target_idx is None:
            updated_blocks.append({"title": "Default", "text": append_str, "enabled": True})
        else:
            prev_text = updated_blocks[target_idx]["text"]
            updated_blocks[target_idx]["text"] = f"{prev_text}\n{append_str}" if prev_text else append_str

        w.call("set_custom_mods", {"list": updated_blocks})
        if cache is not None:
            cache.invalidate("config", "skills")
        combined_text = "\n".join(b["text"] for b in updated_blocks if b.get("enabled") and b.get("text"))
        applied_vars.append(
            {
                "var": "customMods",
                "action": "append",
                "appendedLines": new_lines,
                "customModsText": combined_text,
            }
        )

    return {"assignments": applied_vars}


def modify_config(
    worker: BridgeWorker,
    *,
    assignments: dict[str, Any] | list[tuple[str, Any]] | None = None,
    custom_mods: str | None = None,
    append_custom_mods: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Set/reset PoB ConfigTab variables and/or replace or append `customMods` (0-residue by default)."""
    pairs: list[tuple[str, Any]] = []
    if isinstance(assignments, dict):
        pairs = list(assignments.items())
    elif assignments:
        pairs = [(k, v) for k, v in assignments]

    if not pairs and custom_mods is None and append_custom_mods is None:
        raise ValueError("No config assignments, custom_mods, or append_custom_mods provided.")

    def _do_cfg(w: BridgeWorker) -> dict[str, Any]:
        return _apply_config_in_place(
            w,
            pairs,
            custom_mods=custom_mods,
            append_custom_mods=append_custom_mods,
        )

    return run_mutation(worker, _do_cfg, apply=apply)


# ---------------------------------------------------------------------------
# Loadout / Spec / Main Skill Switching
# ---------------------------------------------------------------------------

LOADOUT_SENTINELS = frozenset({
    "loadouts:",
    "-----",
    "sync",
    "new loadout",
    "manage loadouts",
    "no loadouts",
    "help >>",
})


def _resolve_id_or_title(
    items: list[dict[str, Any]],
    selector: int | str,
    *,
    id_key: str = "id",
    title_keys: tuple[str, ...] = ("title", "titleZh", "label", "labelZh"),
    entity_name: str = "item",
) -> int:
    s = str(selector).strip()
    if s.isdigit():
        val = int(s)
        if any(int(it.get(id_key, -1)) == val for it in items):
            return val
        if 1 <= val <= len(items):
            return int(items[val - 1][id_key])
    s_low = s.lower()
    for it in items:
        for k in title_keys:
            if s_low == strip_pob_escapes(str(it.get(k) or "")).strip().lower():
                return int(it[id_key])
    for it in items:
        for k in title_keys:
            if s_low in strip_pob_escapes(str(it.get(k) or "")).strip().lower():
                return int(it[id_key])
    raise ValueError(f"Could not find {entity_name} matching '{selector}'.")


def _apply_switch_main_in_place(
    w: BridgeWorker,
    *,
    spec_index: int | str | None = None,
    skill_group: str | int | None = None,
    main_skill_index: int | None = None,
    skill_part_index: int | None = None,
    item_set_id: int | str | None = None,
    skill_set_id: int | str | None = None,
    config_set_id: int | str | None = None,
    loadout_index: int | str | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    changes: list[str] = []
    if loadout_index is not None:
        lo_res = w.call("list_loadouts", {})
        raw_list = lo_res.get("entries") if "entries" in lo_res else lo_res.get("list", [])
        real_loadouts = [
            it
            for it in (raw_list or [])
            if isinstance(it, dict)
            and str(it.get("kind", "loadout")).strip().lower() == "loadout"
            and strip_pob_escapes(str(it.get("label") or "")).strip().lower() not in LOADOUT_SENTINELS
        ]
        if not real_loadouts:
            raise ValueError("Build has no synced loadouts available.")
        s_lo = str(loadout_index).strip()
        if s_lo.isdigit():
            k = int(s_lo)
            if 1 <= k <= len(real_loadouts):
                target_raw_idx = int(real_loadouts[k - 1]["index"])
            elif any(int(it["index"]) == k for it in real_loadouts):
                target_raw_idx = k
            else:
                raise ValueError(f"Loadout #{k} out of range (1..{len(real_loadouts)}).")
        else:
            target_raw_idx = _resolve_id_or_title(
                real_loadouts,
                s_lo,
                id_key="index",
                title_keys=("label", "labelZh"),
                entity_name="loadout",
            )
        w.call("select_loadout", {"index": target_raw_idx})
        changes.append(f"loadout={loadout_index} (rawIndex={target_raw_idx})")
        if cache is not None:
            cache.invalidate_all()
    if spec_index is not None:
        specs_res = cache.get_specs() if cache is not None else w.call("list_specs", {})
        resolved_spec = _resolve_id_or_title(
            specs_res.get("specs", []),
            spec_index,
            id_key="index",
            entity_name="tree spec",
        )
        w.call("set_active_spec", {"index": resolved_spec})
        changes.append(f"activeSpec={resolved_spec}")
        if cache is not None:
            cache.invalidate("tree", "classes", "specs", "items", "skills")
    if item_set_id is not None:
        items_res = cache.get_items() if cache is not None else w.call("list_items", {})
        resolved_iset = _resolve_id_or_title(
            items_res.get("itemSets", []),
            item_set_id,
            id_key="id",
            entity_name="item set",
        )
        w.call("set_item_set", {"id": resolved_iset})
        changes.append(f"itemSetId={resolved_iset}")
        if cache is not None:
            cache.invalidate("items", "skills", "tree")
    if skill_set_id is not None:
        skills_res = cache.get_skills() if cache is not None else w.call("list_skills", {})
        resolved_sset = _resolve_id_or_title(
            skills_res.get("skillSets", []),
            skill_set_id,
            id_key="id",
            entity_name="skill set",
        )
        w.call("set_skill_set", {"id": resolved_sset})
        changes.append(f"skillSetId={resolved_sset}")
        if cache is not None:
            cache.invalidate("skills")
    if config_set_id is not None:
        cfg_res = cache.get_config() if cache is not None else w.call("list_config", {})
        resolved_cset = _resolve_id_or_title(
            cfg_res.get("configSets", []),
            config_set_id,
            id_key="id",
            entity_name="config set",
        )
        w.call("set_config_set", {"id": resolved_cset})
        changes.append(f"configSetId={resolved_cset}")
        if cache is not None:
            cache.invalidate("config_schema")
    if skill_group is not None:
        skills_res = cache.get_skills() if cache is not None else w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, skill_group)
        w.call("set_build_field", {"field": "mainSocketGroup", "value": g_idx})
        changes.append(f"mainSocketGroup={g_idx}")
        if cache is not None:
            cache.invalidate("skills")
    if main_skill_index is not None:
        w.call("set_build_field", {"field": "mainSkill", "value": int(main_skill_index)})
        changes.append(f"mainSkill={main_skill_index}")
    if skill_part_index is not None:
        w.call("set_build_field", {"field": "mainSkillPart", "value": int(skill_part_index)})
        changes.append(f"mainSkillPart={skill_part_index}")
    return {"changes": changes, "header": w.call("get_build_header", {})}


def switch_main(
    worker: BridgeWorker,
    *,
    spec_index: int | str | None = None,
    skill_group: str | int | None = None,
    main_skill_index: int | None = None,
    skill_part_index: int | None = None,
    item_set_id: int | str | None = None,
    skill_set_id: int | str | None = None,
    config_set_id: int | str | None = None,
    loadout_index: int | str | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Switch active spec, main skill group, active skill/part, item/skill/config set, or synced loadout."""
    def _do_switch(w: BridgeWorker) -> dict[str, Any]:
        return _apply_switch_main_in_place(
            w,
            spec_index=spec_index,
            skill_group=skill_group,
            main_skill_index=main_skill_index,
            skill_part_index=skill_part_index,
            item_set_id=item_set_id,
            skill_set_id=skill_set_id,
            config_set_id=config_set_id,
            loadout_index=loadout_index,
        )

    return run_mutation(worker, _do_switch, apply=apply)


# ---------------------------------------------------------------------------
# Composite Batch Execution & Return Sections
# ---------------------------------------------------------------------------


def _execute_batch_ops(
    w: BridgeWorker,
    ops: list[dict[str, Any]],
    cache: _BatchCache,
) -> list[dict[str, Any]]:
    """Execute a list of batch mutation ops in place with smart per-tab caching."""
    step_results: list[dict[str, Any]] = []
    for idx, op in enumerate(ops, start=1):
        if not isinstance(op, dict):
            raise ValueError(f"Batch op at step {idx} must be a JSON object.")
        raw_kind = str(op.get("op") or op.get("type") or "").strip().lower()
        kind = raw_kind.replace("-", "_")

        if kind == "config":
            set_map = op.get("set")
            if set_map is not None and not isinstance(set_map, dict):
                raise ValueError(f"Batch op 'config' at step {idx} expects 'set' to be a JSON object.")
            reset_list = op.get("reset")
            if reset_list is not None and not isinstance(reset_list, list):
                raise ValueError(f"Batch op 'config' at step {idx} expects 'reset' to be a JSON list.")
            pairs = [(k, v) for k, v in (set_map or {}).items()]
            for var in reset_list or []:
                pairs.append((var, "reset"))
            cm = op.get("customMods") if "customMods" in op else op.get("custom_mods")
            acm = op.get("appendCustomMods") if "appendCustomMods" in op else op.get("append_custom_mods")
            cfg_res = _apply_config_in_place(
                w,
                pairs,
                custom_mods=str(cm) if cm is not None else None,
                append_custom_mods=str(acm) if acm is not None else None,
                cache=cache,
            )
            step_results.append({"step": idx, "op": "config", "count": len(cfg_res["assignments"])})

        elif kind == "flask":
            f_sel = op.get("index", "all")
            act = bool(op.get("active", True))
            slots = _parse_flask_slots(f_sel)
            _call_batch(w, [("set_slot_active", {"slotName": s_name, "active": act}) for s_name in slots])
            cache.invalidate("items")
            step_results.append({"step": idx, "op": "flask", "slotName": ",".join(slots), "active": act})

        elif kind == "item":
            raw_val = op.get("raw")
            raw_text = str(raw_val).strip() if raw_val is not None else ""
            if not raw_text:
                raise ValueError(f"Batch op 'item' at step {idx} requires non-empty 'raw'.")
            slot_query = op.get("slot") or op.get("slotName")
            if slot_query:
                check_input(str(slot_query), scope=GuardrailScope.SLOT, expected_game="poe1")
            check_input_item_raw(raw_text, expected_game="poe1")
            slot_name = _resolve_slot_from_items(cache.get_items(), str(slot_query)) if slot_query else None
            params: dict[str, Any] = {"raw": raw_text, "equip": True}
            if slot_name:
                params["slotName"] = slot_name
            res = w.call("add_item", params)
            cache.invalidate("items", "skills")
            if _is_jewel_slot_name(slot_name) or "jewel" in str((res.get("item") or {}).get("type") or "").lower():
                cache.invalidate("tree")
            step_results.append({"step": idx, "op": "item", "slotName": slot_name, "item": res.get("item")})

        elif kind == "item_db":
            query = str(op.get("query") or "").strip()
            db_type = str(op.get("dbType") or op.get("db_type") or op.get("kind") or "UNIQUE")
            item_index = op.get("index") if op.get("index") is not None else op.get("itemIndex", op.get("item_index"))
            slot_query = op.get("slot") or op.get("slotName")
            if slot_query:
                check_input(str(slot_query), scope=GuardrailScope.SLOT, expected_game="poe1")
            if query:
                check_input(query, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
            resolved_slot = _resolve_slot_from_items(cache.get_items(), str(slot_query)) if slot_query else None
            db_kind, items, _ = _search_item_db_items(
                w,
                query=query,
                db_type=db_type,
                limit=max(15, int(item_index or 1)),
            )
            if not items:
                raise ValueError(f"No {db_kind} database items matched query '{query}' at step {idx}.")
            pick_idx = int(item_index) if item_index is not None else 1
            if pick_idx < 1 or pick_idx > len(items):
                raise ValueError(f"item_db index {pick_idx} out of range (1..{len(items)}) at step {idx}.")
            chosen = items[pick_idx - 1]
            chosen_raw = str(chosen.get("raw") or "").strip()
            if not chosen_raw:
                raise RuntimeError(f"Selected DB item '{chosen.get('title') or chosen.get('name')}' has no raw text.")
            add_params: dict[str, Any] = {"raw": chosen_raw, "equip": True}
            if resolved_slot:
                add_params["slotName"] = resolved_slot
            res = w.call("add_item", add_params)
            cache.invalidate("items", "skills")
            if _is_jewel_slot_name(resolved_slot) or "jewel" in str(chosen.get("type") or "").lower():
                cache.invalidate("tree")
            step_results.append(
                {
                    "step": idx,
                    "op": "item_db",
                    "query": query,
                    "slotName": resolved_slot or (res.get("item") or {}).get("primarySlot"),
                    "item": res.get("item"),
                }
            )

        elif kind == "item_edit":
            slot_query = str(op.get("slot") or op.get("slotName") or "").strip()
            raw_reps = op.get("replaceMods") or op.get("replace_mods") or []
            rep_tuples = [(str(p[0]), str(p[1])) for p in raw_reps if isinstance(p, (list, tuple)) and len(p) == 2]
            edit_res = _apply_edit_item_mods_in_place(
                w,
                slot_name=slot_query,
                replace_mods=rep_tuples or None,
                add_mods=op.get("addMods") or op.get("add_mods"),
                remove_mods=op.get("removeMods") or op.get("remove_mods"),
                cache=cache,
            )
            step_results.append(
                {
                    "step": idx,
                    "op": "item_edit",
                    "slotName": edit_res["slotName"],
                    "changes": edit_res["changes"],
                }
            )

        elif kind in ("unequip", "item_unequip", "unequip_slot"):
            slot_query = str(op.get("slot") or op.get("slotName") or "").strip()
            if not slot_query:
                raise ValueError(f"Batch op '{kind}' at step {idx} requires 'slot'.")
            check_input(slot_query, scope=GuardrailScope.SLOT, expected_game="poe1")
            items_res = cache.get_items()
            resolved_slot = _resolve_slot_from_items(items_res, slot_query)
            w.call("unequip_slot", {"slotName": resolved_slot})
            cache.invalidate("items", "skills")
            if _is_jewel_slot_name(resolved_slot):
                cache.invalidate("tree")
            step_results.append({"step": idx, "op": "unequip", "slotName": resolved_slot})

        elif kind == "anoint":
            notable_val = op.get("notable")
            notable_text = str(notable_val).strip() if notable_val is not None else ""
            if not notable_text:
                raise ValueError(f"Batch op 'anoint' at step {idx} requires non-empty 'notable'.")
            an_res = _apply_anoint_in_place(
                w,
                slot_name=str(op.get("slot") or "Amulet"),
                notable_query=notable_text,
                anoint_index=int(op["index"]) if op.get("index") is not None else 1,
                cache=cache,
            )
            step_results.append({"step": idx, "op": "anoint", "notable": an_res.get("notable")})

        elif kind == "tree":
            tree_state = cache.get_tree()
            nodes_db = w.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
            dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}
            resolved_alloc = [
                _resolve_node_token(w, str(tok), nodes_db, dynamic_map)
                for tok in (op.get("allocate") or [])
            ]
            resolved_dealloc = [
                _resolve_node_token(w, str(tok), nodes_db, dynamic_map)
                for tok in (op.get("deallocate") or [])
            ]
            allow_casc = bool(
                op["allowCascade"]
                if "allowCascade" in op
                else op.get("allow_cascade", True)
            )
            actions_taken, final_state, orphaned_nodes, points_delta, cascade_warning = _apply_tree_changes_in_place(
                w,
                resolved_alloc,
                resolved_dealloc,
                initial_tree_state=tree_state,
                allow_cascade=allow_casc,
            )
            cache.tree = final_state
            cache.invalidate("items", "skills")
            step_entry: dict[str, Any] = {
                "step": idx,
                "op": "tree",
                "actions": actions_taken,
                "pointsDelta": points_delta,
                "orphanedNodes": orphaned_nodes,
            }
            if cascade_warning:
                step_entry["cascadeWarning"] = cascade_warning
            step_results.append(step_entry)

        elif kind in ("tree_reset", "reset_tree"):
            tattoos = bool(op.get("tattoos", False))
            res = w.call("reset_tree", {"tattoos": True} if tattoos else {})
            if isinstance(res, dict) and "allocatedNodes" in res:
                cache.tree = res
            else:
                cache.invalidate("tree")
            cache.invalidate("items", "skills")
            step_results.append({"step": idx, "op": "tree_reset", "tattoos": tattoos, "points": res.get("points")})

        elif kind == "import_tree_url":
            url = str(op.get("url") or "").strip()
            title = str(op.get("title") or op.get("name") or "Imported Tree").strip()
            if not url:
                raise ValueError(f"Batch op 'import_tree_url' at step {idx} requires 'url'.")
            validate_url(url, expected_game="poe1", cli_subcommand="batch")
            specs_res = w.call("import_tree_url", {"url": url, "title": title})
            cache.invalidate("tree", "classes", "items", "skills")
            cache.specs = specs_res
            step_results.append(
                {
                    "step": idx,
                    "op": "import_tree_url",
                    "title": title,
                    "activeSpec": specs_res.get("activeSpec"),
                }
            )

        elif kind == "new_build":
            nb_res = _apply_new_build_in_place(
                w,
                class_name=op.get("class") or op.get("className") or op.get("class_name"),
                ascendancy=op.get("ascendancy") or op.get("asc") or op.get("ascendancyName") or op.get("ascendancy_name"),
                secondary_ascendancy=(
                    op.get("secondaryAscendancy")
                    or op.get("secondary_ascendancy")
                    or op.get("secondaryAsc")
                ),
                level=int(op["level"]) if op.get("level") is not None else None,
                name=op.get("name"),
                cache=cache,
            )
            step_results.append({"step": idx, "op": "new_build", "changes": nb_res["changes"], "level": nb_res["level"]})

        elif kind == "set_level":
            raw_lv = op.get("level") if op.get("level") is not None else op.get("value")
            if raw_lv is None and "auto" not in op:
                raise ValueError(f"Batch op 'set_level' at step {idx} requires 'level' or 'auto'.")
            lv_val: int | None = None
            if raw_lv is not None:
                lv_val = int(raw_lv)
                if lv_val < 1 or lv_val > 100:
                    raise ValueError(f"Character level must be between 1 and 100 (got {lv_val}).")
                w.call("set_build_field", {"field": "level", "value": lv_val})
            if "auto" in op:
                w.call("set_build_field", {"field": "levelAuto", "value": bool(op["auto"])})
            cache.invalidate("tree", "config", "items", "skills")
            step_results.append({"step": idx, "op": "set_level", "level": lv_val, "auto": op.get("auto")})

        elif kind == "set_class":
            cls_res = _apply_class_ascendancy_in_place(
                w,
                class_name=op.get("class") or op.get("className") or op.get("class_name"),
                ascendancy_name=(
                    op.get("ascendancy")
                    or op.get("asc")
                    or op.get("ascendancyName")
                    or op.get("ascendancy_name")
                ),
                secondary_ascendancy_name=(
                    op.get("secondaryAscendancy")
                    or op.get("secondary_ascendancy")
                    or op.get("secondaryAsc")
                    or op.get("secondaryAscendancyName")
                ),
                cache=cache,
            )
            step_results.append({"step": idx, "op": "set_class", "changes": cls_res["changes"], "after": cls_res["after"]})

        elif kind == "switch_main":
            sw_res = _apply_switch_main_in_place(
                w,
                spec_index=op.get("spec") if "spec" in op else op.get("specIndex", op.get("spec_index")),
                skill_group=(
                    op.get("skillGroup")
                    if "skillGroup" in op
                    else op.get("skill_group", op.get("group"))
                ),
                main_skill_index=(
                    op.get("mainSkill")
                    if "mainSkill" in op
                    else op.get("mainSkillIndex", op.get("main_skill_index"))
                ),
                skill_part_index=(
                    op.get("skillPart")
                    if "skillPart" in op
                    else op.get("skillPartIndex", op.get("skill_part_index"))
                ),
                item_set_id=op.get("itemSet") if "itemSet" in op else op.get("itemSetId", op.get("item_set_id")),
                skill_set_id=op.get("skillSet") if "skillSet" in op else op.get("skillSetId", op.get("skill_set_id")),
                config_set_id=op.get("configSet") if "configSet" in op else op.get("configSetId", op.get("config_set_id")),
                loadout_index=op.get("loadout") if "loadout" in op else op.get("loadoutIndex", op.get("loadout_index")),
                cache=cache,
            )
            step_results.append({"step": idx, "op": "switch_main", "changes": sw_res["changes"]})

        elif kind in ("add_group", "new_group"):
            full_dps_val = (
                op.get("includeInFullDPS")
                if "includeInFullDPS" in op
                else op.get("include_in_full_dps", op.get("fullDps"))
            )
            ag_res = _apply_add_group_in_place(
                w,
                gems=op.get("gems"),
                label=op.get("label"),
                slot=op.get("slot"),
                enabled=bool(op.get("enabled", True)),
                include_in_full_dps=bool(full_dps_val) if full_dps_val is not None else None,
                make_main=bool(op.get("makeMain", op.get("make_main", False))),
                cache=cache,
            )
            step_results.append(
                {
                    "step": idx,
                    "op": "add_group",
                    "groupIndex": ag_res["groupIndex"],
                    "gemsCount": len(ag_res["gemsAdded"]),
                }
            )

        elif kind in ("delete_group", "rm_group", "remove_group"):
            g_sel = op.get("group") if op.get("group") is not None else op.get("index")
            if g_sel is None:
                raise ValueError(f"Batch op '{kind}' at step {idx} requires 'group' or 'index'.")
            skills_res = cache.get_skills()
            g_idx = resolve_skill_group_index(skills_res, g_sel)
            w.call("delete_group", {"index": g_idx})
            cache.invalidate("skills")
            step_results.append({"step": idx, "op": "delete_group", "groupIndex": g_idx})

        elif kind == "set_group":
            g_sel = op.get("group") if op.get("group") is not None else op.get("index", "main")
            skills_res = cache.get_skills()
            g_idx = resolve_skill_group_index(skills_res, g_sel)
            sg_params: dict[str, Any] = {"index": g_idx}
            if "label" in op and op["label"] is not None:
                sg_params["label"] = str(op["label"])
            if "enabled" in op and op["enabled"] is not None:
                sg_params["enabled"] = bool(op["enabled"])
            full_dps_val = (
                op.get("includeInFullDPS")
                if "includeInFullDPS" in op
                else op.get("include_in_full_dps", op.get("fullDps"))
            )
            if full_dps_val is not None:
                sg_params["includeInFullDPS"] = bool(full_dps_val)
            if "slot" in op and op["slot"] is not None:
                s_clean = str(op["slot"]).strip()
                if s_clean:
                    check_input(s_clean, scope=GuardrailScope.SLOT, expected_game="poe1")
                sg_params["slot"] = _resolve_slot_from_items(cache.get_items(), s_clean) if s_clean else ""
            if "mainActiveSkill" in op or "main_active_skill" in op:
                mas = op.get("mainActiveSkill", op.get("main_active_skill"))
                if mas is not None:
                    sg_params["mainActiveSkill"] = int(mas)
            w.call("set_group", sg_params)
            cache.invalidate("skills")
            step_results.append({"step": idx, "op": "set_group", "groupIndex": g_idx, "updates": sg_params})

        elif kind == "replace_group":
            g_sel = op.get("group") if op.get("group") is not None else op.get("index", "main")
            full_dps_val = (
                op.get("includeInFullDPS")
                if "includeInFullDPS" in op
                else op.get("include_in_full_dps", op.get("fullDps"))
            )
            rg_res = _apply_replace_group_in_place(
                w,
                g_sel,
                list(op.get("gems") or []),
                label=op.get("label"),
                slot=op.get("slot"),
                enabled=bool(op["enabled"]) if "enabled" in op and op["enabled"] is not None else None,
                include_in_full_dps=bool(full_dps_val) if full_dps_val is not None else None,
                cache=cache,
            )
            step_results.append(
                {
                    "step": idx,
                    "op": "replace_group",
                    "groupIndex": rg_res["groupIndex"],
                    "removedCount": rg_res["removedCount"],
                    "gemsCount": len(rg_res["gemsAdded"]),
                }
            )

        elif kind == "skill":
            skills_res = cache.get_skills()
            g_idx = resolve_skill_group_index(skills_res, op.get("group", "main"))
            group = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
            if group is None:
                raise ValueError(f"Skill group #{g_idx} not found in build.")

            grp_updates: dict[str, Any] = {"index": g_idx}
            full_dps_val = (
                op.get("includeInFullDPS")
                if "includeInFullDPS" in op
                else op.get("include_in_full_dps")
            )
            if full_dps_val is not None:
                grp_updates["includeInFullDPS"] = bool(full_dps_val)
            grp_en = op.get("groupEnabled") if "groupEnabled" in op else op.get("group_enabled")
            if grp_en is not None:
                grp_updates["enabled"] = bool(grp_en)
            if len(grp_updates) > 1:
                w.call("set_group", grp_updates)

            add_gem_name = op.get("addGem") or op.get("add_gem")
            remove_gem_sel = op.get("removeGem") if "removeGem" in op else op.get("remove_gem")
            if add_gem_name:
                gem_name = str(add_gem_name).strip()
                check_input(gem_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1")
                cand = _resolve_gem_candidate(w, g_idx, gem_name)
                raw_level = op.get("level")
                raw_quality = op.get("quality")
                raw_enabled = op.get("enabled")
                raw_count = op.get("count")
                add_params = {
                    "group": g_idx,
                    "level": int(raw_level) if raw_level is not None else 20,
                    "quality": int(raw_quality) if raw_quality is not None else 20,
                    "enabled": bool(raw_enabled) if raw_enabled is not None else True,
                    "count": int(raw_count) if raw_count is not None else 1,
                }
                if cand and cand.get("gemId"):
                    add_params["gemId"] = cand["gemId"]
                    add_params["nameSpec"] = cand.get("name") or gem_name
                else:
                    add_params["nameSpec"] = gem_name
                _add_gem_with_default_skill_flags(w, add_params)
            elif remove_gem_sel is not None:
                t_gem = _resolve_gem_in_group(group, remove_gem_sel)
                if group.get("source") and int(t_gem.get("index", 0)) == 1:
                    _guard_source_skill_group(group, action="remove gem #1")
                w.call("delete_gem", {"group": g_idx, "index": t_gem["index"]})
            elif op.get("gem") is not None:
                t_gem = _resolve_gem_in_group(group, op["gem"])
                gem_params: dict[str, Any] = {"group": g_idx, "index": t_gem["index"]}
                new_gem_name = op.get("newGem") or op.get("new_gem")
                if new_gem_name:
                    check_input(str(new_gem_name), scope=GuardrailScope.GEM_NAME, expected_game="poe1")
                    cand = _resolve_gem_candidate(w, g_idx, str(new_gem_name))
                    if cand and cand.get("gemId"):
                        gem_params["gemId"] = cand["gemId"]
                        gem_params["nameSpec"] = cand.get("name") or str(new_gem_name).strip()
                    else:
                        gem_params["nameSpec"] = str(new_gem_name).strip()
                for k in ("level", "quality", "enabled", "count"):
                    if k in op:
                        gem_params[k] = op[k]
                w.call("set_gem", gem_params)
            cache.invalidate("skills")
            step_results.append({"step": idx, "op": "skill", "group": g_idx})

        else:
            raise ValueError(f"Unsupported batch op type '{raw_kind}' at step {idx}.")

    return step_results


def _normalize_return_sections(return_sections: list[str] | str | None) -> list[str]:
    if not return_sections:
        return []
    if isinstance(return_sections, str):
        raw_items = [x.strip() for x in return_sections.split(",") if x.strip()]
    else:
        raw_items = [str(x).strip() for x in return_sections if str(x).strip()]
    return raw_items


def _collect_return_sections(
    worker: BridgeWorker,
    sections: list[str],
    *,
    keep: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """Lazily import inspection helpers and collect requested readouts on the active build state."""
    from . import analyzer as _analyzer
    from . import comparator as _comparator

    keep_arg = _normalize_keep_list(keep) if keep is not None else None
    out: dict[str, Any] = {}
    for raw_sec in sections:
        sec = raw_sec.strip()
        if not sec:
            continue
        if ":" in sec:
            head, body = sec.split(":", 1)
            head_low = head.strip().lower()
            body = body.strip()
        else:
            head_low = sec.lower()
            body = ""

        if head_low == "summary":
            fn = getattr(_analyzer, "extract_summary", None) or getattr(_analyzer, "get_summary")
            out["summary"] = fn(worker)
        elif head_low == "audit":
            body_low = body.lower()
            if body_low == "":
                attr = False
            elif body_low in ("full", "attribution"):
                attr = True
            else:
                raise ValueError(
                    f"Unsupported audit return modifier '{raw_sec}'. "
                    "Use 'audit', 'audit:full', or 'audit:attribution'."
                )
            out["audit"] = _call_compat(
                _analyzer.audit_build,
                worker,
                keep=keep_arg,
                attribution=attr,
            )
        elif head_low == "matrix":
            scenarios = [s.strip() for s in body.split(";") if s.strip()] if body else None
            out["matrix"] = _call_compat(
                _comparator.run_matrix,
                worker,
                scenarios=scenarios,
                keep=keep_arg,
            )
        elif head_low == "calc":
            kws = [k.strip() for k in body.split(";") if k.strip()] if body else []
            if not kws:
                raise ValueError("Return section 'calc:<keywords>' requires at least one keyword.")
            calc_fn = getattr(_analyzer, "search_calcs_breakdown", None) or getattr(_analyzer, "inspect_calcs")
            sig_params = inspect.signature(calc_fn).parameters
            if len(kws) == 1:
                out[sec] = calc_fn(worker, kws[0])
            elif "keywords" in sig_params:
                out[sec] = calc_fn(worker, keywords=kws)
            else:
                out[sec] = {kw: calc_fn(worker, kw) for kw in kws}
        elif head_low == "export":
            out["export"] = worker.call("export_code", {}).get("code", "")
        elif head_low == "items":
            out["items"] = _analyzer.inspect_items(worker)
        elif head_low == "skills":
            out["skills"] = _analyzer.inspect_skills(worker)
        elif head_low == "tree":
            out["tree"] = _analyzer.inspect_tree(worker)
        elif head_low == "config":
            out["config"] = _analyzer.inspect_config(worker)
        else:
            raise ValueError(
                f"Unsupported return section '{raw_sec}'. "
                "Supported: summary, audit[:full|attribution], matrix[:s1;s2], calc:<kw1;kw2>, export, items, skills, tree, config."
            )
    return out


def run_batch(
    worker: BridgeWorker,
    ops: list[dict[str, Any]],
    *,
    apply: bool = False,
    no_undo: bool = False,
    return_sections: list[str] | str | None = None,
    keep: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """Execute a composite list of mutations in a single transaction with optional inline readouts.

    - Supports all 19 op types with per-tab read caching across consecutive ops.
    - When `apply=True` and `no_undo=True`, skips pre-mutation snapshot/undo-stack overhead for fast loops.
    - When `return_sections` is provided (e.g. `["summary", "audit", "calc:DPS;Mana", "export"]`),
      attaches requested readouts under `"returns"` evaluated on the mutated state BEFORE any rollback.
    """
    if not ops:
        raise ValueError("Batch operations list cannot be empty.")

    sections = _normalize_return_sections(return_sections)

    if apply and no_undo:
        with worker.lock:
            fallback_code = getattr(worker, "last_good_code", None)
            if not fallback_code:
                fallback_code = str(worker.call("export_code", {}).get("code") or "") or None
            cache = _BatchCache(worker)
            try:
                step_results = _execute_batch_ops(worker, ops, cache)
                digest_after = capture_digest(worker)
                returns_map = _collect_return_sections(worker, sections, keep=keep) if sections else None
            except Exception:
                if fallback_code:
                    try:
                        worker.call("import_code", {"code": fallback_code, "mode": "replace"})
                    except Exception:
                        pass
                raise

            if returns_map and isinstance(returns_map.get("export"), str) and returns_map["export"]:
                snap_after = returns_map["export"]
            else:
                snap_after = str(worker.call("export_code", {}).get("code") or "")
            if snap_after:
                worker.last_good_code = snap_after
            worker.redo_stack.clear()
            save_fn = getattr(worker, "save_slot_state", None)
            if callable(save_fn):
                save_fn()

            result: dict[str, Any] = {
                "applied": True,
                "noUndo": True,
                "mode": "APPLIED (no-undo fast mode)",
                "detail": {"steps": step_results},
                "before": None,
                "after": {
                    "primaryDpsKey": digest_after["primaryDpsKey"],
                    "primaryDps": digest_after["primaryDps"],
                    "totalEhp": digest_after["totalEhp"],
                    "life": digest_after["life"],
                    "points": digest_after.get("buildInfo", {}).get("points", {}),
                },
                "diff": None,
                "undoDepth": len(worker.undo_stack),
                "redoDepth": len(worker.redo_stack),
            }
            if returns_map is not None:
                result["returns"] = returns_map
            return result

    captured_returns: dict[str, Any] | None = None

    def _do_batch(w: BridgeWorker) -> dict[str, Any]:
        nonlocal captured_returns
        cache = _BatchCache(w)
        step_results = _execute_batch_ops(w, ops, cache)
        if sections:
            captured_returns = _collect_return_sections(w, sections, keep=keep)
        return {"steps": step_results}

    res = run_mutation(worker, _do_batch, apply=apply)
    if captured_returns is not None:
        res["returns"] = captured_returns
    return res


# ---------------------------------------------------------------------------
# Multi-Variant Build Sweep Engine (`pob sweep`)
# ---------------------------------------------------------------------------


def _json_safe_num(val: Any) -> float | str | None:
    if val is None:
        return None
    parsed = _parse_numeric_or_none(val)
    if parsed is None:
        return str(val)
    if math.isfinite(parsed):
        return parsed
    return fmt_num(parsed)


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _safe_ele_max_hit(stats: dict[str, Any]) -> float | str | None:
    """Compute elemental max hit (`min(Fire, Cold, Lightning)`) safely across `inf`/`NaN`/None."""
    vals: list[float] = []
    for k in ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken"):
        raw_v = stats.get(k)
        if raw_v is None:
            continue
        parsed = _parse_numeric_or_none(raw_v)
        if parsed is not None and not math.isnan(parsed):
            vals.append(parsed)
    if not vals:
        return None
    m = min(vals)
    return m if math.isfinite(m) else fmt_num(m)


def _compact_sweep_summary(digest: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, RFC-8259-safe offence/defence summary from a `capture_digest` dict."""
    stats = digest.get("stats") or {}
    return {
        "primaryDpsKey": digest.get("primaryDpsKey"),
        "primaryDps": digest.get("primaryDps"),
        "fullDps": _json_safe_num(stats.get("FullDPS")),
        "combinedDps": _json_safe_num(stats.get("CombinedDPS")),
        "totalDps": _json_safe_num(stats.get("TotalDPS")),
        "totalDotDps": _json_safe_num(stats.get("TotalDotDPS")),
        "minionCombinedDps": _json_safe_num(stats.get("MinionCombinedDPS")),
        "averageDamage": _json_safe_num(stats.get("AverageDamage")),
        "speed": _json_safe_num(stats.get("Speed")),
        "critChance": _json_safe_num(stats.get("CritChance")),
        "critMultiplier": _json_safe_num(stats.get("CritMultiplier")),
        "hitChance": _json_safe_num(stats.get("HitChance")),
        "manaCost": _json_safe_num(stats.get("ManaCost")),
        "totalEhp": digest.get("totalEhp"),
        "life": digest.get("life"),
        "energyShield": _json_safe_num(stats.get("EnergyShield")),
        "mana": _json_safe_num(stats.get("Mana")),
        "manaUnreserved": _json_safe_num(stats.get("ManaUnreserved")),
        "armour": _json_safe_num(stats.get("Armour")),
        "evasion": _json_safe_num(stats.get("Evasion")),
        "physMaxHit": _json_safe_num(stats.get("PhysicalMaximumHitTaken")),
        "eleMaxHit": _safe_ele_max_hit(stats),
        "fireMaxHit": _json_safe_num(stats.get("FireMaximumHitTaken")),
        "coldMaxHit": _json_safe_num(stats.get("ColdMaximumHitTaken")),
        "lightningMaxHit": _json_safe_num(stats.get("LightningMaximumHitTaken")),
        "chaosMaxHit": _json_safe_num(stats.get("ChaosMaximumHitTaken")),
        "fireResist": _json_safe_num(stats.get("FireResist")),
        "coldResist": _json_safe_num(stats.get("ColdResist")),
        "lightningResist": _json_safe_num(stats.get("LightningResist")),
        "chaosResist": _json_safe_num(stats.get("ChaosResist")),
        "spellSuppression": _json_safe_num(stats.get("EffectiveSpellSuppressionChance")),
        "blockChance": _json_safe_num(stats.get("EffectiveBlockChance")),
        "spellBlockChance": _json_safe_num(stats.get("EffectiveSpellBlockChance")),
        "points": (digest.get("buildInfo") or {}).get("points", {}),
    }


def _extract_compact_calcs(
    worker: BridgeWorker,
    calc_keywords: list[str],
    *,
    limit_per_keyword: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch `get_calcs` ONCE and extract compact matching row values (no breakdown dumps)."""
    has_cjk = any(_CJK_RE.search(str(kw)) for kw in calc_keywords)
    toggled_translate = False
    if has_cjk:
        caps = getattr(worker, "caps", None) or {}
        if caps.get("translateToggle", not caps):
            tracked = getattr(worker, "translate_enabled", None)
            if tracked is None:
                info_dict = getattr(worker, "hello_info", None) or getattr(worker, "info", None) or {}
                if isinstance(info_dict, dict) and "translate" in info_dict:
                    tracked = info_dict.get("translate")
            if tracked is False:
                try:
                    worker.call("set_translate", {"enabled": True})
                    toggled_translate = True
                except Exception:
                    toggled_translate = False
    try:
        calcs = worker.call("get_calcs", {})
    finally:
        if toggled_translate:
            try:
                worker.call("set_translate", {"enabled": False})
            except Exception:
                pass
    rows_index: list[dict[str, Any]] = []
    for sec in calcs.get("sections", []):
        sec_id = str(sec.get("id") or "")
        for sub in sec.get("subsections", []):
            sub_en = strip_pob_escapes(str(sub.get("label") or ""))
            sub_zh = strip_pob_escapes(str(sub.get("labelZh") or sub_en))
            for row in sub.get("rows", []):
                r_en = strip_pob_escapes(str(row.get("label") or ""))
                r_zh = strip_pob_escapes(str(row.get("labelZh") or r_en))
                vals: list[dict[str, Any]] = []
                cell_texts: list[str] = []
                for cell in row.get("cells", []):
                    raw_v = strip_pob_escapes(str(cell.get("raw") or ""))
                    txt_v = strip_pob_escapes(str(cell.get("text") or raw_v))
                    if raw_v or txt_v:
                        vals.append({"raw": raw_v or txt_v, "text": txt_v or raw_v})
                        cell_texts.append(f"{raw_v} {txt_v}")
                hay_row = f"{r_en} {r_zh}".lower()
                hay_sub = f"{sec_id} {sub_en} {sub_zh}".lower()
                hay_full = f"{hay_sub} {hay_row} {' '.join(cell_texts).lower()}"
                rows_index.append(
                    {
                        "sectionId": sec_id,
                        "subsection": sub_en,
                        "subsectionZh": sub_zh,
                        "rowLabel": r_en,
                        "rowLabelZh": r_zh,
                        "values": vals,
                        "_hay_row": hay_row,
                        "_hay_row_compact": re.sub(r"[\s_\-]+", "", hay_row),
                        "_hay_full": hay_full,
                        "_hay_full_compact": re.sub(r"[\s_\-]+", "", hay_full),
                    }
                )

    out: dict[str, list[dict[str, Any]]] = {}
    for kw in calc_keywords:
        q = kw.strip().lower()
        q_compact = re.sub(r"[\s_\-]+", "", q)
        if not q:
            out[kw] = []
            continue
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for pos, r in enumerate(rows_index):
            if q in r["_hay_row"] or (q_compact and q_compact in r["_hay_row_compact"]):
                score = 3
            elif q in r["_hay_full"] or (q_compact and q_compact in r["_hay_full_compact"]):
                score = 1
            else:
                continue
            scored.append((score, pos, r))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out[kw] = [
            {
                "sectionId": r["sectionId"],
                "subsection": r["subsection"],
                "rowLabel": r["rowLabel"],
                "rowLabelZh": r["rowLabelZh"],
                "values": r["values"],
            }
            for _, _, r in scored[:limit_per_keyword]
        ]
    return out


def run_sweep(
    worker: BridgeWorker,
    variants: list[dict[str, Any]],
    *,
    base_ops: list[dict[str, Any]] | None = None,
    calc_keywords: list[str] | str | None = None,
    scenario: str | dict[str, Any] | None = None,
    keep: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """Evaluate N named build variants in ONE warm daemon worker with fault isolation and 0-residue rollback.

    - Captures the original build code once and ALWAYS restores it in `finally`.
    - Applies `base_ops` and/or `scenario` (e.g. `'Pinnacle'`, `'Uber'`, `'FlasksOff'`) ONCE and caches
      `sweep_base_code` so every variant starts from the identical prepared base.
    - Isolates per-variant exceptions into `{"name": ..., "ok": False, "error": ...}` so one bad gem/node
      never aborts the sweep.
    """
    if not variants:
        raise ValueError("Sweep variants list cannot be empty.")

    kw_list: list[str] = []
    if isinstance(calc_keywords, str):
        kw_list = [k.strip() for k in calc_keywords.split(",") if k.strip()]
    elif calc_keywords:
        kw_list = [str(k).strip() for k in calc_keywords if str(k).strip()]
    for kw in kw_list:
        check_input(kw, scope=GuardrailScope.CALC_QUERY, expected_game="poe1")

    merged_keep: list[str] = []
    for tok in _normalize_keep_list(keep):
        if tok not in merged_keep:
            merged_keep.append(tok)
    if isinstance(scenario, dict) and scenario.get("keep") is not None:
        for tok in _normalize_keep_list(scenario.get("keep")):
            if tok not in merged_keep:
                merged_keep.append(tok)
    eff_keep: list[str] | None = merged_keep or None

    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        orig_code = worker.call("export_code", {})["code"]
        results: list[dict[str, Any]] = []
        base_steps: list[dict[str, Any]] = []
        scenario_applied: dict[str, Any] | None = None
        orig_digest: dict[str, Any] | None = None
        restore_check: dict[str, Any] | None = None
        will_mutate_base = bool(base_ops) or (
            scenario is not None and (not isinstance(scenario, str) or scenario.strip())
        )

        try:
            try:
                if will_mutate_base:
                    orig_digest = capture_digest(worker)

                sweep_base_code = orig_code
                mutated_base = False

                if base_ops:
                    base_steps = _execute_batch_ops(worker, base_ops, _BatchCache(worker))
                    mutated_base = True

                if scenario is not None and (not isinstance(scenario, str) or scenario.strip()):
                    from .comparator import _apply_matrix_scenario_in_place, _parse_matrix_scenario

                    plan = _parse_matrix_scenario(scenario)
                    scenario_applied = _call_compat(
                        _apply_matrix_scenario_in_place,
                        worker,
                        plan,
                        keep=eff_keep,
                    )
                    mutated_base = True

                if mutated_base:
                    sweep_base_code = worker.call("export_code", {})["code"]

                base_digest = capture_digest(worker)
                if orig_digest is None:
                    orig_digest = base_digest
                base_calcs = _extract_compact_calcs(worker, kw_list) if kw_list else None

                dirty = False
                for idx, var in enumerate(variants):
                    if not isinstance(var, dict):
                        results.append(
                            {
                                "name": f"Variant {idx + 1}",
                                "ok": False,
                                "error": f"Variant entry #{idx + 1} must be a JSON object.",
                            }
                        )
                        continue

                    v_name = str(var.get("name") or f"Variant {idx + 1}").strip()
                    v_ops = var.get("ops") or []

                    if dirty:
                        worker.call("import_code", {"code": sweep_base_code, "mode": "replace"})
                        dirty = False

                    try:
                        if not isinstance(v_ops, list):
                            raise ValueError(f"Variant '{v_name}' 'ops' must be a list of batch operations.")
                        step_results: list[dict[str, Any]] = []
                        if v_ops:
                            dirty = True
                            step_results = _execute_batch_ops(worker, v_ops, _BatchCache(worker))

                        v_digest = capture_digest(worker)
                        v_diff = diff_digest(base_digest, v_digest)
                        v_calcs = _extract_compact_calcs(worker, kw_list) if kw_list else None

                        entry: dict[str, Any] = {
                            "name": v_name,
                            "ok": True,
                            "steps": step_results,
                            "summary": _compact_sweep_summary(v_digest),
                            "diffVsBase": v_diff,
                            "warnings": v_digest.get("warnings", []),
                        }
                        if v_calcs is not None:
                            entry["calcs"] = v_calcs
                        results.append(entry)
                    except CrossGameHallucinationError:
                        dirty = True
                        raise
                    except Exception as exc:  # noqa: BLE001
                        dirty = True
                        results.append(
                            {
                                "name": v_name,
                                "ok": False,
                                "error": str(exc),
                            }
                        )
            finally:
                worker.call("import_code", {"code": orig_code, "mode": "replace"})

            from .analyzer import verify_restored

            restore_check = verify_restored(worker, orig_code, orig_digest or {})
        finally:
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good

        baseline_payload: dict[str, Any] = {
            "summary": _compact_sweep_summary(base_digest),
            "warnings": base_digest.get("warnings", []),
        }
        if base_calcs is not None:
            baseline_payload["calcs"] = base_calcs

        return {
            "scenario": scenario,
            "scenarioApplied": scenario_applied,
            "baseOpsCount": len(base_ops or []),
            "baseSteps": base_steps,
            "calcKeywords": kw_list,
            "baseline": baseline_payload,
            "variants": results,
            "succeeded": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok")),
            "restoreCheck": restore_check,
        }


# ---------------------------------------------------------------------------
# Trade Batch What-If Evaluation
# ---------------------------------------------------------------------------


def trade_eval_items(
    worker: BridgeWorker,
    *,
    slot_name: str | None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delegate read-only batch trade evaluation to `trade_eval.eval_trade_items`."""
    from .trade_eval import eval_trade_items

    return eval_trade_items(worker, slot_name=slot_name, items=items)
