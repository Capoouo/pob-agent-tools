"""`pob trade` & `pob bench` — official read-only PoE1 trade search and crafting bench CLI.

All DPS/EHP numbers come strictly from headless PoB (`trade_eval_items` batch RPC
with `item_try` fallback). Never whispers, buys, sends game input, or persists
build mutations.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import click

from . import bench as bench_mod
from .cli import _emit_json, _emit_text, _output_options, _resolve_output_flags
from .daemon import BootstrapMissingError, call_daemon
from .errors import CrossGameHallucinationError
from .external_data import resolve_current_trade_league
from .formatters import emit_json, render_table, render_update_warning
from .guardrails import GuardrailScope, check_input, check_input_item_raw, validate_url
from .importers import _get_cache_dir
from .trade_client import (
    CookieRequired,
    NoResults,
    RateLimited,
    TradeClient,
    TradeError,
)
from .trade_eval import (
    Candidate,
    compare_items,
    format_report,
    parse_weights,
    preferred_templates,
    report_json,
    score_deltas,
    sort_candidates,
    verify_candidates_batch,
)
from .trade_item import (
    build_item_text,
    convert_item_text,
    parse_item_text,
    sanitize_text,
    trade_mod_tiers,
)
from .trade_query import (
    CATEGORY_BY_SLOT,
    OPEN_AFFIX_ID,
    OPEN_PREFIX_ID,
    OPEN_SUFFIX_ID,
    VALID_STATUSES,
    build_query,
    category_for_slot,
    infer_weapon_category_from_base,
    load_trade_stats,
    parse_stat_argument,
    parse_sum_group_argument,
    plan_stats,
    pob_slot_for_slot,
    resolve_stat,
    suggest_stats,
    translate_stat_query,
)
from .updater import format_stderr_update_alert

SLOT_HINT = (
    "helmet, body_armour, gloves, boots, shield, quiver, amulet, belt, "
    "ring1, ring2, ring3, weapon1, weapon2, bow, wand, claw, dagger, "
    "sceptre, staff, flask1..5, jewel"
)

_TARGET_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")


class _TargetSlotType(click.ParamType):
    name = "slot"

    def convert(self, value: Any, param: Any, ctx: Any) -> str:
        s = str(value or "").strip()
        if s in (".", "..") or not _TARGET_SLUG_RE.match(s):
            self.fail(
                f"Invalid build slot slug {value!r}; expected 1-48 chars of [a-zA-Z0-9._-] (not '.' or '..').",
                param,
                ctx,
            )
        return s


_TARGET_SLOT_TYPE = _TargetSlotType()


def _default_target_slot() -> str:
    val = (os.environ.get("POB_SLOT") or "main").strip()
    return val or "main"


def _echo_utf8(text: str) -> None:
    """Write UTF-8 safely to stdout on Windows consoles."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text)


def _err_utf8(text: str) -> None:
    """Write UTF-8 safely to stderr on Windows consoles."""
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text, err=True)


def _trade_error(ctx: click.Context, error: TradeError) -> None:
    _err_utf8(str(error))
    if isinstance(error, CookieRequired):
        ctx.exit(3)
    if isinstance(error, RateLimited):
        ctx.exit(4)
    if isinstance(error, NoResults):
        ctx.exit(2)
    ctx.exit(5)


def _is_known_slot(slot: str | None) -> bool:
    if not slot:
        return False
    norm = slot.strip().lower().replace("-", "_").replace(" ", "_")
    compact = norm.replace("_", "")
    return (
        norm in CATEGORY_BY_SLOT
        or compact in CATEGORY_BY_SLOT
        or pob_slot_for_slot(slot) is not None
    )


def _cached_league_offline() -> str | None:
    cache_file = _get_cache_dir() / "poe_trade_league.json"
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        league = str(data.get("league") or "").strip()
        return league or None
    except Exception:
        return None


def _resolve_league(
    explicit: str | None,
    *,
    offline_only: bool = False,
    proxy: str | None = None,
    client: TradeClient | None = None,
) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    if offline_only:
        return _cached_league_offline() or "Standard"
    try:
        league = resolve_current_trade_league(proxy=proxy)
        if league:
            return league
    except Exception:
        pass
    try:
        if client is not None:
            return client.default_league()
        with TradeClient(proxy=proxy) as fallback_client:
            return fallback_client.default_league()
    except TradeError:
        return "Standard"


def _daemon_call_safe(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Call local PoB daemon, surfacing BootstrapMissingError cleanly."""
    return call_daemon(action, params, auto_start=auto_start, timeout=timeout)


def _build_label_from_info(info: dict[str, Any] | None, target: str) -> str:
    if isinstance(info, dict) and info:
        parts = [
            str(info.get("classNameZh") or info.get("className") or "").strip(),
            str(info.get("ascendClassNameZh") or info.get("ascendClassName") or "").strip(),
        ]
        label = " ".join(part for part in parts if part)
        level = info.get("level")
        if level:
            label = f"{label} (level {level})" if label else f"level {level}"
        if label:
            return label
    return f"slot:{target}"


def _read_current_item(slot: str, target: str) -> tuple[Any, str | None, str | None, str | None]:
    try:
        payload = _daemon_call_safe("items", {"target": target, "slot": slot, "raw": True})
    except CrossGameHallucinationError:
        raise
    except BootstrapMissingError as exc:
        alert = format_stderr_update_alert(exc.update_status) or str(exc)
        return None, sanitize_text(alert, 300), None, None
    except Exception as exc:
        return None, sanitize_text(exc, 300), None, None
    warn = payload.get("updateWarning") if isinstance(payload, dict) else None
    label = _build_label_from_info(payload.get("buildInfo") if isinstance(payload, dict) else None, target)
    raw = payload.get("raw")
    if not raw:
        return None, None, warn, label
    try:
        return parse_item_text(raw), None, warn, label
    except ValueError as exc:
        return None, sanitize_text(exc, 300), warn, label


def _price_of(row: dict[str, Any]) -> tuple[float | None, str | None]:
    price = (row.get("listing") or {}).get("price") or {}
    amount = price.get("amount")
    try:
        parsed_amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        parsed_amount = None
    currency = price.get("currency")
    return parsed_amount, (str(currency) if currency else None)


_PSEUDO_SUM_RE = re.compile(r"^\s*Sum:\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)


def _weighted_sum_of(raw_item: dict[str, Any]) -> float | None:
    """Extract the official PoE1 Trade weighted sum (``pseudoMods: ["Sum: 32"]``) if present."""
    pseudo_mods = raw_item.get("pseudoMods")
    if not isinstance(pseudo_mods, list):
        return None
    for entry in pseudo_mods:
        text = entry.get("description") if isinstance(entry, dict) else str(entry)
        if not text:
            continue
        m = _PSEUDO_SUM_RE.match(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _conversion_payload(plan: Any) -> dict[str, Any]:
    if not plan.conversions:
        return {"enabled": False}
    return {
        "enabled": True,
        "targets": [
            {
                "group": conversion.group,
                "target": conversion.target,
                "elements": conversion.elements,
                "cost": conversion.cost,
                "patch": conversion.patch,
                "rerolled": True,
            }
            for conversion in plan.conversions
        ],
        "note": (
            "Harvest keeps the tier but re-rolls the value inside the tier; "
            "the listing value is a best case, not a guarantee."
        ),
    }


def _prepend_warning(body: str, update_warning: str | None) -> str:
    banner = render_update_warning(update_warning)
    return f"{banner}{body}" if banner else body


# ---------------------------------------------------------------------------
# `pob trade` CLI group
# ---------------------------------------------------------------------------


@click.group("trade")
def trade_group() -> None:
    """Search official PoE1 trade (read-only), preview Harvest swaps, and verify in PoB."""


@trade_group.command("stats")
@click.argument("text")
@click.option("--slot", help=f"Optional slot for (Local)/(Shields) routing ({SLOT_HINT}).")
@click.option("--limit", default=5, show_default=True, help="Maximum suggestions to show.")
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (overrides POB_PROXY).")
@click.option("--refresh", is_flag=True, help="Force refresh remote trade stats cache.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_trade_stats(
    text: str,
    slot: str | None,
    limit: int,
    proxy: str | None,
    refresh: bool,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Resolve an English or Chinese modifier to an official PoE1 trade stat ID."""
    ctx = click.get_current_context()
    validate_url(text, expected_game="poe1", cli_subcommand="trade stats")
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(text, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    with TradeClient(proxy=proxy) as client:
        try:
            entries, source = load_trade_stats(
                remote_fetcher=lambda: client.stats(force_refresh=refresh),
                force_refresh=refresh,
            )
        except TradeError as exc:
            _trade_error(ctx, exc)
            return
        except Exception as exc:
            _err_utf8(str(exc))
            ctx.exit(5)
            return

    match = resolve_stat(text, entries, slot=slot)
    suggestions = suggest_stats(text, entries, limit=limit, slot=slot)
    if as_json:
        payload = {
            "query": text,
            "slot": slot,
            "source": source,
            "match": (
                {"id": match.id, "text": match.text, "score": match.score} if match else None
            ),
            "suggestions": [
                {"id": item.id, "text": item.text, "score": item.score} for item in suggestions
            ],
        }
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
    else:
        from .guardrails import get_active_warnings

        lines: list[str] = [w.format_markdown() for w in get_active_warnings()]
        if match:
            lines.append(
                f"Resolved (`{source}`): `{match.text}` -> `{match.id}` (score {match.score:.3f})"
            )
        else:
            lines.append(f"No confident match for `{text}` (source: `{source}`).")
        if suggestions:
            lines.append(
                render_table(
                    ["Text", "Stat ID", "Score"],
                    [[item.text, item.id, f"{item.score:.3f}"] for item in suggestions],
                )
            )
        _emit_text("\n".join(lines), out_path=out_path)
    if match is None:
        ctx.exit(1)


@trade_group.command("search")
@click.option("--slot", help=f"Equipment slot to upgrade ({SLOT_HINT}).")
@click.option(
    "--stat",
    "stats",
    multiple=True,
    help='Hard filter (EN or 中文), e.g. "to Cold Resistance=40" or "冰霜抗性=40".',
)
@click.option(
    "--sum",
    "sum_groups",
    multiple=True,
    help=(
        'Weighted Sum group ("stat1[*w1]|stat2[*w2]|...[>=min]", EN or 中文). '
        "Groups stackable numeric mods so the trade site displays Sum: XX for one-click sorting."
    ),
)
@click.option("--want", "wants", multiple=True, help="Soft preference (EN or 中文, scoring only).")
@click.option("--weight", "weights", multiple=True, help="Override scoring weight, e.g. resist=1.2.")
@click.option("--budget", type=float, help="Maximum price cap.")
@click.option(
    "--currency",
    default="chaos_equiv",
    show_default=True,
    help="Budget currency: chaos_equiv (GGG auto equivalent), chaos, divine, etc.",
)
@click.option(
    "--status",
    type=click.Choice(list(VALID_STATUSES)),
    default="available",
    show_default=True,
    help="Trade listing availability (available = instant buyout + online).",
)
@click.option("--league", help="League name (defaults to current softcore trade league).")
@click.option("--rarity", default="rare", show_default=True, help="Rarity filter (any to disable).")
@click.option("--ilvl-min", type=int, default=0, show_default=True, help="Minimum item level.")
@click.option("--category", help="Override trade category, e.g. weapon.bow, armour.gloves.")
@click.option("--no-category", is_flag=True, help="Disable item category filter.")
@click.option(
    "--allow-conversion",
    is_flag=True,
    help="Expand Harvest-convertible single-element stats and exclude corrupted/mirrored.",
)
@click.option(
    "--convert-to",
    type=click.Choice(["fire", "cold", "lightning"]),
    help="Target element when --allow-conversion is enabled.",
)
@click.option(
    "--allow-corrupted",
    is_flag=True,
    help="Do not auto-exclude corrupted/mirrored items when --allow-conversion is set.",
)
@click.option(
    "--search-sort",
    type=click.Choice(["asc", "desc", "sum"]),
    default="asc",
    show_default=True,
    help="Trade API sort order: price asc/desc, or sum (primary --sum weighted group desc).",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["score", "price", "pob", "sum"]),
    default="score",
    show_default=True,
)
@click.option("--max-results", type=int, default=8, show_default=True, help="Rows in report.")
@click.option(
    "--fetch-limit",
    type=int,
    default=10,
    show_default=True,
    help="Listings to fetch from API (max 20).",
)
@click.option("--open-prefix", is_flag=True, help="Require at least one open prefix.")
@click.option("--open-suffix", is_flag=True, help="Require at least one open suffix.")
@click.option("--open-affix", is_flag=True, help="Require at least one open prefix or suffix.")
@click.option("--bench-craft", "bench_crafts", multiple=True,
              help='Try adding/replacing a bench craft, e.g. "Area Damage" (repeatable; requires PoB).')
@click.option(
    "--bench-hint/--no-bench-hint",
    default=True,
    show_default=True,
    help="Include matching crafting bench options for the slot.",
)
@click.option(
    "--pob",
    "pob_mode",
    type=click.Choice(["auto", "on", "off"]),
    default="auto",
    show_default=True,
    help="Verify top candidates via headless PoB.",
)
@click.option("--pob-limit", type=int, default=5, show_default=True, help="Candidates to PoB-verify.")
@click.option(
    "--target",
    type=_TARGET_SLOT_TYPE,
    default=_default_target_slot,
    help="Target build slot slug (defaults to POB_SLOT or 'main').",
)
@click.option(
    "--current-item-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Compare against item text file instead of daemon equipped slot.",
)
@click.option("--no-compare", is_flag=True, help="Skip reading the currently equipped item.")
@click.option("--dump-query", is_flag=True, help="Print official trade query JSON offline and exit.")
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (overrides POB_PROXY).")
@click.option("--json", "as_json", is_flag=True, help="Output RFC-8259 JSON.")
@_output_options
@click.option("--verbose", is_flag=True, help="Log HTTP request paths to stderr (never cookies).")
def cmd_trade_search(
    slot: str | None,
    stats: tuple[str, ...],
    sum_groups: tuple[str, ...],
    wants: tuple[str, ...],
    weights: tuple[str, ...],
    budget: float | None,
    currency: str,
    status: str,
    league: str | None,
    rarity: str,
    ilvl_min: int,
    category: str | None,
    no_category: bool,
    allow_conversion: bool,
    convert_to: str | None,
    allow_corrupted: bool,
    search_sort: str,
    sort_by: str,
    max_results: int,
    fetch_limit: int,
    open_prefix: bool,
    open_suffix: bool,
    open_affix: bool,
    bench_crafts: tuple[str, ...],
    bench_hint: bool,
    pob_mode: str,
    pob_limit: int,
    target: str,
    current_item_file: str | None,
    no_compare: bool,
    dump_query: bool,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
    verbose: bool,
) -> None:
    """Search official PoE1 trade listings, compare against equipped gear, and verify in PoB."""
    ctx = click.get_current_context()
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    if category:
        check_input(category, scope=GuardrailScope.CATEGORY, expected_game="poe1")
    for s_val in stats:
        validate_url(s_val, expected_game="poe1", cli_subcommand="trade search")
        check_input(s_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    for sg_val in sum_groups:
        validate_url(sg_val, expected_game="poe1", cli_subcommand="trade search")
        sg_body = re.split(r"(?:>=|<=|=)", sg_val, maxsplit=1)[0]
        for part in sg_body.split("|"):
            if part.strip():
                check_input(part.strip(), scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    for w_val in wants:
        validate_url(w_val, expected_game="poe1", cli_subcommand="trade search")
        check_input(w_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    for bc_val in bench_crafts:
        validate_url(bc_val, expected_game="poe1", cli_subcommand="trade search")
        check_input(bc_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    if bench_crafts and (pob_mode == "off" or not slot or no_compare or current_item_file):
        raise click.UsageError("--bench-craft requires --slot, PoB verification and the loaded build comparison.")
    if bench_crafts and pob_limit <= 0:
        raise click.UsageError("--bench-craft requires --pob-limit greater than zero.")
    if bench_crafts and ctx.get_parameter_source("sort_by") == click.core.ParameterSource.DEFAULT:
        sort_by = "pob"
    if not slot and not category and not no_category:
        _err_utf8("Provide --slot (or --category / --no-category).")
        ctx.exit(1)
        return

    if slot and not _is_known_slot(slot) and not category and not no_category:
        _err_utf8(f"Unknown slot {slot!r}; pass a known --slot, --category, or --no-category.")
        ctx.exit(1)
        return

    try:
        weight_map = parse_weights(weights)
        translated_wants = [translate_stat_query(w) for w in wants]
        preferred = preferred_templates(translated_wants)
        requests = [parse_stat_argument(value) for value in stats]
        weighted_groups = [parse_sum_group_argument(value) for value in sum_groups]
    except ValueError as exc:
        _err_utf8(str(exc))
        ctx.exit(1)
        return

    # Load current item early (when not --dump-query) so weapon1/weapon2 can infer exact category
    current = None
    current_error: str | None = None
    update_warning: str | None = None
    build_label = f"slot:{target}"
    pob_slot = pob_slot_for_slot(slot or "") if slot else None

    if not dump_query:
        if current_item_file:
            try:
                current = parse_item_text(
                    Path(current_item_file).read_text(encoding="utf-8", errors="replace")
                )
            except ValueError as exc:
                current_error = sanitize_text(exc, 300)
        elif not no_compare:
            if pob_slot:
                current, current_error, update_warning, item_label = _read_current_item(pob_slot, target)
                if item_label:
                    build_label = item_label
            elif slot:
                current_error = f"no PoB slot mapping for {slot!r}"

    resolved_category: str | None = None
    if not no_category:
        resolved_category = category or (category_for_slot(slot) if slot else None)
        if resolved_category is None and slot and current is not None and current.base_type:
            resolved_category = infer_weapon_category_from_base(current.base_type)

    with TradeClient(
        proxy=proxy,
        logger=(lambda line: _err_utf8(line)) if verbose else None,
    ) as client:
        league_name = _resolve_league(league, offline_only=dump_query, proxy=proxy, client=client)

        try:
            entries, stats_source = load_trade_stats(
                remote_fetcher=None if dump_query else client.stats,
            )
        except TradeError as exc:
            _trade_error(ctx, exc)
            return
        except Exception as exc:
            if dump_query:
                # If local TradeSiteStats.lua is missing (e.g. CI), allow remote fetch only if explicitly needed
                try:
                    entries, stats_source = load_trade_stats(remote_fetcher=client.stats)
                except TradeError as trade_exc:
                    _trade_error(ctx, trade_exc)
                    return
                except Exception as inner_exc:
                    _err_utf8(str(inner_exc))
                    ctx.exit(5)
                    return
            else:
                _err_utf8(str(exc))
                ctx.exit(5)
                return

        pseudo_filters: list[dict[str, Any]] = []
        if open_prefix:
            pseudo_filters.append({"id": OPEN_PREFIX_ID, "value": {"min": 1}, "disabled": False})
        if open_suffix:
            pseudo_filters.append({"id": OPEN_SUFFIX_ID, "value": {"min": 1}, "disabled": False})
        if open_affix and not (open_prefix or open_suffix):
            pseudo_filters.append({"id": OPEN_AFFIX_ID, "value": {"min": 1}, "disabled": False})
        open_flags = {
            "prefix": bool(open_prefix),
            "suffix": bool(open_suffix),
            "any": bool(open_affix and not (open_prefix or open_suffix)),
        }

        bench_options: list[dict[str, Any]] = []
        if bench_hint and slot:
            seen_ids: set[str] = set()
            affix_filter = (
                "prefix"
                if open_prefix and not open_suffix
                else ("suffix" if open_suffix and not open_prefix else None)
            )
            for text in [req.text for req in requests] + list(wants):
                for result in bench_mod.search(slot, text, limit=2, affix=affix_filter):
                    recipe = result.recipe
                    if recipe.id in seen_ids:
                        continue
                    seen_ids.add(recipe.id)
                    bench_options.append(recipe.as_dict())

        try:
            plan = plan_stats(
                requests,
                entries,
                slot=slot,
                allow_conversion=allow_conversion,
                convert_to=convert_to,
                rarity=rarity,
                weighted_groups=weighted_groups,
            )
            query = build_query(
                plan=plan,
                category=resolved_category,
                rarity=rarity,
                item_level_min=ilvl_min,
                status=status,
                currency=currency,
                budget=budget,
                search_sort=search_sort,
                pseudo_filters=pseudo_filters or None,
                allow_corrupted=allow_corrupted,
            )
        except ValueError as exc:
            _err_utf8(str(exc))
            ctx.exit(1)
            return

        if dump_query:
            payload = {
                "league": league_name,
                "slot": slot,
                "category": resolved_category,
                "statsSource": stats_source,
                "conversion": _conversion_payload(plan),
                "openAffixes": open_flags,
                "benchOptions": bench_options,
                "query": query,
            }
            _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
            return

        try:
            search_res = client.search(league_name, query)
        except TradeError as exc:
            _trade_error(ctx, exc)
            return

        query_id = str(search_res.get("id") or "")
        ids = [str(item) for item in search_res.get("result") or []]
        total = int(search_res.get("total") or len(ids))

        try:
            rows = client.fetch_many(ids, query_id, limit=max(0, min(fetch_limit, 20)))
        except TradeError as exc:
            _trade_error(ctx, exc)
            return

        search_url = client.search_url(league_name, query_id) if query_id else ""

    candidates: list[Candidate] = []
    for row in rows:
        raw_item = row.get("item") or {}
        try:
            text = build_item_text(raw_item)
        except ValueError:
            continue

        conversions = []
        conversion_warnings: list[str] = []
        if plan.conversions:
            tiers = trade_mod_tiers(raw_item)
            for conv_plan in plan.conversions:
                converted = convert_item_text(
                    text,
                    conv_plan.target,
                    group=conv_plan.group,
                    tiers=tiers,
                )
                text = converted.text
                conversions.extend(converted.conversions)
                conversion_warnings.extend(converted.warnings)

        try:
            item = parse_item_text(text)
        except ValueError:
            continue

        deltas = compare_items(current, item, weights=weight_map, preferred=preferred)
        amount, price_currency = _price_of(row)
        weighted_sum = _weighted_sum_of(raw_item)
        candidates.append(
            Candidate(
                raw=row,
                item=item,
                item_text=text,
                price_amount=amount,
                price_currency=price_currency,
                score=score_deltas(deltas),
                deltas=deltas,
                conversions=conversions,
                conversion_warnings=conversion_warnings,
                url=search_url,
                listing_id=row.get("id"),
                weighted_sum=weighted_sum,
            )
        )

    pre_sort = "sum" if (sort_by == "sum" or (search_sort == "sum" and sort_by != "price")) else "score"
    candidates = sort_candidates(candidates, pre_sort)

    verification_limit = max(0, pob_limit)
    if bench_crafts and not dump_query:
        from .trade_craft import expand_bench_candidates
        selected = expand_bench_candidates(candidates[:verification_limit], slot=slot, wants=bench_crafts)
        for candidate in selected:
            if candidate.bench_craft:
                candidate.deltas = compare_items(current, candidate.item, weights=weight_map, preferred=preferred)
                candidate.score = score_deltas(candidate.deltas)
        candidates = selected + candidates[verification_limit:]
        verification_limit = len(selected)

    if pob_mode != "off" and candidates:
        if not pob_slot:
            if pob_mode == "on":
                _err_utf8(f"PoB verification needs a supported --slot, got {slot!r}.")
                ctx.exit(6)
                return
            _err_utf8("PoB verification skipped: no PoB slot mapping for this search.")
        else:
            try:
                verify_candidates_batch(
                    candidates,
                    slot=pob_slot,
                    target=target,
                    limit=verification_limit,
                    daemon_call=_daemon_call_safe,
                )
            except CrossGameHallucinationError:
                raise
            except BootstrapMissingError as exc:
                alert = format_stderr_update_alert(exc.update_status) or str(exc)
                _err_utf8(alert)
                if pob_mode == "on":
                    ctx.exit(6)
                    return
            except Exception as exc:
                err_text = sanitize_text(exc, 300)
                for c in candidates[:verification_limit]:
                    if c.pob is None and not c.pob_error:
                        c.pob_error = err_text
                if pob_mode == "on":
                    _err_utf8(f"PoB verification failed: {err_text}")
                    ctx.exit(6)
                    return

            checked = candidates[:verification_limit]
            if pob_mode == "on" and checked:
                failed = [c for c in checked if c.pob is None]
                if failed:
                    _err_utf8(
                        f"PoB verification failed: {failed[0].pob_error or 'unknown error'}"
                    )
                    ctx.exit(6)
                    return
            elif pob_mode == "auto" and checked and all(c.pob is None for c in checked):
                _err_utf8(
                    "PoB verification unavailable (daemon/PoB not usable); "
                    "report only shows modifier deltas, not DPS."
                )

    ordered = sort_candidates(candidates, sort_by)[: max(0, max_results)]
    conversion_note = ""
    if plan.conversions:
        parts = []
        for conv_plan in plan.conversions:
            elements = "/".join(conv_plan.elements)
            parts.append(
                f"harvest {elements} -> {conv_plan.target} "
                f"(cost: {conv_plan.cost}; {conv_plan.patch} rules; "
                "tier kept, roll re-rolled)"
            )
        conversion_note = "; ".join(parts)

    if as_json:
        payload: dict[str, Any] = {
            "league": league_name,
            "slot": slot,
            "category": resolved_category,
            "status": status,
            "currency": currency,
            "statsSource": stats_source,
            "query": query,
            "searchUrl": search_url,
            "total": total,
            "fetched": len(rows),
            "conversion": _conversion_payload(plan),
            "openAffixes": open_flags,
            "benchOptions": bench_options,
            "current": {
                "found": current is not None,
                "error": current_error,
                "name": current.display_name if current else None,
            },
            "candidates": report_json(ordered),
        }
        if update_warning:
            payload["updateWarning"] = update_warning
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
        return

    body = format_report(
        build_label=build_label,
        slot=slot or resolved_category or "",
        current=current,
        candidates=ordered,
        league=league_name,
        status=status,
        budget=budget,
        currency=currency,
        search_url=search_url,
        total=total,
        fetched=len(rows),
        conversion_note=conversion_note,
        open_affixes=open_flags,
        bench_options=bench_options,
    )
    _emit_text(_prepend_warning(body, update_warning), out_path=out_path)
    if current_error:
        _err_utf8(f"Equipped item could not be read: {current_error}")


@trade_group.command("convert")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="Item text file.")
@click.option("--text", "raw_text", help="Item text (PoB clipboard format).")
@click.option(
    "--to",
    "target",
    required=True,
    type=click.Choice(["fire", "cold", "lightning"]),
    help="Target element for Harvest conversion.",
)
@click.option("--out", "out_path", type=click.Path(dir_okay=False), help="Write converted text to file.")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass unique/corrupted/mirrored item-level guards (what-if preview only).",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.option(
    "--compact",
    is_flag=True,
    default=False,
    help="Emit compact single-line JSON (when --json is used).",
)
def cmd_trade_convert(
    file_path: str | None,
    raw_text: str | None,
    target: str,
    out_path: str | None,
    force: bool,
    as_json: bool,
    compact: bool,
) -> None:
    """Preview PoE1 3.30 Harvest same-tier element conversion on item text."""
    ctx = click.get_current_context()
    if file_path:
        raw_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    if not raw_text:
        _err_utf8("Provide --text or --file.")
        ctx.exit(1)
        return
    check_input_item_raw(raw_text, expected_game="poe1")

    try:
        converted = convert_item_text(raw_text, target, force=force)
    except ValueError as exc:
        _err_utf8(str(exc))
        ctx.exit(1)
        return

    if out_path:
        out_file = Path(out_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(converted.text + "\n", encoding="utf-8", newline="\n")

    if as_json:
        payload = {
            "target": target,
            "text": converted.text,
            "conversions": [c.as_dict() for c in converted.conversions],
            "warnings": list(converted.warnings),
            "skipped": list(converted.skipped),
        }
        eff_compact, root_out = _resolve_output_flags(compact=compact, out_path=None)
        json_out = None if out_path else root_out
        text = emit_json(payload, compact=eff_compact, out=json_out, sanitize=True)
        if json_out is not None:
            _echo_utf8(f"Saved JSON to `{json_out}` ({len(text.encode('utf-8')):,} bytes).")
        return

    lines = [
        f"### Converted item text (target: {target})",
        "```text",
        converted.text,
        "```",
    ]
    if converted.conversions:
        lines.append("\nConverted modifiers:")
        for c in converted.conversions:
            tier_info = f"tier {c.tier}; " if c.tier else ""
            lines.append(
                f"- {c.template} -> {c.converted_template} "
                f"(cost: {c.cost}; {tier_info}{c.patch} rules; same tier, roll re-rolled)"
            )
    else:
        lines.append("\nNo convertible modifiers were found.")
    if converted.warnings:
        lines.append("\nWarnings:")
        for w in converted.warnings:
            lines.append(f"- {w}")
    if converted.skipped:
        lines.append("\nSkipped:")
        for reason in converted.skipped:
            lines.append(f"- {reason}")
    lines.append(
        "\n> Harvest keeps the tier but re-rolls the value inside that tier. "
        "Listed values are best-case references; verify with `pob item try --slot <slot>`."
    )
    rendered = "\n".join(lines)
    if out_path:
        _echo_utf8(rendered)
    else:
        _emit_text(rendered)


# ---------------------------------------------------------------------------
# `pob bench` CLI group
# ---------------------------------------------------------------------------


@click.group("bench")
def bench_group() -> None:
    """Look up PoE1 crafting bench modifiers (ModMaster.lua authoritative + cost overlay)."""


@bench_group.command("list")
@click.option("--slot", required=True, help=f"Equipment slot ({SLOT_HINT}).")
@click.option("--search", "text", help="Filter by English or Chinese modifier text.")
@click.option("--affix", type=click.Choice(["prefix", "suffix"]), help="Filter by affix type.")
@click.option("--limit", default=25, show_default=True, help="Maximum rows to show.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_bench_list(
    slot: str,
    text: str | None,
    affix: str | None,
    limit: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """List crafting bench modifiers available for a slot."""
    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    if text:
        check_input(text, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
        results = bench_mod.search(slot, text, limit=limit, affix=affix)
        items = [r.recipe for r in results]
    else:
        items = bench_mod.list_for_slot(slot, affix=affix)[: max(0, limit)]

    meta = bench_mod.source_metadata()
    if as_json:
        payload = {
            "slot": slot,
            "slotKey": bench_mod.slot_key(slot),
            "count": len(items),
            "source": meta,
            "recipes": [recipe.as_dict() for recipe in items],
        }
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
        return

    if not items:
        _emit_text(f"No bench modifiers matched for `{slot}`.", out_path=out_path)
        return

    rows = [
        [
            recipe.affix,
            recipe.text,
            recipe.text_zh or "-",
            recipe.roll_text or "-",
            recipe.cost,
            recipe.unlock or "-",
        ]
        for recipe in items
    ]
    _emit_text(
        render_table(
            ["Affix", "Modifier", "中文", "Roll", "Cost", "Unlock"],
            rows,
        ),
        out_path=out_path,
    )


@bench_group.command("suggest")
@click.option("--slot", required=True, help=f"Equipment slot ({SLOT_HINT}).")
@click.option("--stat", "text", required=True, help="Desired modifier (EN or 中文).")
@click.option("--affix", type=click.Choice(["prefix", "suffix"]), help="Filter by affix type.")
@click.option("--limit", default=5, show_default=True, help="Maximum suggestions.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_bench_suggest(
    slot: str,
    text: str,
    affix: str | None,
    limit: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Suggest the strongest crafting bench craft for a desired modifier."""
    ctx = click.get_current_context()
    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(text, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    results = bench_mod.search(slot, text, limit=limit, affix=affix)
    if not results:
        _emit_text(f"No bench modifier for `{slot}` matched `{text}`.", out_path=out_path)
        ctx.exit(1)
        return

    best_recipe = results[0].recipe
    meta = bench_mod.source_metadata()
    if as_json:
        payload = {
            "slot": slot,
            "slotKey": bench_mod.slot_key(slot),
            "query": text,
            "best": best_recipe.as_dict(),
            "alternatives": [r.recipe.as_dict() for r in results[1:]],
            "source": meta,
        }
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
        return

    zh_part = f" ({best_recipe.text_zh})" if best_recipe.text_zh and best_recipe.text_zh != best_recipe.text else ""
    lines = [
        f"Best bench craft for `{slot}` / `{text}` (source: `{meta.get('mode', 'unknown')}`):",
        f"- affix: {best_recipe.affix}",
        f"- modifier: {best_recipe.text}{zh_part}",
        f"- roll: {best_recipe.roll_text or '-'} (minimum {best_recipe.min_value:g} is the safe floor)",
        f"- crafted line (min): `{' | '.join(best_recipe.crafted_lines(use_min=True))}`",
        f"- cost: {best_recipe.cost}",
    ]
    if best_recipe.unlock:
        lines.append(f"- unlock: {best_recipe.unlock}")
    lines.append(
        f"- note: bench mods roll lower than high-tier natural mods; requires an open {best_recipe.affix}. "
        "Append the `{crafted}` line and verify with `pob item try`."
    )
    if len(results) > 1:
        lines.append("\nAlternatives:")
        for r in results[1:]:
            rec = r.recipe
            lines.append(f"- [{rec.affix}] {rec.text} ({rec.roll_text or '-'}; {rec.cost})")
    _emit_text("\n".join(lines), out_path=out_path)
