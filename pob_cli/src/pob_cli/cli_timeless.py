"""`pob timeless` - exact timeless jewel seed search, every seed calculated by headless PoB."""

from __future__ import annotations

import sys
from typing import Any

import click

from . import timeless
from .daemon import BootstrapMissingError, call_daemon
from .formatters import render_table, to_json_str
from .updater import format_stderr_update_alert


def _echo(text: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text, err=err)


def _export_build(target: str) -> dict[str, Any]:
    try:
        return call_daemon("export", {"target": target, "include_xml": True})
    except BootstrapMissingError as exc:
        _echo(format_stderr_update_alert(exc.update_status) or str(exc), err=True)
        sys.exit(1)


def _pct(new: Any, old: Any) -> str:
    if not isinstance(new, (int, float)) or not isinstance(old, (int, float)) or not old:
        return "-"
    return f"{(new / old - 1) * 100:+.1f}%"


@click.group("timeless")
def timeless_group() -> None:
    """Find the best timeless jewel seeds for the loaded build (exact, calculated by PoB)."""


@timeless_group.command("list")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_timeless_list(target: str, as_json: bool) -> None:
    """List timeless jewel types and the build's allocated jewel sockets."""
    info = timeless.describe(_export_build(target)["code"])
    if as_json:
        _echo(to_json_str(info))
        return
    _echo("### Timeless jewels\n" + render_table(
        ["Jewel", "Local Name", "Seeds", "Conquerors", "Search"],
        [[j["name"], j["nameLocal"], j["seeds"], ", ".join(j["conquerors"]),
          "grouped" if j["grouped"] else "every seed"] for j in info["jewels"]],
    ))
    _echo(f"\n### Allocated jewel sockets (`{target}`)\n" + render_table(
        ["--socket", "Near", "Local Name"],
        [[s["nodeId"], s["label"], s["labelLocal"]] for s in info["sockets"]],
    ))


@timeless_group.command("search")
@click.option("--jewel", required=True, help="English name, or the name your PoB shows (see `pob timeless list`).")
@click.option("--socket", type=int, required=True, help="Jewel socket node id (see `pob timeless list`).")
@click.option("--conqueror", help="Conqueror name; only decides the keystone. Default: the first one.")
@click.option("--mod", "mods", multiple=True, help="Extra jewel line, e.g. a Militant Faith devotion mod. Repeatable.")
@click.option("--stat", type=click.Choice(timeless.STAT_KEYS), default="CombinedDPS", show_default=True, help="Rank by.")
@click.option("--top", default=15, show_default=True,
              help="How deep to go: the N best seed groups, plus everything tied with the last of them.")
@click.option("--workers", type=int, help="PoB engines at once, ~1.3 GB RAM each. Default: half the cores, max 4.")
@click.option("--idle", is_flag=True, help="Run at the lowest CPU priority.")
@click.option("--exact", is_flag=True, help="Calculate every group instead of estimating which ones can reach the top.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON (all outcomes).")
def cmd_timeless_search(
    jewel: str,
    socket: int,
    conqueror: str | None,
    mods: tuple[str, ...],
    stat: str,
    top: int,
    workers: int | None,
    idle: bool,
    exact: bool,
    target: str,
    as_json: bool,
) -> None:
    """Find the best seeds of a jewel at a socket; the jewel replaces whatever is socketed there."""
    build = _export_build(target)
    if idle:
        timeless.set_idle_priority()
    try:
        res = timeless.search(
            build["code"], build.get("xml") or "", socket, jewel,
            conqueror=conqueror, mods=mods, stat=stat, top=top, workers=workers, exact=exact,
            progress=None if as_json else lambda msg: _echo(f"  {msg}", err=True),
        )
    except ValueError as exc:
        _echo(str(exc), err=True)
        sys.exit(1)

    if as_json:
        _echo(to_json_str(res))
        return

    base = res["baseline"]
    rows = [["now", f"{base['CombinedDPS']:,.0f}", "", f"{base['TotalEHP']:,.0f}", "", base["Life"], "-", "socket as loaded"]]
    shown = res["outcomes"][:top]
    if res["cutoff"] is not None:  # estimated search: list exactly the outcomes it guarantees to be complete
        shown = [o for o in res["outcomes"] if (o["stats"].get(stat) or 0) >= res["cutoff"]]
    for i, outcome in enumerate(shown, 1):
        s, seeds = outcome["stats"], outcome["seeds"]
        rows.append([
            i, f"{s['CombinedDPS']:,.0f}", _pct(s["CombinedDPS"], base["CombinedDPS"]),
            f"{s['TotalEHP']:,.0f}", _pct(s["TotalEHP"], base["TotalEHP"]), s["Life"], len(seeds),
            ", ".join(map(str, seeds[:6])) + (" ..." if len(seeds) > 6 else ""),
        ])
    check, est = res["selfCheck"], res["estimate"]
    _echo(
        f"### Timeless Search: `{res['jewel']}` ({res['conqueror']}) at socket `{res['socket']}` - "
        f"{res['seedCount']:,} seeds in {res['groupCount']:,} groups, {res['calculated']:,} calculated on "
        f"{res['workers']} engines in {res['seconds']}s, ranked by `{stat}`\n"
        + render_table(["#", "Combined DPS", "vs now", "Total EHP", "vs now", "Life", "Seeds", "Seed numbers"], rows)
    )
    if est:
        err = f"{est['maxError'] * 100:.2f}%" if est["relative"] else f"{est['maxError']:.3g}"
        _echo(
            f"\n> Rows are real PoB results and list every seed at or above the {top}th best group. The other "
            f"{res['groupCount'] - res['calculated']:,} groups were estimated from the calculated ones (error allowed for: "
            f"{err}) and none of them is estimated to reach that level. Use `--exact` to calculate everything."
        )
    if check["mismatches"]:
        _echo(f"\n> [WARN] Seeds {check['mismatches']} differ from their group; please report this build and jewel.")
