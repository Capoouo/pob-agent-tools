"""Click CLI entry point for `pob` (lazy-loaded for fast cold startup)."""

from __future__ import annotations

import os
import re
import sys
from typing import TYPE_CHECKING, Any

import click

from . import __version__

if TYPE_CHECKING:
    from pathlib import Path

_SLOT_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")
_GEM_SPEC_RE = re.compile(r"^(.*?):(\d+)(?::(\d+))?$")

_LAZY_SUBCOMMANDS: dict[str, tuple[str, str, str]] = {
    "trade": (
        ".cli_trade",
        "trade_group",
        "Search official PoE1 trade (read-only), preview Harvest swaps, and verify in PoB.",
    ),
    "bench": (
        ".cli_trade",
        "bench_group",
        "Look up PoE1 crafting bench modifiers (ModMaster.lua authoritative + cost overlay).",
    ),
    "timeless": (
        ".cli_timeless",
        "timeless_group",
        "Find optimal timeless jewel seeds verified by headless PoB.",
    ),
}


class _SlotSlugType(click.ParamType):
    name = "slot"

    def convert(self, value: Any, param: Any, ctx: Any) -> str:
        s = str(value or "").strip()
        if s in (".", "..") or not _SLOT_SLUG_RE.match(s):
            self.fail(
                f"Invalid slot slug {value!r}; expected 1-48 chars matching [a-zA-Z0-9._-] (not '.' or '..').",
                param,
                ctx,
            )
        return s


_SLOT_SLUG_TYPE = _SlotSlugType()


def _default_target() -> str:
    val = (os.environ.get("POB_SLOT") or "main").strip()
    return val or "main"


class LazyGroup(click.Group):
    """Click Group that defers importing heavy subcommand groups until invoked."""

    def __init__(self, *args: Any, lazy_subcommands: dict[str, tuple[str, str, str]] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lazy_subcommands: dict[str, tuple[str, str, str]] = dict(lazy_subcommands or {})

    def list_commands(self, ctx: click.Context) -> list[str]:
        base = set(super().list_commands(ctx))
        base.update(self._lazy_subcommands.keys())
        return sorted(base)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        spec = self._lazy_subcommands.get(cmd_name)
        if spec is None:
            return None
        mod_path, attr_name, _ = spec
        import importlib

        mod = importlib.import_module(mod_path, __package__)
        loaded = getattr(mod, attr_name)
        self.commands[cmd_name] = loaded
        return loaded

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        commands: list[tuple[str, str]] = []
        for subcommand in self.list_commands(ctx):
            if subcommand in self._lazy_subcommands and subcommand not in self.commands:
                commands.append((subcommand, self._lazy_subcommands[subcommand][2]))
                continue
            cmd = super().get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            commands.append((subcommand, cmd.get_short_help_str(limit=formatter.width)))

        if commands:
            limit = formatter.width - 6 - max(len(c[0]) for c in commands)
            rows = []
            for subcommand, help_text in commands:
                if len(help_text) > limit > 3:
                    help_text = help_text[: limit - 3] + "..."
                rows.append((subcommand, help_text))
            with formatter.section("Commands"):
                formatter.write_dl(rows)


def _ctx_wants_json(ctx: click.Context | None) -> bool:
    """Return True if the active CLI invocation requested `--json` (works in Click CliRunner and real argv)."""
    if ctx is not None:
        if ctx.params.get("as_json"):
            return True
        root = ctx.find_root()
        raw_args = root.meta.get("pob_raw_args")
        if isinstance(raw_args, (list, tuple)) and "--json" in raw_args:
            return True
    return "--json" in sys.argv[1:]


def _augment_click_usage_error(exc: click.UsageError, fallback_ctx: click.Context | None = None) -> None:
    """Append actionable `Did you mean` / `Suggested command` hints onto Click UsageError/NoSuchOption."""
    import difflib

    msg = str(exc.message or "")
    if "Did you mean" in msg or "Suggested command" in msg:
        return
    ctx = exc.ctx or fallback_ctx
    cmd = ctx.command if ctx is not None else None
    cmd_path = ctx.command_path if ctx is not None else "pob"

    if isinstance(exc, click.NoSuchOption):
        bad_opt = str(exc.option_name or "")
        all_opts: list[str] = []
        if cmd is not None:
            for p in getattr(cmd, "params", ()):
                all_opts.extend(getattr(p, "opts", ()))
                all_opts.extend(getattr(p, "secondary_opts", ()))
        all_opts = sorted(set(o for o in all_opts if o.startswith("-")))
        matches = difflib.get_close_matches(bad_opt, all_opts, n=3, cutoff=0.45)
        if matches:
            exc.message = f"{msg}\nDid you mean: {', '.join(matches)}? (Run `{cmd_path} --help` for valid options.)"
        else:
            exc.message = f"{msg}\nSuggested command: `{cmd_path} --help`"
        return

    # Generic UsageError: if missing required option or unknown subcommand, add help pointer
    if "No such command" in msg and isinstance(cmd, click.Group) and ctx is not None:
        m = re.search(r"No such command ['\"]([^'\"]+)['\"]", msg)
        if m:
            bad_cmd = m.group(1)
            known_cmds = cmd.list_commands(ctx)
            matches = difflib.get_close_matches(bad_cmd, known_cmds, n=3, cutoff=0.45)
            if matches:
                exc.message = (
                    f"{msg}\nDid you mean: {', '.join(matches)}? "
                    f"Suggested command: `{cmd_path} {matches[0]} --help`"
                )
                return
    if cmd_path:
        exc.message = f"{msg}\nSuggested command: `{cmd_path} --help`"


class GuardrailGroup(LazyGroup):
    """LazyGroup catching CrossGameHallucinationError / LegacyPatchTrapError (-> exit 1) & enriching Click errors."""

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            _augment_click_usage_error(exc, fallback_ctx=ctx)
            raise

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        raw_args_copy = list(args)
        try:
            ctx = super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as exc:
            _augment_click_usage_error(exc, fallback_ctx=parent)
            raise
        ctx.meta["pob_raw_args"] = raw_args_copy
        return ctx

    def invoke(self, ctx: click.Context) -> Any:
        from .errors import CrossGameHallucinationError, LegacyPatchTrapError
        from .guardrails import reset_cli_warnings, warning_context

        reset_cli_warnings()
        with warning_context():
            try:
                return super().invoke(ctx)
            except (CrossGameHallucinationError, LegacyPatchTrapError) as exc:
                as_json = _ctx_wants_json(ctx)
                _echo_stderr_utf8(exc.format_human())
                if as_json:
                    raw_args = ctx.find_root().meta.get("pob_raw_args") or ()
                    eff_compact, eff_out = _resolve_output_flags(compact="--compact" in raw_args)
                    from .formatters import emit_json

                    emit_json(exc.to_dict(), compact=eff_compact, out=eff_out, sanitize=False)
                ctx.exit(1)
            except click.UsageError as exc:
                _augment_click_usage_error(exc, fallback_ctx=ctx)
                raise


def _target_option(*, allow_slot_alias: bool = True) -> Any:
    """Attach named multi-slot `--target` (plus `--slot` alias when non-conflicting)."""
    decls = ("--target", "--slot", "target") if allow_slot_alias else ("--target", "target")
    return click.option(
        *decls,
        type=_SLOT_SLUG_TYPE,
        default=_default_target,
        help="Target build slot slug (defaults to POB_SLOT or 'main').",
    )


def _output_options(fn: Any) -> Any:
    """Attach `--compact` and `--out` output options to a Click command."""
    fn = click.option(
        "--out",
        "out_path",
        type=click.Path(dir_okay=False),
        default=None,
        help="Write output directly to UTF-8 (no BOM) file.",
    )(fn)
    fn = click.option(
        "--compact",
        is_flag=True,
        default=False,
        help="Emit compact single-line JSON (when --json is used).",
    )(fn)
    return fn


def _resolve_output_flags(compact: bool = False, out_path: str | Path | None = None) -> tuple[bool, Path | None]:
    ctx = click.get_current_context(silent=True)
    root_obj = (ctx.find_root().obj if ctx is not None else None) or {}
    eff_compact = bool(compact or root_obj.get("compact"))
    eff_out_raw = out_path if out_path is not None else root_obj.get("out")
    if eff_out_raw is None:
        return eff_compact, None
    from pathlib import Path

    return eff_compact, Path(eff_out_raw)


def _echo_utf8(text: str) -> None:
    """Write UTF-8 safely to stdout on Windows consoles."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text)


def _echo_stderr_utf8(text: str) -> None:
    """Write UTF-8 safely to stderr on Windows consoles."""
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text, err=True)


def _emit_json(
    data: Any,
    *,
    compact: bool = False,
    out_path: str | Path | None = None,
    sanitize: bool = True,
) -> None:
    from .formatters import emit_json

    eff_compact, eff_out = _resolve_output_flags(compact=compact, out_path=out_path)
    text = emit_json(data, compact=eff_compact, out=eff_out, sanitize=sanitize)
    if eff_out is not None:
        _echo_utf8(f"Saved JSON to `{eff_out}` ({len(text.encode('utf-8')):,} bytes).")


def _emit_text(text: str, *, out_path: str | Path | None = None) -> None:
    _, eff_out = _resolve_output_flags(out_path=out_path)
    if eff_out is not None:
        eff_out.parent.mkdir(parents=True, exist_ok=True)
        eff_out.write_text(text + "\n", encoding="utf-8", newline="\n")
        _echo_utf8(f"Saved output to `{eff_out}` ({len(text.encode('utf-8')):,} bytes).")
        return
    _echo_utf8(text)


def _read_json_input(file_path: str | None, inline_json: str | None, *, label: str) -> Any:
    import json
    from pathlib import Path

    if file_path:
        if file_path == "-":
            raw = sys.stdin.read().lstrip("\ufeff")
        else:
            p = Path(file_path)
            if not p.is_file():
                raise click.UsageError(f"{label} file not found: {file_path}")
            raw = p.read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    elif inline_json:
        raw = inline_json.lstrip("\ufeff")
        if raw.startswith("@"):
            p = Path(raw[1:].strip())
            if not p.is_file():
                raise click.UsageError(f"{label} file not found: {p}")
            raw = p.read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    else:
        raise click.UsageError(f"Provide {label} via --file <path|-> or inline JSON.")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"Invalid JSON for {label}: {exc}") from exc


def _parse_gem_spec(spec: str) -> dict[str, Any]:
    """Parse 'Gem Name[:level[:quality]]' into a gem dict."""
    s = spec.strip()
    m = _GEM_SPEC_RE.match(s)
    if m and m.group(1).strip():
        out: dict[str, Any] = {"name": m.group(1).strip(), "level": int(m.group(2))}
        if m.group(3) is not None:
            out["quality"] = int(m.group(3))
        return out
    return {"name": s}


DAEMON_FAILURE_EXIT_CODE = 5  # same code as upstream/API failures in the exit-code contract


def _daemon_call(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Call the daemon; report known failures as clean messages instead of tracebacks.

    - BootstrapMissingError: engine not installed -> update/bootstrap guidance, exit 1.
    - DaemonRequestError: the daemon rejected the request (invalid arg/build state)
      -> `Error: <message>`, exit 1.
    - Any other RuntimeError / OSError / HTTP / ValueError: daemon itself failed
      (not running, boot failure, startup timeout, crash) -> `Error: PoB daemon failure ...`, exit 5.
    """
    import http.client

    from .daemon import BootstrapMissingError, DaemonRequestError, call_daemon
    from .errors import CrossGameHallucinationError, LegacyPatchTrapError
    from .updater import format_stderr_update_alert

    try:
        return call_daemon(action, params, auto_start=auto_start, timeout=timeout)
    except (CrossGameHallucinationError, LegacyPatchTrapError):
        raise
    except BootstrapMissingError as exc:
        alert = format_stderr_update_alert(exc.update_status) or str(exc)
        _echo_stderr_utf8(alert)
        raise click.exceptions.Exit(1) from exc
    except DaemonRequestError as exc:
        msg = str(exc).strip() or f"Daemon rejected '{action}'."
        raise click.ClickException(msg) from exc
    except (RuntimeError, OSError, http.client.HTTPException, ValueError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        err = click.ClickException(
            f"PoB daemon failure during '{action}': {detail}\n"
            "Run `pob stop` and retry; check daemon logs in the runtime directory if this persists."
        )
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc


def _fmt_signed_pct(delta: Any, pct: Any = None) -> str:
    """Lazy wrapper delegating to `formatters.fmt_signed_pct`."""
    from .formatters import fmt_signed_pct

    return fmt_signed_pct(delta, pct)


def _keep_rejected_warning(stripped: Any) -> list[str]:
    """Lazy wrapper delegating to `formatters.keep_rejected_warning`."""
    from .formatters import keep_rejected_warning

    return keep_rejected_warning(stripped)


def _restore_warning(check: Any) -> str | None:
    """Lazy wrapper delegating to `formatters.restore_warning`."""
    from .formatters import restore_warning

    return restore_warning(check)


def _merge_keep_tokens(*sources: Any) -> list[str]:
    """Merge repeatable CLI `--keep` values with optional JSON dict `keep` preserving order."""
    merged: list[str] = []
    for src in sources:
        if src is None:
            continue
        items = [src] if isinstance(src, str) else (list(src) if isinstance(src, (list, tuple)) else [str(src)])
        for item in items:
            token = str(item).strip()
            if token and token not in merged:
                merged.append(token)
    return merged


def _prepend_warning(body: str, res: dict[str, Any]) -> str:
    from .formatters import render_update_warning

    banner = render_update_warning(res.get("updateWarning"))
    return f"{banner}{body}" if banner else body


def _render_mutation_report(title: str, res: dict[str, Any], *, target: str | None = None) -> str:
    from .formatters import fmt_delta, fmt_num, format_recency_stamp, render_table

    stamp = format_recency_stamp(res, slot=target or res.get("targetSlot"))
    lines = [stamp, f"### {title} — `{res.get('mode', '')}`"]
    before = res.get("before") or {}
    after = res.get("after") or {}
    no_undo = bool(res.get("noUndo")) or (not before and bool(after))
    p_key = after.get("primaryDpsKey") or before.get("primaryDpsKey") or "CombinedDPS"
    b_pts = before.get("points") or {}
    a_pts = after.get("points") or {}

    if no_undo:
        summary_rows = [
            [f"Primary DPS ({p_key})", "-", fmt_num(after.get("primaryDps")), "(no-undo)"],
            ["Effective HP (TotalEHP)", "-", fmt_num(after.get("totalEhp")), "(no-undo)"],
            ["Maximum Life", "-", fmt_num(after.get("life")), "(no-undo)"],
            [
                "Passive Points",
                "-",
                f"{a_pts.get('used', 0)}/{a_pts.get('usedMax', 0)} (Asc {a_pts.get('ascUsed', 0)}/{a_pts.get('ascMax', 0)})",
                "(no-undo)",
            ],
        ]
    else:
        summary_rows = [
            [
                f"Primary DPS ({p_key})",
                fmt_num(before.get("primaryDps")),
                fmt_num(after.get("primaryDps")),
                fmt_delta(before.get("primaryDps"), after.get("primaryDps")),
            ],
            [
                "Effective HP (TotalEHP)",
                fmt_num(before.get("totalEhp")),
                fmt_num(after.get("totalEhp")),
                fmt_delta(before.get("totalEhp"), after.get("totalEhp")),
            ],
            [
                "Maximum Life",
                fmt_num(before.get("life")),
                fmt_num(after.get("life")),
                fmt_delta(before.get("life"), after.get("life")),
            ],
            [
                "Passive Points",
                f"{b_pts.get('used', 0)}/{b_pts.get('usedMax', 0)} (Asc {b_pts.get('ascUsed', 0)}/{b_pts.get('ascMax', 0)})",
                f"{a_pts.get('used', 0)}/{a_pts.get('usedMax', 0)} (Asc {a_pts.get('ascUsed', 0)}/{a_pts.get('ascMax', 0)})",
                fmt_delta(b_pts.get("used", 0), a_pts.get("used", 0), " pts"),
            ],
        ]
    lines.append(render_table(["Headline Metric", "Before", "After", "Delta"], summary_rows))

    diff = res.get("diff") or {}
    deltas = diff.get("deltas") or []
    if deltas:
        lines.append("\n#### Changed Stats")
        d_rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
        lines.append(render_table(["Stat", "Before", "After", "Delta"], d_rows))
    elif no_undo:
        lines.append("\n_(Before/diff snapshot skipped in `--no-undo` mode)_")
    else:
        lines.append("\n_(No stat changes detected)_")

    cascade_warn = res.get("cascadeWarning") or (res.get("detail") or {}).get("cascadeWarning")
    if cascade_warn:
        lines.append(f"\n> [WARN] {cascade_warn}")

    new_w = diff.get("newWarnings") or []
    res_w = diff.get("resolvedWarnings") or []
    if new_w:
        lines.append("\n#### New Warnings")
        for w in new_w:
            lines.append(f"- [NEW WARNING] {w}")
    if res_w:
        lines.append("\n#### Resolved Warnings")
        for w in res_w:
            lines.append(f"- [RESOLVED] {w}")

    return "\n".join(lines)


def _call_with_supported_kwargs(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke a helper passing only kwargs accepted by its signature (resilient across rolling refactors)."""
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if has_var_kw:
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(*args, **filtered)


@click.group(
    cls=GuardrailGroup,
    lazy_subcommands=_LAZY_SUBCOMMANDS,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "-V", "--version", prog_name="pob")
@click.option("--offline", is_flag=True, help="Skip GitHub engine update checks (sets POB_OFFLINE=1).")
@click.option("--compact", is_flag=True, default=False, help="Emit compact single-line JSON for --json commands.")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None, help="Write output to UTF-8 (no BOM) file.")
@click.pass_context
def main(ctx: click.Context, offline: bool, compact: bool, out_path: str | None) -> None:
    """Headless Path of Building (PoB) CLI for build inspection, what-if testing, and optimization."""
    if offline:
        os.environ["POB_OFFLINE"] = "1"
    ctx.ensure_object(dict)
    ctx.obj["compact"] = compact
    ctx.obj["out"] = out_path


@main.command("_daemon", hidden=True)
@click.option("--pob-dir", type=click.Path(), default=None)
@click.option("--runtime-dir", type=click.Path(), default=None)
@click.option("--port", type=int, default=0)
@click.option("--offline", is_flag=True, default=False)
@click.option("--proxy", type=str, default=None)
@click.option("--mirror", type=str, default=None)
def cmd_internal_daemon(
    pob_dir: str | None,
    runtime_dir: str | None,
    port: int,
    offline: bool,
    proxy: str | None,
    mirror: str | None,
) -> None:
    """Internal entry point for spawning the background daemon (used by standalone binaries)."""
    from pathlib import Path

    from .daemon import run_daemon_server

    run_daemon_server(
        pob_dir=Path(pob_dir) if pob_dir else None,
        port=port,
        runtime_dir=Path(runtime_dir) if runtime_dir else None,
        offline=True if offline else None,
        proxy=proxy,
        mirror=mirror,
    )


@main.command("serve")
@click.option("--offline", is_flag=True, default=False, help="Skip GitHub engine update checks.")
@click.option("--proxy", type=str, default=None, help="HTTP/SOCKS proxy URL.")
@click.option("--mirror", type=str, default=None, help="Optional GitHub mirror prefix URL.")
@click.option("--port", type=int, default=0, help="Loopback TCP port (0 = auto-assign).")
@click.option("--runtime-dir", type=click.Path(), default=None, help="Override runtime state directory.")
def cmd_serve(
    offline: bool,
    proxy: str | None,
    mirror: str | None,
    port: int,
    runtime_dir: str | None,
) -> None:
    """Run the PoB daemon persistently in the foreground (ideal for IsDaemon:true agent shells)."""
    from pathlib import Path

    from . import daemon as daemon_mod
    from .errors import CrossGameHallucinationError
    from .updater import format_stderr_update_alert

    runner = getattr(daemon_mod, "run_daemon_forever", None) or daemon_mod.run_daemon_server
    try:
        _call_with_supported_kwargs(
            runner,
            port=port,
            runtime_dir=Path(runtime_dir) if runtime_dir else None,
            offline=True if offline else None,
            proxy=proxy,
            mirror=mirror,
        )
    except CrossGameHallucinationError:
        raise
    except daemon_mod.BootstrapMissingError as exc:
        alert = format_stderr_update_alert(exc.update_status) or str(exc)
        _echo_stderr_utf8(alert)
        raise click.exceptions.Exit(1) from exc
    except (OSError, RuntimeError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        err = click.ClickException(f"PoB daemon failed to start: {detail}")
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc


# --- 4.1 Lifecycle, Engine Updates & Build I/O ---


@main.command("bootstrap")
@click.option("--proxy", help="HTTP/SOCKS proxy URL (e.g. http://127.0.0.1:7890).")
@click.option("--mirror", help="Optional GitHub mirror prefix URL.")
@click.option("--no-zh", is_flag=True, help="Skip PobTools-zh bilingual Chinese DataV2 pack.")
@click.option(
    "--from-dir",
    "from_dir",
    type=click.Path(exists=True, file_okay=False),
    help="Offline import from an existing local PobTools directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_bootstrap(
    proxy: str | None,
    mirror: str | None,
    no_zh: bool,
    from_dir: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Bootstrap or offline-import the managed headless PoB engine."""
    import time
    from pathlib import Path

    from .daemon import call_daemon, emit_stderr_update_alert, read_daemon_state
    from .errors import CrossGameHallucinationError
    from .formatters import render_table
    from .updater import (
        STATE_BOOTSTRAP_MISSING,
        EngineGateError,
        UpdateError,
        bootstrap_engine,
        get_update_warning,
    )

    if read_daemon_state() is not None:
        try:
            call_daemon("stop", auto_start=False)
            deadline = time.monotonic() + 2.0
            while read_daemon_state() is not None and time.monotonic() < deadline:
                time.sleep(0.1)
        except CrossGameHallucinationError:
            raise
        except Exception:
            pass

    try:
        res = bootstrap_engine(
            proxy=proxy,
            mirror=mirror,
            include_zh=not no_zh,
            from_dir=Path(from_dir) if from_dir else None,
        )
    except EngineGateError as exc:
        detail = str(exc).strip() or type(exc).__name__
        err = click.ClickException(f"PoB engine bootstrap gate check failed: {detail}")
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc
    except UpdateError as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise click.ClickException(detail) from exc
    except OSError as exc:
        detail = str(exc).strip() or type(exc).__name__
        err = click.ClickException(f"PoB engine bootstrap failed: {detail}")
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc
    warn = get_update_warning(res)
    if warn:
        res["updateWarning"] = warn
    state_name = str(res.get("state") or "")
    if state_name == STATE_BOOTSTRAP_MISSING or state_name.startswith("degraded_"):
        emit_stderr_update_alert(res)

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
    else:
        rows = [
            ["State", state_name or "-"],
            ["Engine Dir", res.get("engineDir") or "-"],
            ["PoB Version", res.get("localPobVersion") or "-"],
            ["PobTools-zh", res.get("pobToolsVersion") or "-"],
            ["Data Pack", res.get("dataVersion") or "-"],
            ["Updated Files", res.get("updatedFilesCount") if res.get("updatedFilesCount") is not None else "-"],
            ["Source", res.get("source") or "github"],
        ]
        body = "### PoB Engine Bootstrap\n" + render_table(["Field", "Value"], rows)
        _emit_text(_prepend_warning(body, res), out_path=out_path)

    if state_name == STATE_BOOTSTRAP_MISSING:
        sys.exit(1)


@main.command("update")
@click.option("--check", "check_only", is_flag=True, help="Check remote versions without applying updates.")
@click.option("--force", is_flag=True, help="Force full check/apply ignoring cached ETag.")
@click.option("--proxy", help="HTTP/SOCKS proxy URL (e.g. http://127.0.0.1:7890).")
@click.option("--mirror", help="Optional GitHub mirror prefix URL.")
@click.option("--offline", is_flag=True, help="Skip network check and report local versions.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_update(
    check_only: bool,
    force: bool,
    proxy: str | None,
    mirror: str | None,
    offline: bool,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Check or incrementally update managed PoB & PobTools-zh engine (preserving loaded builds if daemon is alive)."""
    import http.client

    from .daemon import DaemonRequestError, call_daemon, emit_stderr_update_alert, read_daemon_state
    from .errors import CrossGameHallucinationError
    from .formatters import render_table
    from .updater import STATE_BOOTSTRAP_MISSING, check_and_apply_updates, get_update_warning

    eff_offline = bool(offline or os.environ.get("POB_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on"))
    res: dict[str, Any] | None = None
    if read_daemon_state() is not None:
        try:
            res = call_daemon(
                "update",
                {
                    "check_only": check_only,
                    "force": force,
                    "proxy": proxy,
                    "mirror": mirror,
                    "offline": eff_offline,
                },
                auto_start=False,
                timeout=180.0,
            )
        except CrossGameHallucinationError:
            raise
        except DaemonRequestError as exc:
            msg = str(exc).strip() or "Daemon rejected 'update'."
            raise click.ClickException(msg) from exc
        except (RuntimeError, OSError, http.client.HTTPException, ValueError) as exc:
            if "not running" not in str(exc).lower():
                detail = str(exc).strip() or type(exc).__name__
                err = click.ClickException(
                    f"PoB daemon failure during 'update': {detail}\n"
                    "Run `pob stop` and retry; check daemon logs in the runtime directory if this persists."
                )
                err.exit_code = DAEMON_FAILURE_EXIT_CODE
                raise err from exc

    if res is None:
        res = check_and_apply_updates(
            check_only=check_only,
            force=force,
            proxy=proxy,
            mirror=mirror,
            offline=eff_offline,
        )
        warn = get_update_warning(res)
        if warn:
            res["updateWarning"] = warn

    state_name = str(res.get("state") or "")
    if state_name == STATE_BOOTSTRAP_MISSING or state_name.startswith("degraded_"):
        emit_stderr_update_alert(res)

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
    else:
        rows = [
            ["State", state_name or "-"],
            ["Local PoB Version", res.get("localPobVersion") or "-"],
            ["Remote PoB Version", res.get("remotePobVersion") or "-"],
            ["PobTools-zh Version", res.get("pobToolsVersion") or "-"],
            ["Data Pack Version", res.get("dataVersion") or "-"],
            ["Engine Dir", res.get("engineDir") or "-"],
            ["Update Available", res.get("updateAvailable") if res.get("updateAvailable") is not None else "-"],
            ["Files Updated", res.get("updatedFilesCount") if res.get("updatedFilesCount") is not None else "-"],
        ]
        if res.get("networkError"):
            rows.append(["Network Error", res["networkError"]])
        body = "### PoB Engine Update Status\n" + render_table(["Field", "Value"], rows)
        _emit_text(_prepend_warning(body, res), out_path=out_path)

    if state_name == STATE_BOOTSTRAP_MISSING:
        sys.exit(1)


@main.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_status(as_json: bool, compact: bool, out_path: str | None) -> None:
    """Show daemon status, engine versions, update state, and loaded builds across slots."""
    from .daemon import emit_stderr_update_alert
    from .formatters import render_table

    res = _daemon_call("status")
    upd = res.get("updateStatus") or {}
    if str(upd.get("state") or "").startswith("degraded_") or res.get("updateWarning"):
        emit_stderr_update_alert(upd)

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    m = res.get("main", {})
    m_hello = m.get("hello") or {}

    pob_ver = upd.get("localPobVersion") or m_hello.get("pobVersion") or "-"
    tools_ver = upd.get("pobToolsVersion") or "-"
    data_ver = upd.get("dataVersion") or "-"
    upd_state = upd.get("state") or "-"

    slots_map = res.get("slots") if isinstance(res.get("slots"), dict) else {}
    slot_entries: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for default_slot in ("main", "ref"):
        if default_slot in slots_map:
            slot_entries.append((default_slot, slots_map[default_slot]))
            seen.add(default_slot)
        elif default_slot in res and isinstance(res[default_slot], dict):
            slot_entries.append((default_slot, res[default_slot]))
            seen.add(default_slot)
    for k, v in sorted(slots_map.items()):
        if k not in seen and isinstance(v, dict):
            slot_entries.append((k, v))

    rows = []
    for slot_name, slot_data in slot_entries:
        s_info = slot_data.get("buildInfo") or {}
        state_lbl = "Running" if slot_data.get("alive") else ("Stopped" if slot_name == "main" else "Idle (Stopped)")
        undo_str = (
            f"{slot_data.get('undoDepth', 0)} / {slot_data.get('redoDepth', 0)}"
            if "undoDepth" in slot_data
            else "-"
        )
        rows.append(
            [
                slot_name,
                state_lbl,
                s_info.get("classNameZh") or s_info.get("className") or "-",
                s_info.get("ascendClassNameZh") or s_info.get("ascendClassName") or "-",
                s_info.get("level") or "-",
                undo_str,
            ]
        )

    header = (
        f"Daemon PID: `{res.get('pid')}` | Uptime: `{res.get('uptimeSec')}s` | "
        f"PoB: `{pob_ver}` | PobTools: `{tools_ver}` | Data: `{data_ver}` | Update: `{upd_state}`\n"
    )
    body = header + render_table(["Slot", "State", "Class", "Ascendancy", "Level", "Undo/Redo"], rows)
    _emit_text(_prepend_warning(body, res), out_path=out_path)


@main.command("stop")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_stop(as_json: bool, compact: bool, out_path: str | None) -> None:
    """Stop the background PoB daemon and release headless processes."""
    import http.client

    from .daemon import DaemonRequestError, call_daemon
    from .errors import CrossGameHallucinationError

    try:
        res = call_daemon("stop", auto_start=False)
    except CrossGameHallucinationError:
        raise
    except DaemonRequestError as exc:
        msg = str(exc).strip() or "Daemon rejected 'stop'."
        raise click.ClickException(msg) from exc
    except (RuntimeError, OSError, http.client.HTTPException, ValueError) as exc:
        if "not running" in str(exc).lower():
            res = {"stopped": True, "note": "Daemon was not running"}
        else:
            detail = str(exc).strip() or type(exc).__name__
            err = click.ClickException(
                f"PoB daemon failure during 'stop': {detail}\n"
                "Check daemon logs in the runtime directory or terminate the stale process manually if this persists."
            )
            err.exit_code = DAEMON_FAILURE_EXIT_CODE
            raise err from exc
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text("PoB daemon stopped.", out_path=out_path)


@main.command("new")
@click.option("--class", "class_name", help="Starting character class (e.g. Witch, Templar, Shadow).")
@click.option("--asc", "ascendancy", help="Primary ascendancy name (e.g. Hierophant, Saboteur).")
@click.option("--secondary-asc", "secondary_asc", help="Secondary / Bloodline ascendancy name.")
@click.option("--level", type=int, help="Character level (1..100).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_new(
    class_name: str | None,
    ascendancy: str | None,
    secondary_asc: str | None,
    level: int | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Create a fresh empty build in the target slot with optional class, ascendancy, and level."""
    from .formatters import render_table
    from .guardrails import check_poe2_class_or_ascendancy

    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_asc, field="secondary_ascendancy")

    res = _daemon_call(
        "new_build",
        {
            "target": target,
            "class_name": class_name,
            "ascendancy": ascendancy,
            "secondary_ascendancy": secondary_asc,
            "level": level,
            "apply": True,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    detail = res.get("detail") if isinstance(res.get("detail"), dict) else {}
    info = detail.get("buildInfo") or res.get("buildInfo") or (res.get("after") or {}).get("buildInfo") or {}
    pts = info.get("points") or (res.get("after") or {}).get("points") or {}
    eff_level = info.get("level") or detail.get("level") or level or "-"
    rows = [
        ["Target Slot", target],
        [
            "Class / Ascendancy",
            f"{info.get('classNameZh') or info.get('className') or class_name or '-'} / "
            f"{info.get('ascendClassNameZh') or info.get('ascendClassName') or ascendancy or '-'}",
        ],
        ["Level", eff_level],
        [
            "Passive Points",
            f"{pts.get('used', 0)}/{pts.get('usedMax', 0)} (Asc: {pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)})",
        ],
    ]
    _emit_text("### New Build Created\n" + render_table(["Field", "Value"], rows), out_path=out_path)


def _prepare_load_source(source: str) -> tuple[str | None, str | None]:
    """Resolve a user build source string (`@path`, `.xml` file, share-code `.txt` file, URL, or Base64 code).

    Returns `(source_str, xml_code_str)`.
    """
    from pathlib import Path

    from .guardrails import validate_build_xml, validate_url

    raw = source.lstrip("\ufeff").strip()
    candidate_path = raw[1:].lstrip("\ufeff").strip() if raw.startswith("@") else raw
    is_local_file = False
    if len(candidate_path) <= 260 and not candidate_path.startswith(("http://", "https://")):
        try:
            p = Path(candidate_path)
            is_local_file = p.exists() and p.is_file()
        except OSError:
            is_local_file = False

    if is_local_file:
        text = Path(candidate_path).read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff").strip()
        if text.lstrip().startswith("<"):
            validate_build_xml(text, expected_game="poe1", source_hint=candidate_path)
            from .importers import xml_to_share_code

            return None, xml_to_share_code(text)
        validate_url(text, expected_game="poe1", cli_subcommand="load")
        from .bridge_client import _decode_share_code_xml

        decoded_file_xml = _decode_share_code_xml(text)
        if decoded_file_xml:
            validate_build_xml(decoded_file_xml, expected_game="poe1", source_hint=candidate_path)
        return text, None

    if raw.startswith("@"):
        raise click.ClickException(f"Build file not found: {candidate_path or raw}")

    if raw.lstrip().startswith("<"):
        validate_build_xml(raw, expected_game="poe1")
        from .importers import xml_to_share_code

        return None, xml_to_share_code(raw)

    validate_url(raw, expected_game="poe1", cli_subcommand="load")
    if not raw.startswith(("http://", "https://")):
        from .bridge_client import _decode_share_code_xml

        decoded_xml = _decode_share_code_xml(raw)
        if decoded_xml:
            validate_build_xml(decoded_xml, expected_game="poe1")

    return raw, None


@main.command("load")
@click.argument("source")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_load(source: str, target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Load a build from local .xml/.txt file (or @file), share URL (pobb.in/poedb/poe.ninja/...), or Base64 code."""
    from .daemon import emit_stderr_update_alert
    from .errors import CrossGameHallucinationError
    from .formatters import render_table, strip_pob_escapes
    from .updater import load_update_state

    params: dict[str, Any] = {"target": target}
    resolved_src, resolved_xml_code = _prepare_load_source(source)
    if resolved_xml_code is not None:
        params["xml_code"] = resolved_xml_code
    else:
        params["source"] = resolved_src

    try:
        res = _daemon_call("load", params)
    except CrossGameHallucinationError:
        raise
    except click.exceptions.Exit:
        raise
    except click.ClickException:
        upd_state = load_update_state()
        if str(upd_state.get("state") or "").startswith("degraded_"):
            emit_stderr_update_alert(upd_state)
        raise
    except Exception as exc:
        upd_state = load_update_state()
        if str(upd_state.get("state") or "").startswith("degraded_"):
            emit_stderr_update_alert(upd_state)
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    info = res.get("buildInfo") or {}
    pts = info.get("points") or {}
    header = res.get("buildHeader") or {}
    main_sg = header.get("mainSocketGroup") or {}
    main_sk = header.get("mainSkill") or {}

    def _dd_label(dd: dict[str, Any]) -> str:
        lst = dd.get("list") or []
        idx = max(0, int(dd.get("index") or 1) - 1)
        if not lst or idx >= len(lst):
            return "-"
        entry = lst[idx]
        if isinstance(entry, dict):
            return strip_pob_escapes(entry.get("labelZh") or entry.get("label") or "-")
        return strip_pob_escapes(str(entry))

    rows = [
        ["Target Slot", target],
        ["Import Method", res.get("importMethod")],
        [
            "Class / Ascendancy",
            f"{info.get('classNameZh') or info.get('className')} / {info.get('ascendClassNameZh') or info.get('ascendClassName')}",
        ],
        ["Level", info.get("level")],
        [
            "Passive Points",
            f"{pts.get('used', 0)}/{pts.get('usedMax', 0)} (Asc: {pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)})",
        ],
        ["Tree Version", info.get("treeVersion")],
        ["Main Skill Group", f"#{main_sg.get('index')} ({_dd_label(main_sg)})"],
        ["Active Skill", _dd_label(main_sk)],
    ]
    body = render_table(["Field", "Value"], rows)
    _emit_text(_prepend_warning(body, res), out_path=out_path)


@main.command("save")
@click.argument("xml_path", type=click.Path(dir_okay=False, writable=True))
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_save(xml_path: str, target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Save the current build to a local .xml file (written by CLI, R11)."""
    from pathlib import Path

    res = _daemon_call("export", {"target": target, "include_xml": True})
    out_file = Path(xml_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    xml_content = res.get("xml") or ""
    out_file.write_text(xml_content, encoding="utf-8")
    payload = {"savedPath": str(out_file), "bytes": len(xml_content.encode("utf-8")), "target": target}
    if as_json:
        _emit_json(payload, compact=compact, out_path=out_path)
    else:
        _emit_text(f"Saved `{target}` build XML to `{out_file}` ({payload['bytes']:,} bytes).", out_path=out_path)


@main.command("export-code")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_export_code(target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Export current build as a URL-safe Base64 PoB share code."""
    res = _daemon_call("export", {"target": target, "include_xml": False})
    if as_json:
        _emit_json({"target": target, "code": res.get("code")}, compact=compact, out_path=out_path)
    else:
        _emit_text(res.get("code") or "", out_path=out_path)


@main.command("switch-main")
@click.option("--spec", "spec_index", help="1-based passive tree spec index or title.")
@click.option("--skill-group", "skill_group", help="1-based skill group index or label.")
@click.option("--skill", "main_skill_index", type=int, help="1-based active skill index in group.")
@click.option("--part", "skill_part_index", type=int, help="1-based skill part index.")
@click.option("--item-set", "item_set_id", help="Item set ID or title.")
@click.option("--skill-set", "skill_set_id", help="Skill set ID or title.")
@click.option("--config-set", "config_set_id", help="Config set ID or title.")
@click.option("--loadout", "loadout_index", help="1-based synced loadout index or title.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_switch_main(
    spec_index: str | None,
    skill_group: str | None,
    main_skill_index: int | None,
    skill_part_index: int | None,
    item_set_id: str | None,
    skill_set_id: str | None,
    config_set_id: str | None,
    loadout_index: str | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Switch active passive tree spec, main skill group/part, item/skill/config set, or loadout."""
    res = _daemon_call(
        "switch_main",
        {
            "target": target,
            "spec_index": spec_index,
            "skill_group": skill_group,
            "main_skill_index": main_skill_index,
            "skill_part_index": skill_part_index,
            "item_set_id": item_set_id,
            "skill_set_id": skill_set_id,
            "config_set_id": config_set_id,
            "loadout_index": loadout_index,
            "apply": True,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_render_mutation_report("Switch Active Selection", res), out_path=out_path)


@main.command("undo")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_undo(target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Undo the last `--apply` mutation."""
    from .formatters import render_table

    res = _daemon_call("undo", {"target": target})
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    deltas = res.get("diff", {}).get("deltas", [])
    rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
    body = (
        f"### Undo Applied (Undo depth: `{res.get('undoDepth')}`, Redo depth: `{res.get('redoDepth')}`)\n"
        + render_table(["Stat", "Before Undo", "After Undo", "Delta"], rows)
    )
    _emit_text(body, out_path=out_path)


@main.command("redo")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_redo(target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Redo the last undone mutation."""
    from .formatters import render_table

    res = _daemon_call("redo", {"target": target})
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    deltas = res.get("diff", {}).get("deltas", [])
    rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
    body = (
        f"### Redo Applied (Undo depth: `{res.get('undoDepth')}`, Redo depth: `{res.get('redoDepth')}`)\n"
        + render_table(["Stat", "Before Redo", "After Redo", "Delta"], rows)
    )
    _emit_text(body, out_path=out_path)


# --- 4.2 Build Inspection & Audit ---


@main.command("summary")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_summary(target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Display comprehensive build identity, offence, minion offence, defence, and warnings."""
    from .formatters import fmt_num, format_recency_stamp, render_table, strip_pob_escapes

    res = _daemon_call("summary", {"target": target})
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    info = res.get("buildInfo", {})
    pts = info.get("points", {})
    mg = res.get("mainGroup") or {}
    gems_str = ", ".join(
        f"{strip_pob_escapes(g.get('nameZh') or g.get('name'))} ({g.get('level')}/{g.get('quality')}%)"
        for g in mg.get("gems", [])
        if g.get("enabled", True)
    )

    lines = [
        format_recency_stamp(res, slot=target),
        f"### Build Summary (`{target}`): Lv.{info.get('level')} {info.get('classNameZh') or info.get('className')} / {info.get('ascendClassNameZh') or info.get('ascendClassName')}",
        f"- **Points**: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` | **Ascendancy**: `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}` | **Tree**: `{info.get('treeVersion')}`",
        f"- **Main Skill Group**: `#{mg.get('index', '-')} {strip_pob_escapes(mg.get('displayLabelZh') or mg.get('displayLabel') or '-')}` (`{mg.get('slot') or 'Unassigned'}`)",
        f"- **Linked Gems**: {gems_str or '-'}",
    ]

    off = res.get("offence", {})
    crit_mult = off.get("CritMultiplier")
    crit_mult_str = f"{float(crit_mult) * 100:.0f}% (x{float(crit_mult):.2f})" if crit_mult is not None else "-"
    dot_dps = off.get("TotalDotDPS") or off.get("TotalDot")
    off_rows = [
        ["Full DPS", fmt_num(off.get("FullDPS")), "Combined DPS", fmt_num(off.get("CombinedDPS"))],
        ["Hit DPS (TotalDPS)", fmt_num(off.get("TotalDPS")), "DoT DPS (TotalDotDPS)", fmt_num(dot_dps)],
        ["Average Damage", fmt_num(off.get("AverageDamage")), "Attack/Cast Rate", f"{fmt_num(off.get('Speed'), 2)}/s"],
        ["Crit Chance", f"{fmt_num(off.get('CritChance'), 1)}%", "Crit Multiplier", crit_mult_str],
        ["Hit Chance", f"{fmt_num(off.get('HitChance'), 1)}%", "Mana Cost", fmt_num(off.get("ManaCost"))],
    ]
    lines.append("\n#### Offence")
    lines.append(render_table(["Metric", "Value", "Metric ", "Value "], off_rows))

    m_off = res.get("minionOffence")
    if m_off:
        lines.append("\n#### Minion Offence")
        m_rows = [
            ["Minion Combined DPS", fmt_num(m_off.get("MinionCombinedDPS")), "Minion Hit DPS", fmt_num(m_off.get("MinionTotalDPS"))],
            ["Minion Avg Damage", fmt_num(m_off.get("MinionAverageDamage")), "Minion Speed", f"{fmt_num(m_off.get('MinionSpeed'), 2)}/s"],
            ["Minion Crit Chance", f"{fmt_num(m_off.get('MinionCritChance'), 1)}%", "Minion Life", fmt_num(m_off.get("MinionLife"))],
        ]
        for s_row in m_off.get("sidebarRows", []):
            m_rows.append([s_row["label"], s_row["value"], s_row["stat"], "-"])
        lines.append(render_table(["Minion Metric", "Value", "Detail", "Extra"], m_rows))

    dfn = res.get("defence", {})
    def_rows = [
        ["Effective HP (TotalEHP)", fmt_num(dfn.get("TotalEHP")), "Maximum Life / Unres", f"{fmt_num(dfn.get('Life'))} / {fmt_num(dfn.get('LifeUnreserved'))} ({fmt_num(dfn.get('LifeUnreservedPercent'), 0)}%)"],
        ["Energy Shield / Ward", f"{fmt_num(dfn.get('EnergyShield'))} / {fmt_num(dfn.get('Ward'))}", "Maximum Mana / Unres", f"{fmt_num(dfn.get('Mana'))} / {fmt_num(dfn.get('ManaUnreserved'))} ({fmt_num(dfn.get('ManaUnreservedPercent'), 0)}%)"],
        ["Phys / Chaos Max Hit", f"{fmt_num(dfn.get('PhysicalMaximumHitTaken'))} / {fmt_num(dfn.get('ChaosMaximumHitTaken'))}", "Fire/Cold/Light Max Hit", f"{fmt_num(dfn.get('FireMaximumHitTaken'))} / {fmt_num(dfn.get('ColdMaximumHitTaken'))} / {fmt_num(dfn.get('LightningMaximumHitTaken'))}"],
        ["Fire / Cold Resist", f"{fmt_num(dfn.get('FireResist'), 0)}% (+{fmt_num(dfn.get('FireResistOverCap'), 0)}%) / {fmt_num(dfn.get('ColdResist'), 0)}% (+{fmt_num(dfn.get('ColdResistOverCap'), 0)}%)", "Light / Chaos Resist", f"{fmt_num(dfn.get('LightningResist'), 0)}% (+{fmt_num(dfn.get('LightningResistOverCap'), 0)}%) / {fmt_num(dfn.get('ChaosResist'), 0)}% (+{fmt_num(dfn.get('ChaosResistOverCap'), 0)}%)"],
        ["Armour / Evade", f"{fmt_num(dfn.get('Armour'))} / {fmt_num(dfn.get('Evasion'))}", "Block / Spell Block / Supp", f"{fmt_num(dfn.get('EffectiveBlockChance'), 0)}% / {fmt_num(dfn.get('EffectiveSpellBlockChance'), 0)}% / {fmt_num(dfn.get('EffectiveSpellSuppressionChance'), 0)}%"],
        ["Net Life / Mana Regen", f"{fmt_num(dfn.get('NetLifeRegen'), 1)}/s / {fmt_num(dfn.get('NetManaRegen'), 1)}/s", "Str / Dex / Int", f"{fmt_num(dfn.get('Str'))} / {fmt_num(dfn.get('Dex'))} / {fmt_num(dfn.get('Int'))}"],
    ]
    lines.append("\n#### Defence & Sustain")
    lines.append(render_table(["Metric", "Value", "Metric ", "Value "], def_rows))

    warnings = res.get("warnings", [])
    if warnings:
        lines.append("\n#### Warnings")
        for w in warnings:
            lines.append(f"- {w}")

    _emit_text(_prepend_warning("\n".join(lines), res), out_path=out_path)


@main.command("diagnose")
@click.argument("source", required=False)
@click.option(
    "-c",
    "--compare",
    "compare_source",
    default=None,
    help="Second build source (@file.xml/@file.code/URL/code) for one-shot 3-baseline A/B comparison + Pinnacle audit of both builds.",
)
@click.option(
    "-q",
    "--ask",
    "ask_text",
    default=None,
    help="Player question text to ground (<15ms 3.30 fact cards + PoE2/patch trap alerts) and merge into the diagnosis.",
)
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed config/flask/skill/customMods/party items during dewatering (repeatable or comma-separated).",
)
@click.option("--top", type=int, default=5, show_default=True, help="Top N passive nodes & candidate support gems.")
@click.option("--no-rank", "no_rank", is_flag=True, help="Skip passive node & support gem ROI rankings (faster; default when --compare is set).")
@_target_option(allow_slot_alias=True)
@click.option(
    "--ref-target",
    "ref_target",
    type=_SLOT_SLUG_TYPE,
    default=None,
    help="Reference build slot slug for --compare (defaults to 'ref' when target=='main', else '<target>_ref').",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_diagnose(
    ctx: click.Context,
    source: str | None,
    compare_source: str | None,
    ask_text: str | None,
    keep: tuple[str, ...],
    top: int,
    no_rank: bool,
    target: str,
    ref_target: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """One-shot comprehensive build diagnosis (and optional dual-build --compare & --ask question grounding)."""
    from click.core import ParameterSource

    from .formatters import format_diagnose_markdown

    # In --compare mode, default to no_rank=True (<8KB compact report) unless --top was explicitly passed.
    eff_no_rank = bool(no_rank)
    if compare_source and not no_rank:
        top_src = ctx.get_parameter_source("top")
        if top_src != ParameterSource.COMMANDLINE:
            eff_no_rank = True

    params: dict[str, Any] = {
        "target": target,
        "top": top,
        "no_rank": eff_no_rank,
    }
    if keep:
        params["keep"] = list(keep)
    if ask_text and ask_text.strip():
        params["ask"] = ask_text.strip()
    if source:
        resolved_src, resolved_xml_code = _prepare_load_source(source)
        if resolved_xml_code is not None:
            params["xml_code"] = resolved_xml_code
        else:
            params["source"] = resolved_src
    if compare_source:
        eff_ref = ref_target or ("ref" if target == "main" else f"{target}_ref")
        params["ref_target"] = eff_ref
        cmp_src, cmp_xml_code = _prepare_load_source(compare_source)
        if cmp_xml_code is not None:
            params["compare_xml_code"] = cmp_xml_code
        else:
            params["compare_source"] = cmp_src

    res = _daemon_call("diagnose", params, timeout=240.0 if compare_source else 180.0)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_diagnose_markdown(res), out_path=out_path)


@main.command("ground")
@click.argument("text", nargs=-1, required=True)
@click.option("--limit", type=int, default=8, show_default=True, help="Maximum live gem/unique fact cards to attach.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_ground(
    text: tuple[str, ...],
    limit: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Fast (<15ms, daemon-free) entity & PoE2/legacy-patch trap grounder for arbitrary player prompts."""
    from .formatters import format_ground_markdown
    from .game_data import ground_text_entities

    joined = " ".join(t for t in text if t).strip()
    if not joined:
        raise click.UsageError("Provide non-empty prompt text to ground.")
    res = ground_text_entities(joined, limit=limit)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
        return
    _emit_text(format_ground_markdown(res), out_path=out_path)


@main.command("audit")
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed config/flask/skill/customMods/party items during dewatering (repeatable or comma-separated).",
)
@click.option(
    "--no-attribution",
    "no_attribution",
    is_flag=True,
    help="Skip per-item water attribution recalculations.",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_audit(
    keep: tuple[str, ...],
    no_attribution: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Run build health check and non-destructive 4-part PoB inflation ('water') audit."""
    from .formatters import fmt_delta, fmt_num, format_recency_stamp, render_table

    params: dict[str, Any] = {"target": target, "attribution": not no_attribution}
    if keep:
        params["keep"] = list(keep)
    res = _daemon_call("audit", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    lines = [format_recency_stamp(res, slot=target), "### Build Health & Inflation ('Water') Audit"]
    r_warn = _restore_warning(res)
    if r_warn:
        lines.append(r_warn)
    issues = res.get("healthIssues", [])
    if issues:
        lines.append("\n#### 1. Health & Hard Gaps")
        i_rows = [[x["severity"], x["category"], x["message"]] for x in issues]
        lines.append(render_table(["Severity", "Category", "Finding"], i_rows))
    else:
        lines.append("\n#### 1. Health & Hard Gaps\n- No critical resistance, attribute, or sustain gaps detected.")

    stripped = res.get("inflationStripped", {})
    lines.append("\n#### 2. Inflation ('Water') Stripped for Pinnacle Boss Baseline")
    s_rows = []
    for dim in ("config", "flasks", "skills", "party"):
        items = stripped.get(dim) or []
        s_rows.append([dim.capitalize(), len(items), "; ".join(items) if items else "_(Clean)_"])
    lines.append(render_table(["Dimension", "Count", "Stripped Items"], s_rows))
    kept = stripped.get("kept") or []
    if kept:
        lines.append("\n**Kept (--keep)**: " + "; ".join(kept))
    rej_lines = _keep_rejected_warning(stripped)
    if rej_lines:
        lines.append("\n" + "\n".join(rej_lines))

    attr = res.get("waterAttribution") or []
    if attr:
        dps_key = (res.get("original") or {}).get("primaryDpsKey") or "DPS"
        lines.append(
            "\n#### 2b. Per-Item Water Attribution (PoB re-calc, restoring ONE item on the de-watered baseline)"
        )
        a_rows = []
        for r in attr:
            if r.get("error"):
                a_rows.append([r.get("dimension") or "-", r.get("label") or "-", "error", r["error"], "", ""])
                continue
            a_rows.append(
                [
                    r.get("dimension") or "-",
                    r.get("label") or "-",
                    _fmt_signed_pct(r.get("dpsDelta"), r.get("dpsPct")),
                    _fmt_signed_pct(r.get("totalEhpDelta"), r.get("totalEhpPct")),
                    _fmt_signed_pct(r.get("physMaxHitDelta"), r.get("physMaxHitPct")),
                    _fmt_signed_pct(r.get("eleMaxHitDelta"), r.get("eleMaxHitPct")),
                ]
            )
        lines.append(
            render_table(
                ["Dim", "Item", f"Δ{dps_key}", "ΔTotalEHP", "ΔPhys MaxHit", "ΔEle MaxHit"],
                a_rows,
            )
        )
        lines.append(
            "_Items your build guarantees in a real boss fight (ascendancy/keystone conditions, "
            "self-applied ailments, always-on charges) can be re-run with `--keep <var>`._"
        )

    orig = res.get("original", {})
    dew = res.get("dewateredPinnacle", {})
    lines.append("\n#### 3. Original Panel vs True Pinnacle Boss Baseline (De-watered)")
    comp_rows = [
        [
            f"Primary DPS ({orig.get('primaryDpsKey')})",
            fmt_num(orig.get("primaryDps")),
            fmt_num(dew.get("primaryDps")),
            fmt_delta(orig.get("primaryDps"), dew.get("primaryDps")),
        ],
        [
            "Effective HP (TotalEHP)",
            fmt_num(orig.get("totalEhp")),
            fmt_num(dew.get("totalEhp")),
            fmt_delta(orig.get("totalEhp"), dew.get("totalEhp")),
        ],
        [
            "Phys Max Hit",
            fmt_num(orig.get("physMaxHit")),
            fmt_num(dew.get("physMaxHit")),
            fmt_delta(orig.get("physMaxHit"), dew.get("physMaxHit")),
        ],
        [
            "Elemental Max Hit (Min)",
            fmt_num(orig.get("eleMaxHit")),
            fmt_num(dew.get("eleMaxHit")),
            fmt_delta(orig.get("eleMaxHit"), dew.get("eleMaxHit")),
        ],
        [
            "Chaos Max Hit",
            fmt_num(orig.get("chaosMaxHit")),
            fmt_num(dew.get("chaosMaxHit")),
            fmt_delta(orig.get("chaosMaxHit"), dew.get("chaosMaxHit")),
        ],
    ]
    lines.append(render_table(["Metric", "Original Panel", "Dewatered Pinnacle", "Water Delta"], comp_rows))
    _emit_text("\n".join(lines), out_path=out_path)


@main.command("items")
@click.option("--slot", help="Inspect a specific slot (e.g. 'Weapon 1', 'Amulet', 'Flask 1', or '#1' jewel socket).")
@click.option("--all", "all_items", is_flag=True, help="Fetch all equipped items and allocated jewel sockets with full raw mods.")
@click.option("--raw", is_flag=True, help="Include raw PoE item text.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_items(
    slot: str | None,
    all_items: bool,
    raw: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """List equipped items, inspect a single slot, or dump all equipped items & jewels (`--all --raw`)."""
    from .formatters import render_table, strip_pob_escapes

    if slot:
        from .guardrails import GuardrailScope, check_input

        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")

    res = _daemon_call(
        "items",
        {"target": target, "slot": slot, "raw": bool(raw or all_items), "allRaw": bool(all_items)},
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    if slot:
        item = res.get("item")
        slot_name = (res.get("slot") or {}).get("name", slot)
        if not item:
            _emit_text(f"Slot `{slot_name}` is empty.", out_path=out_path)
            return
        lines = [
            f"### Slot `{slot_name}`: {strip_pob_escapes(item.get('titleZh') or item.get('title') or item.get('name'))} (`{item.get('rarity')}`)",
        ]
        tip = res.get("tooltip") or {}
        for ln in tip.get("lines", []):
            txt = strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
            if txt:
                lines.append(f"  {txt}")
        if raw and res.get("raw"):
            lines.append("\n```text\n" + res["raw"].strip() + "\n```")
        _emit_text(_prepend_active_guardrail_warnings("\n".join(lines)), out_path=out_path)
        return

    if all_items:
        slots = res.get("equippedSlots", [])
        lines = [
            f"### All Equipped Items & Allocated Jewels (`{res.get('equippedCount', 0)}` equipped / `{len(slots)}` slots)",
        ]
        for s in slots:
            slot_disp = s["slotName"]
            if s.get("socketIndex"):
                slot_disp = f"{slot_disp} (#{s['socketIndex']})"
            it = s.get("item")
            if not it:
                lines.append(f"\n#### `{slot_disp}` — _(Empty)_")
                continue
            title = strip_pob_escapes(it.get("titleZh") or it.get("title") or it.get("nameZh") or it.get("name") or "-")
            base = strip_pob_escapes(it.get("baseNameZh") or it.get("baseName") or "")
            status = " [OFF]" if (s.get("isFlask") and not s.get("active")) else ""
            lines.append(f"\n#### `{slot_disp}`{status}: {title} — {base} (`{it.get('rarity') or '-'}`)")
            raw_txt = s.get("raw")
            if raw_txt:
                lines.append("```text\n" + str(raw_txt).strip() + "\n```")
        _emit_text(_prepend_active_guardrail_warnings("\n".join(lines)), out_path=out_path)
        return

    rows = []
    for s in res.get("equippedSlots", []):
        it = s.get("item") or {}
        slot_disp = s["slotName"]
        if s.get("socketIndex"):
            slot_disp = f"{slot_disp} (#{s['socketIndex']})"
        state = "Active" if s.get("active") else ("Flask Off" if s.get("isFlask") else ("Inactive" if s.get("inactive") else "Equipped"))
        rows.append(
            [
                slot_disp,
                strip_pob_escapes(it.get("titleZh") or it.get("title") or it.get("nameZh") or it.get("name") or "-"),
                it.get("baseNameZh") or it.get("baseName") or "-",
                it.get("rarity") or "-",
                state if it else "Empty",
            ]
        )
    _emit_text(render_table(["Slot", "Item Name", "Base", "Rarity", "Status"], rows), out_path=out_path)


@main.command("skills")
@click.option("--group", help="Inspect a specific skill group ('main', index, or label).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skills(group: str | None, target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """List all skill groups or inspect gems and per-gem DPS tooltips in a group."""
    from .formatters import fmt_num, render_table, strip_pob_escapes

    res = _daemon_call("skills", {"target": target, "group": group})
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    if group is not None:
        grp = res.get("group", {})
        rows = []
        for gm in grp.get("gems", []):
            dps_info = gm.get("tooltipDps") or {}
            rows.append(
                [
                    gm.get("index"),
                    strip_pob_escapes(gm.get("nameZh") or gm.get("name") or gm.get("nameSpec")),
                    "Support" if gm.get("support") else "Active",
                    f"{gm.get('level')}/{gm.get('quality')}%",
                    "Yes" if gm.get("enabled", True) else "No",
                    f"{dps_info.get('pct', 0):+.1f}% ({fmt_num(dps_info.get('delta'))})" if dps_info.get("pct") is not None else "-",
                ]
            )
        body = (
            f"### Skill Group #{grp.get('index')}: {strip_pob_escapes(grp.get('displayLabelZh') or grp.get('displayLabel'))} (Slot: `{grp.get('slot') or '-'}`)\n"
            + render_table(["#", "Gem", "Type", "Lv/Qual", "Enabled", "Tooltip DPS Impact"], rows)
        )
        _emit_text(body, out_path=out_path)
        return

    rows = []
    for g in res.get("groups", []):
        gems_summary = ", ".join(
            f"{strip_pob_escapes(gm.get('nameZh') or gm.get('name'))}{'' if gm.get('enabled', True) else '[OFF]'}"
            for gm in g.get("gems", [])
        )
        rows.append(
            [
                f"#{g['index']}{' *MAIN*' if g.get('isMain') else ''}",
                strip_pob_escapes(g.get("displayLabelZh") or g.get("displayLabel") or g.get("label")),
                g.get("slot") or "-",
                "Yes" if g.get("enabled", True) else "No",
                "Yes" if g.get("includeInFullDPS") else "No",
                gems_summary,
            ]
        )
    _emit_text(render_table(["Group", "Label", "Slot", "Enabled", "FullDPS", "Gems"], rows), out_path=out_path)


def _prepend_active_guardrail_warnings(body: str) -> str:
    from .guardrails import get_active_warnings

    warns = get_active_warnings()
    if not warns:
        return body
    lines = [w.format_markdown() for w in warns]
    return "\n".join(lines) + "\n\n" + body


def _run_tree_inspect(
    *,
    searches: tuple[str, ...],
    node_types: tuple[str, ...],
    allocated: bool | None,
    max_cost: int | None,
    show_path: bool,
    masteries: bool,
    socket_radius: str | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Summarize allocated passive tree nodes or search/filter tree nodes with paths, masteries, or jewel radius."""
    from .formatters import format_tree_markdown
    from .guardrails import GuardrailScope, check_input

    search_list = [s for s in searches if s]
    for s in search_list:
        check_input(s, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    params: dict[str, Any] = {
        "target": target,
        "search": search_list[0] if search_list else None,
        "queries": search_list or None,
        "node_types": list(node_types) or None,
        "allocated": allocated,
        "max_cost": max_cost,
        "show_path": show_path,
        "masteries": masteries,
        "socket_radius": socket_radius,
    }
    res = _daemon_call("tree", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    search_label = ", ".join(search_list) if search_list else None
    body = format_tree_markdown(res, search_label=search_label, show_path=show_path, masteries=masteries)
    _emit_text(_prepend_active_guardrail_warnings(body), out_path=out_path)


@main.command("calc")
@click.argument("keywords", nargs=-1, required=True)
@click.option("--limit", type=int, default=20, show_default=True, help="Maximum matched rows to return.")
@click.option("--no-breakdown", "no_breakdown", is_flag=True, help="Skip calcs_breakdown sub-tables (fast numeric values only).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_calc(
    keywords: tuple[str, ...],
    limit: int,
    no_breakdown: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Search PoB Calcs breakdown tables and formulas by one or more English/Chinese keywords."""
    from .formatters import render_table, strip_pob_escapes
    from .guardrails import GuardrailScope, check_input

    kw_list = list(keywords)
    for kw in kw_list:
        if kw.strip():
            check_input(kw, scope=GuardrailScope.CALC_QUERY, expected_game="poe1")
    res = _daemon_call(
        "calc",
        {
            "target": target,
            "keyword": kw_list[0],
            "keywords": kw_list,
            "limit": limit,
            "no_breakdown": no_breakdown,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    matches = res.get("matches") or []
    kw_disp = ", ".join(kw_list)
    row_limit = max(1, limit)

    def _append_calc_match(lines_out: list[str], m: dict[str, Any]) -> None:
        cell_texts = " | ".join(
            str(c.get("text") or c.get("raw") or c.get("value") or "")
            for c in m.get("cells", [])
            if c.get("text") or c.get("raw") or c.get("value") is not None
        )
        lines_out.append(
            f"\n#### [{m.get('sectionGroup')} / {m.get('subsection')}] {m.get('rowLabelZh') or m.get('rowLabel')}: `{cell_texts}`"
        )
        if no_breakdown:
            return
        for c in m.get("cells", []):
            for sec in c.get("breakdown") or []:
                if sec.get("type") == "text":
                    for ln in sec.get("lines", []):
                        lines_out.append(f"  - {strip_pob_escapes(ln)}")
                elif sec.get("type") == "table":
                    cols = sec.get("cols", [])
                    headers = [col.get("label") or col.get("key") or "" for col in cols]
                    keys = [col.get("key") for col in cols]
                    t_rows = [[r.get(k, "") for k in keys] for r in sec.get("rows", [])]
                    if headers and t_rows:
                        lines_out.append(render_table(headers, t_rows))

    sub_results = res.get("results")
    if isinstance(sub_results, list):
        sub_dicts = [sr for sr in sub_results if isinstance(sr, dict)]
        total_shown = sum(len(sr.get("matches") or []) for sr in sub_dicts)
        if total_shown == 0 and not matches:
            _emit_text(f"No calculation rows matched `{kw_disp}`.", out_path=out_path)
            return
        lines = [f"### Calcs Breakdown for `{kw_disp}` ({res.get('totalMatches', total_shown)} matches)"]
        for sub in sub_dicts:
            sub_kw = str(sub.get("keyword") or "-")
            sub_matches = sub.get("matches") or []
            sub_total = sub.get("totalMatches", len(sub_matches))
            lines.append(f"\n### {sub_kw} ({sub_total} matches)")
            if not sub_matches:
                lines.append("_(no matches)_")
                continue
            for m in sub_matches[:row_limit]:
                _append_calc_match(lines, m)
        _emit_text(_prepend_active_guardrail_warnings("\n".join(lines)), out_path=out_path)
        return

    if not matches:
        _emit_text(f"No calculation rows matched `{kw_disp}`.", out_path=out_path)
        return

    lines = [f"### Calcs Breakdown for `{kw_disp}` ({res.get('totalMatches', len(matches))} matches)"]
    for m in matches[:row_limit]:
        _append_calc_match(lines, m)

    _emit_text(_prepend_active_guardrail_warnings("\n".join(lines)), out_path=out_path)


# --- 4.3 What-If & Apply Mutations ---


@main.group("item")
def grp_item() -> None:
    """Inspect, evaluate, edit, or mutate equipped items, flasks, and anoints."""
    pass


@grp_item.command("inspect")
@click.option("--slot", help="Equipped slot, or the item type for pasted text (e.g. 'Ring 1').")
@click.option("--raw", "raw_text", help="Pasted PoE/PoB item text.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="File containing item text.")
@click.option("--want", "wants", multiple=True, help="Bench target, e.g. 'Cold Resistance' (repeatable).")
@click.option("--limit", type=click.IntRange(min=1, max=20), default=8, show_default=True)
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output structured JSON.")
@_output_options
def cmd_item_inspect(
    slot: str | None,
    raw_text: str | None,
    file_path: str | None,
    wants: tuple[str, ...],
    limit: int,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Classify item affixes and show compatible crafting-bench options (read-only)."""
    from pathlib import Path

    from .guardrails import GuardrailScope, check_input, check_input_item_raw, validate_url
    from .item_inspect import format_inspection, inspect_item

    if raw_text is not None and file_path is not None:
        raise click.UsageError("Use either --raw or --file, not both.")
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    for w_val in wants:
        validate_url(w_val, expected_game="poe1", cli_subcommand="item inspect")
        check_input(w_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    if file_path is not None:
        raw_text = Path(file_path).read_text(encoding="utf-8-sig", errors="replace")
    if raw_text is None:
        if not slot:
            raise click.UsageError("Pass --slot, --raw, or --file.")
        loaded = _daemon_call("items", {"target": target, "slot": slot, "raw": True})
        raw_text = loaded.get("raw")
        if not raw_text:
            raise click.ClickException(f"Slot {slot!r} is empty or its item text is unavailable.")
        slot = (loaded.get("slot") or {}).get("name") or slot
    check_input_item_raw(raw_text, expected_game="poe1")
    try:
        report = inspect_item(raw_text, slot=slot, wants=wants, limit=limit)
    except (ValueError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit_json(report, compact=compact, out_path=out_path)
    else:
        _emit_text(format_inspection(report), out_path=out_path)


@grp_item.command("try")
@click.option("--slot", help="Target equipment slot (e.g. 'Weapon 1', 'Ring 1', '#1').")
@click.option("--raw", "raw_text", help="Raw PoE item text string.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="File containing raw PoE item text.")
@click.option("--tooltip-only", is_flag=True, help="Fast native tooltip preview only (skip snapshot secondary checks).")
@click.option("--apply", is_flag=True, help="Persist equipped item into build and push undo snapshot.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_try(
    slot: str | None,
    raw_text: str | None,
    file_path: str | None,
    tooltip_only: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Evaluate or equip a raw PoE item with native tooltip comparison + full snapshot diff."""
    from pathlib import Path

    from .formatters import strip_pob_escapes

    if file_path:
        raw_text = Path(file_path).read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    if not raw_text:
        raise click.UsageError('Provide either --raw "<item_text>" or --file <item.txt>.')

    from .guardrails import GuardrailScope, check_input, check_input_item_raw

    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input_item_raw(raw_text, expected_game="poe1")

    res = _daemon_call(
        "item_try",
        {
            "target": target,
            "slot": slot,
            "raw": raw_text,
            "tooltip_only": tooltip_only,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    lines = [f"### Item Evaluation (Slot: `{res.get('slotName') or 'Auto'}`)"]
    slot_tips = res.get("slotTooltips") or []
    if not slot_tips and res.get("tooltip"):
        slot_tips = [res["tooltip"]]
    for st in slot_tips:
        st_slot = st.get("slotName") or res.get("slotName") or "Default"
        t_lines = st.get("lines") or []
        if t_lines:
            lines.append(f"\n#### Native PoB Tooltip (`{st_slot}`)")
            for ln in t_lines:
                txt = (
                    strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
                    if isinstance(ln, dict)
                    else strip_pob_escapes(str(ln))
                )
                if txt:
                    lines.append(f"  {txt}")

    if res.get("mutation"):
        lines.append("\n" + _render_mutation_report("Snapshot Full Simulation", res["mutation"]))
    _emit_text("\n".join(lines), out_path=out_path)


@grp_item.command("edit")
@click.option("--slot", required=True, help="Equipped slot to edit (e.g. 'Ring 1', 'Weapon 1', '#1').")
@click.option(
    "--replace-mod",
    "replace_mods",
    nargs=2,
    multiple=True,
    metavar="<OLD> <NEW>",
    help="Replace matching mod line(s) with NEW text (repeatable).",
)
@click.option("--add-mod", "add_mods", multiple=True, help="Append an explicit mod line (repeatable).")
@click.option("--remove-mod", "remove_mods", multiple=True, help="Remove matching mod line(s) (repeatable).")
@click.option("--apply", is_flag=True, help="Persist edited item into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_edit(
    slot: str,
    replace_mods: tuple[tuple[str, str], ...],
    add_mods: tuple[str, ...],
    remove_mods: tuple[str, ...],
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """In-place replace, add, or remove mod lines on an equipped item (0-residue what-if by default)."""
    from .formatters import render_table

    if not replace_mods and not add_mods and not remove_mods:
        raise click.UsageError("Provide at least one of --replace-mod, --add-mod, or --remove-mod.")

    from .guardrails import GuardrailScope, check_input

    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    for _, new_line in replace_mods:
        check_input(new_line, scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    for new_mod in add_mods:
        check_input(new_mod, scope=GuardrailScope.ITEM_RAW, expected_game="poe1")

    res = _daemon_call(
        "item_edit",
        {
            "target": target,
            "slot": slot,
            "replace_mods": [list(p) for p in replace_mods],
            "add_mods": list(add_mods),
            "remove_mods": list(remove_mods),
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    lines = [f"### Item Mod Edit (`{res.get('slotName')}`)"]
    changes = res.get("changes") or []
    if changes:
        c_rows = [
            [
                c.get("action"),
                c.get("section") or "-",
                c.get("before") or "-",
                c.get("after") or "-",
            ]
            for c in changes
        ]
        lines.append(render_table(["Action", "Section", "Before", "After"], c_rows))
    if res.get("mutation"):
        lines.append("\n" + _render_mutation_report(f"Item Edit Impact ({res.get('slotName')})", res["mutation"]))
    _emit_text("\n".join(lines), out_path=out_path)


@grp_item.command("unequip")
@click.option("--slot", required=True, help="Equipment slot to unequip (e.g. 'Weapon 2', 'Ring 2', '#1').")
@click.option("--apply", is_flag=True, help="Persist unequip change into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_unequip(
    slot: str,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Unequip the item in an equipment or jewel slot (0-residue what-if unless --apply)."""
    from .guardrails import GuardrailScope, check_input

    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    res = _daemon_call("item_unequip", {"target": target, "slot": slot, "apply": apply})
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_render_mutation_report(f"Unequip Slot `{slot}`", res), out_path=out_path)


@grp_item.command("db")
@click.argument("query")
@click.option(
    "--type",
    "db_type",
    type=click.Choice(["UNIQUE", "RARE"], case_sensitive=False),
    default="UNIQUE",
    show_default=True,
    help="PoB item database kind.",
)
@click.option("--slot", help="Target equipment slot when equipping/comparing.")
@click.option("--index", "item_index", type=int, help="1-based match index to select when equipping.")
@click.option("--equip", is_flag=True, help="Try equipping selected DB item (what-if unless --apply).")
@click.option("--apply", is_flag=True, help="Persist equipped DB item into build (implies --equip).")
@click.option("--limit", type=int, default=15, show_default=True, help="Maximum matches to return.")
@click.option("--with-tooltips", "with_tooltips", is_flag=True, help="Fetch native PoB tooltips for search matches (slower).")
@click.option("--with-raw", "with_raw", is_flag=True, help="Include raw PoE item text in search listing.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_db(
    query: str,
    db_type: str,
    slot: str | None,
    item_index: int | None,
    equip: bool,
    apply: bool,
    limit: int,
    with_tooltips: bool,
    with_raw: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Search PoB's built-in Unique/Rare item database (EN/ZH) and optionally try/equip a match."""
    from .formatters import render_table, strip_pob_escapes
    from .guardrails import GuardrailScope, check_input

    check_input(query, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")

    res = _daemon_call(
        "item_db",
        {
            "target": target,
            "query": query,
            "db_type": db_type.upper(),
            "slot": slot,
            "index": item_index,
            "equip": bool(equip or apply),
            "apply": apply,
            "limit": limit,
            "with_tooltips": with_tooltips,
            "with_raw": with_raw,
        },
        timeout=60.0,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    if not res.get("equipped"):
        rows = []
        for m in res.get("matches", []):
            title = strip_pob_escapes(m.get("titleZh") or m.get("title") or m.get("nameZh") or m.get("name") or "-")
            base = strip_pob_escapes(m.get("baseNameZh") or m.get("baseName") or m.get("type") or "-")
            rows.append([m.get("index"), title, base, m.get("primarySlot") or "-", m.get("league") or "-"])
        body = (
            f"### PoB Item DB Search (`{res.get('dbType')}`): `{query}` ({res.get('total', 0)} hits)\n"
            + render_table(["#", "Title", "Base", "Primary Slot", "League"], rows)
        )
        _emit_text(body, out_path=out_path)
        return

    sel = res.get("selectedItem") or {}
    sel_name = strip_pob_escapes(sel.get("titleZh") or sel.get("title") or sel.get("name") or query)
    lines = [f"### Item DB Equip: {sel_name} (Slot: `{res.get('slotName') or 'Auto'}`)"]
    tip = res.get("tooltip") or {}
    t_lines = tip.get("lines") or []
    if t_lines:
        lines.append("\n#### Native PoB Tooltip")
        for ln in t_lines:
            txt = (
                strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
                if isinstance(ln, dict)
                else strip_pob_escapes(str(ln))
            )
            if txt:
                lines.append(f"  {txt}")
    if res.get("mutation"):
        lines.append("\n" + _render_mutation_report(f"Item DB Simulation ({sel_name})", res["mutation"]))
    _emit_text("\n".join(lines), out_path=out_path)


@grp_item.command("flask")
@click.argument("index", type=str)
@click.argument("state", type=click.Choice(["on", "off"], case_sensitive=False))
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_flask(
    index: str,
    state: str,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Toggle Flask 1..5 or `all` on or off."""
    res = _daemon_call(
        "item_flask",
        {
            "target": target,
            "flask_index": index,
            "active": state.lower() == "on",
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_render_mutation_report(f"Flask {index} -> {state.upper()}", res), out_path=out_path)


@grp_item.command("anoint")
@click.option("--slot", default="Amulet", show_default=True, help="Equipment slot to anoint.")
@click.option("--notable", "--oil", "notable", required=True, help="Notable passive name (Chinese or English).")
@click.option("--index", "anoint_idx", type=int, default=1, show_default=True, help="Anoint slot index (1..4).")
@click.option("--apply", is_flag=True, help="Persist anoint into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_anoint(
    slot: str,
    notable: str,
    anoint_idx: int,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Anoint an equipped item via PoB's native item_edit_popup workflow."""
    from .formatters import strip_pob_escapes
    from .guardrails import GuardrailScope, check_input

    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(notable, scope=GuardrailScope.NOTABLE_OR_OIL, expected_game="poe1")

    res = _daemon_call(
        "item_anoint",
        {
            "target": target,
            "slot": slot,
            "notable": notable,
            "index": anoint_idx,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        notable_obj = res.get("detail", {}).get("notable", {})
        lbl = strip_pob_escapes(notable_obj.get("labelZh") or notable_obj.get("label") or notable)
        _emit_text(_render_mutation_report(f"Anoint {slot} (#{anoint_idx}) -> {lbl}", res), out_path=out_path)


def _extract_trap_opt(raw_args: tuple[str, ...], names: tuple[str, ...]) -> str | None:
    args_list = list(raw_args)
    for idx, tok in enumerate(args_list):
        if tok in names and idx + 1 < len(args_list):
            return args_list[idx + 1]
        for nm in names:
            if tok.startswith(f"{nm}="):
                return tok.split("=", 1)[1]
    return None


@grp_item.command(
    "charm",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    short_help="[TRAP -> pob item flask] PoE1 uses Flask 1..5, not PoE2 belt Charms.",
)
@click.argument("raw_args", nargs=-1, type=click.UNPROCESSED)
def cmd_item_charm_trap(raw_args: tuple[str, ...]) -> None:
    """Intentional reverse guardrail trap for PoE2 `item charm` invocations in `pob`."""
    from .errors import CrossGameHallucinationError

    pos = [a for a in raw_args if not a.startswith("-")]
    idx_tok = pos[0] if pos and pos[0] in ("1", "2", "3", "4", "5", "all") else "1"
    state_tok = pos[1].lower() if len(pos) >= 2 and pos[1].lower() in ("on", "off") else "on"
    raise CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger=" ".join(["pob item charm", *raw_args]).strip(),
        error="Belt `Charm 1..3` slots (`护符`/`咒符`) are a PoE2 mechanic; Path of Exile 1 does not have belt Charm slots.",
        remediation=(
            "In PoE1, toggle `Flask 1..5` via `pob item flask <1..5|all> <on|off>`, "
            "or run `pob2 item charm` in `F:/poe2` for PoE2 builds."
        ),
        equivalent_command=f"pob item flask {idx_tok} {state_tok}",
        rule_id="CLI_TRAP_ITEM_CHARM_IN_POB1",
    )


@grp_item.command(
    "instill",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    short_help="[TRAP -> pob item anoint] PoE1 uses Blight Oils (`anoint`), not PoE2 `instill`.",
)
@click.argument("raw_args", nargs=-1, type=click.UNPROCESSED)
def cmd_item_instill_trap(raw_args: tuple[str, ...]) -> None:
    """Intentional reverse guardrail trap for PoE2 `item instill` invocations in `pob`."""
    from .errors import CrossGameHallucinationError

    slot_val = _extract_trap_opt(raw_args, ("--slot",))
    notable_val = _extract_trap_opt(raw_args, ("--notable",))
    slot_part = f"--slot {slot_val} " if slot_val else ""
    notable_part = f'--notable "{notable_val}"' if notable_val else '--notable "<name>"'
    raise CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger=" ".join(["pob item instill", *raw_args]).strip(),
        error="Amulet Instilling (`涂灌` with Delirium Distilled/Liquid Emotions) is a PoE2 mechanic.",
        remediation=(
            "In PoE1, anoint amulets/rings with Blight Oils (`圣油`/`涂油`) via `pob item anoint`, "
            "or run `pob2 item instill` in `F:/poe2` for PoE2 builds."
        ),
        equivalent_command=f"pob item anoint {slot_part}{notable_part}".strip(),
        rule_id="CLI_TRAP_ITEM_INSTILL_IN_POB1",
    )


@main.command(
    "rune",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    short_help="[TRAP -> pob bench] PoE1 uses Hideout Crafting Bench, not PoE2 equipment Runes.",
)
@click.argument("raw_args", nargs=-1, type=click.UNPROCESSED)
def cmd_rune_trap(raw_args: tuple[str, ...]) -> None:
    """Intentional reverse guardrail trap for PoE2 `rune` invocations in `pob`."""
    from .errors import CrossGameHallucinationError

    slot_val = _extract_trap_opt(raw_args, ("--slot",)) or "helmet"
    stat_val = _extract_trap_opt(raw_args, ("--stat", "--search"))
    if stat_val:
        equiv = f'pob bench suggest --slot {slot_val} --stat "{stat_val}"'
    else:
        equiv = f"pob bench list --slot {slot_val}"
    raise CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger=" ".join(["pob rune", *raw_args]).strip(),
        error="Path of Exile 1 does not have PoE2 equipment Rune/Soul Core sockets (`pob2 rune` / `ModRunes.lua`).",
        remediation=(
            "In PoE1, gear crafting uses the Hideout Crafting Bench (`{crafted}` via `pob bench list` / `pob bench suggest`), "
            "or run `pob2 rune` in `F:/poe2` for PoE2."
        ),
        equivalent_command=equiv,
        rule_id="CLI_TRAP_RUNE_IN_POB1",
    )


@main.command(
    "instill",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    short_help="[TRAP -> pob item anoint] PoE1 uses Blight Oils (`pob item anoint`), not PoE2 `instill`.",
)
@click.argument("raw_args", nargs=-1, type=click.UNPROCESSED)
def cmd_instill_trap(raw_args: tuple[str, ...]) -> None:
    """Intentional reverse guardrail trap for PoE2 `instill` invocations in `pob`."""
    from .errors import CrossGameHallucinationError

    slot_val = _extract_trap_opt(raw_args, ("--slot",))
    notable_val = _extract_trap_opt(raw_args, ("--notable",))
    slot_part = f"--slot {slot_val} " if slot_val else ""
    notable_part = f'--notable "{notable_val}"' if notable_val else '--notable "<name>"'
    raise CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger=" ".join(["pob instill", *raw_args]).strip(),
        error="Amulet Instilling (`涂灌` with Delirium Distilled/Liquid Emotions) is a PoE2 mechanic.",
        remediation=(
            "In PoE1, anoint items with Blight Oils (`圣油`/`涂油`) via `pob item anoint`, "
            "or run `pob2 item instill` in `F:/poe2` for PoE2."
        ),
        equivalent_command=f"pob item anoint {slot_part}{notable_part}".strip(),
        rule_id="CLI_TRAP_INSTILL_IN_POB1",
    )


@main.group("skill")
def grp_skill() -> None:
    """Modify, add, or replace skill groups and support/active gems."""
    pass


@grp_skill.command("set")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", "--gem-name", "gem", help="Gem 1-based index or name within group.")
@click.option("--new-gem", "--name", "new_gem", help="Replace gem with a new gem name (English or Chinese).")
@click.option("--level", type=int, help="Gem level.")
@click.option("--quality", type=int, help="Gem quality.")
@click.option("--enabled", type=click.Choice(["true", "false"], case_sensitive=False), help="Enable/disable gem or group.")
@click.option("--count", type=int, help="Skill/minion count.")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_set(
    group: str,
    gem: str | None,
    new_gem: str | None,
    level: int | None,
    quality: int | None,
    enabled: str | None,
    count: int | None,
    full_dps: str | None,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Modify gem level/quality/enabled/count/gemId or group FullDPS state."""
    if new_gem:
        from .guardrails import GuardrailScope, check_input

        check_input(new_gem, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    res = _daemon_call(
        "skill_set",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "new_gem": new_gem,
            "level": level,
            "quality": quality,
            "enabled": (enabled.lower() == "true") if enabled is not None else None,
            "count": count,
            "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_render_mutation_report(f"Skill Set (Group: {group}, Gem: {gem or 'Group'})", res), out_path=out_path)


@grp_skill.command("add")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", "--gem-name", "--name", "gem", required=True, help="Gem name (English or Chinese).")
@click.option("--level", type=int, default=20, show_default=True)
@click.option("--quality", type=int, default=20, show_default=True)
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_add(
    group: str,
    gem: str,
    level: int,
    quality: int,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Add a gem to a skill group."""
    from .guardrails import GuardrailScope, check_input

    check_input(gem, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    res = _daemon_call(
        "skill_add",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "level": level,
            "quality": quality,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(
            _render_mutation_report(f"Add Gem '{gem}' ({level}/{quality}%) to Group '{group}'", res),
            out_path=out_path,
        )


@grp_skill.command("rm")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", "--gem-name", "--name", "gem", required=True, help="Gem 1-based index or name to remove.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_rm(
    group: str,
    gem: str,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Remove a gem from a skill group."""
    res = _daemon_call(
        "skill_rm",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_render_mutation_report(f"Remove Gem '{gem}' from Group '{group}'", res), out_path=out_path)


@grp_skill.command("new-group")
@click.option("--label", help="Skill group label.")
@click.option("--slot", help="Item slot for the skill group (e.g. 'Body Armour', 'Helmet').")
@click.option("--gem", "gem_specs", multiple=True, help="Gem spec 'Name[:level[:quality]]' (repeatable).")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--enabled", type=click.Choice(["true", "false"], case_sensitive=False), help="Enable/disable group.")
@click.option("--apply", is_flag=True, help="Persist new skill group into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_new_group(
    label: str | None,
    slot: str | None,
    gem_specs: tuple[str, ...],
    full_dps: str | None,
    enabled: str | None,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Create a new skill group and optionally populate its linked gems in one transaction."""
    from .guardrails import GuardrailScope, check_input

    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    gems = [_parse_gem_spec(g) for g in gem_specs] if gem_specs else None
    for g_item in gems or []:
        if g_item.get("name"):
            check_input(str(g_item["name"]), scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    res = _daemon_call(
        "skill_new_group",
        {
            "target": target,
            "gems": gems,
            "label": label,
            "slot": slot,
            "enabled": (enabled.lower() == "true") if enabled is not None else True,
            "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_render_mutation_report(f"New Skill Group ({label or slot or 'Unnamed'})", res), out_path=out_path)


@grp_skill.command("replace-group")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gems", "gems_json", help="JSON array of gem objects or 'Name[:lvl[:qual]]' strings.")
@click.option("--gem", "gem_specs", multiple=True, help="Gem spec 'Name[:level[:quality]]' (repeatable).")
@click.option("--label", help="Optional new group label.")
@click.option("--slot", help="Optional equipment slot for the skill group.")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--apply", is_flag=True, help="Persist replaced skill group into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_replace_group(
    group: str,
    gems_json: str | None,
    gem_specs: tuple[str, ...],
    label: str | None,
    slot: str | None,
    full_dps: str | None,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Atomically replace all gems in a skill group in ONE daemon call."""
    parsed_gems: list[dict[str, Any]] = []
    if gems_json:
        raw_list = _read_json_input(None, gems_json, label="--gems")
        if not isinstance(raw_list, list):
            raise click.UsageError("--gems must be a JSON array.")
        for entry in raw_list:
            if isinstance(entry, str):
                parsed_gems.append(_parse_gem_spec(entry))
            elif isinstance(entry, dict):
                parsed_gems.append(entry)
            else:
                raise click.UsageError(f"Unsupported gem entry in --gems: {entry!r}")
    for spec in gem_specs:
        parsed_gems.append(_parse_gem_spec(spec))

    if not parsed_gems:
        raise click.UsageError("Provide at least one gem via --gem 'Name[:lvl[:qual]]' or --gems '[...]'.")

    from .guardrails import GuardrailScope, check_input

    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    for g_item in parsed_gems:
        g_name = str(g_item.get("name") or g_item.get("nameSpec") or g_item.get("gem") or "").strip()
        if g_name:
            check_input(g_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)

    res = _daemon_call(
        "skill_replace_group",
        {
            "target": target,
            "group": group,
            "gems": parsed_gems,
            "label": label,
            "slot": slot,
            "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(
            _render_mutation_report(f"Replace Skill Group '{group}' ({len(parsed_gems)} gems)", res),
            out_path=out_path,
        )


@main.group("tree", invoke_without_command=True)
@click.option("--search", "searches", multiple=True, help="Search passive tree nodes by EN/ZH name, stat, ID, or '#1' (repeatable).")
@click.option("--type", "node_types", multiple=True, help="Filter by node type (Keystone, Notable, Mastery, Socket, Ascendancy; repeatable).")
@click.option("--allocated/--unallocated", "allocated", default=None, help="Filter to allocated or unallocated nodes.")
@click.option("--max-cost", "max_cost", type=int, default=None, help="Maximum shortest-path point cost.")
@click.option("--show-path", "show_path", is_flag=True, help="Include shortest allocation path node IDs.")
@click.option("--masteries", "masteries", is_flag=True, help="Include full masteryEffects IDs and text without truncation.")
@click.option("--socket-radius", "socket_radius", type=str, default=None, help="Inspect nodes within jewel radii of socket (ID, '#1', or name).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def grp_tree(
    ctx: click.Context,
    searches: tuple[str, ...],
    node_types: tuple[str, ...],
    allocated: bool | None,
    max_cost: int | None,
    show_path: bool,
    masteries: bool,
    socket_radius: str | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Inspect passive tree (`pob tree [--search ...]`), mutate nodes (`change`), or switch class/ascendancy (`set-class`)."""
    if ctx.invoked_subcommand is None:
        _run_tree_inspect(
            searches=searches,
            node_types=node_types,
            allocated=allocated,
            max_cost=max_cost,
            show_path=show_path,
            masteries=masteries,
            socket_radius=socket_radius,
            target=target,
            as_json=as_json,
            compact=compact,
            out_path=out_path,
        )
        return

    from click.core import ParameterSource

    ctx.ensure_object(dict)
    tree_opts: dict[str, Any] = {}
    if ctx.get_parameter_source("target") == ParameterSource.COMMANDLINE:
        tree_opts["target"] = target
    if ctx.get_parameter_source("as_json") == ParameterSource.COMMANDLINE:
        tree_opts["as_json"] = as_json
    if ctx.get_parameter_source("compact") == ParameterSource.COMMANDLINE:
        tree_opts["compact"] = compact
    if ctx.get_parameter_source("out_path") == ParameterSource.COMMANDLINE:
        tree_opts["out_path"] = out_path
    ctx.obj["tree_group_opts"] = tree_opts


def _resolve_tree_subcommand_opts(
    ctx: click.Context,
    *,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> tuple[str, bool, bool, str | None]:
    """Propagate CLI-provided `pob tree` group options into `change` / `set-class` when not overridden."""
    from click.core import ParameterSource

    parent_opts = (ctx.obj.get("tree_group_opts") if isinstance(ctx.obj, dict) else None) or {}
    if "target" in parent_opts and ctx.get_parameter_source("target") != ParameterSource.COMMANDLINE:
        target = parent_opts["target"]
    if "as_json" in parent_opts and ctx.get_parameter_source("as_json") != ParameterSource.COMMANDLINE:
        as_json = bool(parent_opts["as_json"])
    if "compact" in parent_opts and ctx.get_parameter_source("compact") != ParameterSource.COMMANDLINE:
        compact = bool(parent_opts["compact"])
    if "out_path" in parent_opts and ctx.get_parameter_source("out_path") != ParameterSource.COMMANDLINE:
        out_path = parent_opts["out_path"]
    return target, as_json, compact, out_path


@grp_tree.command("change")
@click.option("--allocate", help="Comma-separated node IDs, names, or Mastery `<nodeId>:<effectId>` to allocate.")
@click.option("--deallocate", help="Comma-separated node IDs or names to deallocate.")
@click.option(
    "--allow-cascade/--no-allow-cascade",
    "allow_cascade",
    default=True,
    show_default=True,
    help="Allow deallocations that cascade-orphan >3 downstream passive nodes.",
)
@click.option("--apply", is_flag=True, help="Persist passive tree changes into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_change(
    ctx: click.Context,
    allocate: str | None,
    deallocate: str | None,
    allow_cascade: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Allocate or deallocate passive tree nodes idempotently (R12)."""
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    alloc_list = [x.strip() for x in allocate.split(",") if x.strip()] if allocate else []
    dealloc_list = [x.strip() for x in deallocate.split(",") if x.strip()] if deallocate else []
    res = _daemon_call(
        "tree_change",
        {
            "target": target,
            "allocate": alloc_list,
            "deallocate": dealloc_list,
            "allow_cascade": allow_cascade,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(_render_mutation_report("Passive Tree Change", res), out_path=out_path)


@grp_tree.command("set-class")
@click.argument("class_name", required=False)
@click.argument("ascendancy", required=False)
@click.option("--secondary-asc", "secondary_asc", help="Secondary / Bloodline ascendancy name (or 'None' to clear).")
@click.option("--apply", is_flag=True, help="Persist class/ascendancy change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_set_class(
    ctx: click.Context,
    class_name: str | None,
    ascendancy: str | None,
    secondary_asc: str | None,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Switch character class, primary ascendancy, and/or secondary (Bloodline) ascendancy."""
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    if not class_name and not ascendancy and not secondary_asc:
        raise click.UsageError("Specify class_name, ascendancy, and/or --secondary-asc.")

    from .guardrails import check_poe2_class_or_ascendancy

    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_asc, field="secondary_ascendancy")

    res = _daemon_call(
        "tree_set_class",
        {
            "target": target,
            "class_name": class_name,
            "ascendancy_name": ascendancy,
            "secondary_ascendancy_name": secondary_asc,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    detail = res.get("detail") or {}
    changes = detail.get("changes") or []
    title = f"Set Class / Ascendancy ({'; '.join(changes) or 'no-op'})"
    _emit_text(_render_mutation_report(title, res), out_path=out_path)


@main.group("config")
def grp_config() -> None:
    """Inspect or modify PoB ConfigTab parameters and customMods."""
    pass


@grp_config.command("list")
@click.option("--all", "include_defaults", is_flag=True, help="Include default/inactive config variables.")
@click.option("--search", "search", help="Filter config variables by case-insensitive EN/ZH substring.")
@click.option("--with-options", "with_options", is_flag=True, help="Include full dropdown options arrays in JSON.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_config_list(
    include_defaults: bool,
    search: str | None,
    with_options: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """List active (non-default) ConfigTab variables and raw customMods."""
    from .formatters import fmt_num, render_table

    if search:
        from .guardrails import GuardrailScope, check_input

        check_input(search, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")

    res = _daemon_call(
        "config_list",
        {
            "target": target,
            "includeDefaults": include_defaults,
            "search": search,
            "with_options": with_options,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    active_set = res.get("activeConfigSet") or {}
    set_title = active_set.get("title") or res.get("activeConfigSetId") or "Default"
    filter_tag = f" | Filter: `{search}`" if search else ""
    lines = [
        f"### ConfigTab (`{set_title}`) — `{res.get('activeCount', 0)}` active / `{res.get('totalCount', 0)}` total{filter_tag}",
    ]
    rows = [
        [
            v.get("sectionZh") or v.get("section") or "-",
            v.get("var") or "-",
            fmt_num(v.get("value")),
            v.get("labelZh") or v.get("label") or "-",
        ]
        for v in res.get("variables", [])
    ]
    lines.append(render_table(["Section", "Var", "Value", "Label"], rows))

    cm_text = (res.get("customModsText") or "").strip()
    if cm_text:
        lines.append("\n#### Custom Modifiers\n```text\n" + cm_text + "\n```")
    _emit_text(_prepend_active_guardrail_warnings("\n".join(lines)), out_path=out_path)


@grp_config.command("set")
@click.argument("pairs", nargs=-1, required=False)
@click.option("--custom-mods", "custom_mods", help="Replace custom modifiers text (or 'clear' to disable).")
@click.option("--append-mods", "append_mods", help="Append custom modifier line(s) without overwriting existing customMods.")
@click.option("--apply", is_flag=True, help="Persist config changes into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_config_set(
    pairs: tuple[str, ...],
    custom_mods: str | None,
    append_mods: str | None,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Set/reset ConfigTab variables (`key=value` or `key=reset`), replace `--custom-mods`, or `--append-mods`."""
    if not pairs and custom_mods is None and append_mods is None:
        raise click.UsageError("Provide at least one `key=value` assignment, `--custom-mods`, or `--append-mods`.")
    assignments: list[list[str]] = []
    for p in pairs:
        if "=" not in p:
            raise click.UsageError(f"Invalid config assignment '{p}'; expected key=value.")
        k, v = p.split("=", 1)
        assignments.append([k.strip(), v.strip()])

    from .guardrails import GuardrailScope, check_input

    for k, _ in assignments:
        check_input(k, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    if custom_mods is not None and custom_mods.strip().lower() not in ("clear", "reset", "none", "disable", "disabled", ""):
        check_input(custom_mods, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    if append_mods is not None:
        check_input(append_mods, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")

    res = _daemon_call(
        "config_set",
        {
            "target": target,
            "assignments": assignments,
            "custom_mods": custom_mods,
            "appendCustomMods": append_mods,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        desc_parts = list(pairs)
        if custom_mods is not None:
            desc_parts.append(f"customMods={custom_mods!r}")
        if append_mods is not None:
            desc_parts.append(f"appendMods={append_mods!r}")
        _emit_text(_render_mutation_report(f"Config Set ({', '.join(desc_parts)})", res), out_path=out_path)


@main.command("batch")
@click.option("--file", "file_path", type=str, help="JSON file containing batch operations list (or '-' for stdin).")
@click.option("--json-ops", "json_ops", help="Inline JSON array of batch operations (or '@path').")
@click.option("--apply", is_flag=True, help="Persist batch changes into build.")
@click.option("--no-undo", "no_undo", is_flag=True, help="Skip undo snapshot overhead when --apply is set.")
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed items in audit/matrix/Pinnacle scenarios.",
)
@click.option(
    "--return",
    "return_spec",
    type=str,
    default=None,
    help="Comma-separated readouts to attach (e.g. 'summary,audit,audit:full,matrix,calc:DPS;Mana,export').",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_batch(
    file_path: str | None,
    json_ops: str | None,
    apply: bool,
    no_undo: bool,
    keep: tuple[str, ...],
    return_spec: str | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Execute multiple item/skill/tree/config/build mutations in a single snapshot transaction."""
    parsed = _read_json_input(file_path, json_ops, label="batch ops")
    ops = parsed.get("ops") if isinstance(parsed, dict) else parsed
    if not isinstance(ops, list):
        raise click.UsageError('Batch input must be a JSON list of operation objects (or `{"ops": [...]}`).')

    json_keep = parsed.get("keep") if isinstance(parsed, dict) else None
    eff_keep = _merge_keep_tokens(json_keep, keep)
    return_sections = [s.strip() for s in (return_spec or "").split(",") if s.strip()] or None
    params: dict[str, Any] = {
        "target": target,
        "ops": ops,
        "apply": apply,
        "no_undo": no_undo,
        "return_sections": return_sections,
    }
    if eff_keep:
        params["keep"] = eff_keep
    res = _daemon_call("batch", params, timeout=300.0)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    parts = [_render_mutation_report(f"Batch Execution ({len(ops)} steps)", res)]
    returns = res.get("returns")
    if isinstance(returns, dict) and returns:
        from .formatters import format_matrix_markdown

        parts.append("\n#### Attached Returns")
        for sec_name, sec_val in returns.items():
            if sec_name == "matrix" and isinstance(sec_val, dict):
                parts.append(format_matrix_markdown(sec_val, target=target))
            elif sec_name == "audit" and isinstance(sec_val, dict):
                parts.append("- **audit**: attached (`--json` for full payload)")
                r_warn = _restore_warning(sec_val)
                if r_warn:
                    parts.append(r_warn)
                stripped = sec_val.get("inflationStripped")
                if isinstance(stripped, dict):
                    kept = stripped.get("kept") or []
                    if kept:
                        parts.append("**Kept (--keep)**: " + "; ".join(kept))
                    for line in _keep_rejected_warning(stripped):
                        parts.append(line)
            elif sec_name == "export":
                code_str = (
                    sec_val
                    if isinstance(sec_val, str)
                    else (str(sec_val.get("code") or "") if isinstance(sec_val, dict) else "")
                )
                if code_str:
                    preview = f"{code_str[:64]}..." if len(code_str) > 64 else code_str
                    parts.append(f"- **export**: `{preview}` ({len(code_str)} chars)")
                else:
                    parts.append("- **export**: _(empty)_")
            else:
                parts.append(f"- **{sec_name}**: attached (`--json` for full payload)")
    _emit_text("\n".join(parts), out_path=out_path)


@main.command("sweep")
@click.option("--file", "file_path", type=str, help="JSON file of sweep variants (or '-' for stdin).")
@click.option("--json-variants", "json_variants", help="Inline JSON array of variants (or '@path').")
@click.option("--base-ops", "base_ops_str", help="Optional base ops JSON array (or '@path') applied once before variants.")
@click.option("--calc", "calc_keywords", multiple=True, help="Calcs keyword(s) to extract per variant (repeatable).")
@click.option("--scenario", "scenario", help="Optional baseline scenario (e.g. Pinnacle, Uber, FlasksOff).")
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed items in audit/matrix/Pinnacle scenarios.",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_sweep(
    file_path: str | None,
    json_variants: str | None,
    base_ops_str: str | None,
    calc_keywords: tuple[str, ...],
    scenario: str | None,
    keep: tuple[str, ...],
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Evaluate N named build variants in one warm daemon worker with fault isolation and 0-residue rollback."""
    from .formatters import format_sweep_markdown

    parsed = _read_json_input(file_path, json_variants, label="sweep variants")
    base_ops: list[dict[str, Any]] | None = None
    eff_calcs = list(calc_keywords)
    eff_scenario: Any = scenario
    json_keep: Any = None

    if isinstance(parsed, dict):
        variants = parsed.get("variants")
        json_keep = parsed.get("keep")
        if base_ops_str is None and isinstance(parsed.get("base_ops") or parsed.get("baseOps"), list):
            base_ops = parsed.get("base_ops") or parsed.get("baseOps")
        if not eff_calcs and isinstance(parsed.get("calc_keywords") or parsed.get("calcKeywords"), list):
            eff_calcs = [str(x) for x in (parsed.get("calc_keywords") or parsed.get("calcKeywords"))]
        if eff_scenario is None and parsed.get("scenario"):
            raw_sc = parsed["scenario"]
            eff_scenario = raw_sc if isinstance(raw_sc, dict) else str(raw_sc)
    else:
        variants = parsed

    if not isinstance(variants, list):
        raise click.UsageError('Sweep input must be a JSON list of variant objects (or `{"variants": [...]}`).')

    if base_ops_str:
        parsed_base = _read_json_input(None, base_ops_str, label="--base-ops")
        if isinstance(parsed_base, dict) and isinstance(parsed_base.get("ops"), list):
            base_ops = parsed_base["ops"]
        elif isinstance(parsed_base, list):
            base_ops = parsed_base
        else:
            raise click.UsageError("--base-ops must be a JSON list of operation objects.")

    eff_keep = _merge_keep_tokens(json_keep, keep)
    params: dict[str, Any] = {
        "target": target,
        "variants": variants,
        "base_ops": base_ops,
        "calc_keywords": eff_calcs or None,
        "scenario": eff_scenario,
    }
    if eff_keep:
        params["keep"] = eff_keep
    res = _daemon_call("sweep", params, timeout=300.0)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_sweep_markdown(res, target=target), out_path=out_path)


@main.command("matrix")
@click.argument("scenarios", nargs=-1, required=False)
@click.option(
    "-s",
    "--scenario",
    "opt_scenarios",
    multiple=True,
    help="Scenario spec (e.g. Original, Pinnacle, Uber, FlasksOff, 'Wither15:Pinnacle,MultiplierWitheredStackCount=15').",
)
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed items in Pinnacle/Uber scenarios (repeatable or comma-separated).",
)
@click.option("--full", "full_payload", is_flag=True, help="Include full raw 200-key stats & headers per scenario (default is compact).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_matrix(
    scenarios: tuple[str, ...],
    opt_scenarios: tuple[str, ...],
    keep: tuple[str, ...],
    full_payload: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Run multi-scenario what-if comparison in one atomic 0-residue transaction (default: Original/Pinnacle/Uber/FlasksOff)."""
    from .formatters import format_matrix_markdown

    all_specs = [*scenarios, *opt_scenarios] or None
    params: dict[str, Any] = {"target": target, "scenarios": all_specs, "full": full_payload}
    if keep:
        params["keep"] = list(keep)
    res = _daemon_call("matrix", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_matrix_markdown(res, target=target), out_path=out_path)


# --- 4.4 ROI Rankings & Cross-Build Comparison ---


@main.command("rank-nodes")
@click.option("--stat", default="CombinedDPS", show_default=True, help="Target stat (CombinedDPS, FullDPS, TotalEHP, Life, etc.).")
@click.option("--max-dist", type=int, default=5, show_default=True, help="Maximum path distance in points.")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top candidates to show.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_rank_nodes(
    stat: str,
    max_dist: int,
    top: int,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Rank unallocated passive tree nodes by ROI per point (`pathPower`) and total gain (`power`)."""
    from .formatters import fmt_num, render_table, strip_pob_escapes

    res = _daemon_call(
        "rank_nodes",
        {"target": target, "stat": stat, "max_dist": max_dist, "top": top},
        timeout=180.0,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    rows = []
    for idx, r in enumerate(res.get("byPerPoint", []), start=1):
        rows.append(
            [
                idx,
                r.get("id"),
                strip_pob_escapes(r.get("nameZh") or r.get("name")),
                r.get("type"),
                r.get("pathDist"),
                strip_pob_escapes(r.get("pathPowerStr") or fmt_num(r.get("pathPower"), 3)),
                strip_pob_escapes(r.get("powerStr") or fmt_num(r.get("power"), 3)),
            ]
        )
    body = (
        f"### Passive Node ROI Ranking (Stat: `{res.get('stat')}`, Max Dist: `{max_dist}` pts)\n"
        + render_table(["#", "Node ID", "Name", "Type", "Cost", "Per-Point Gain", "Total Gain"], rows)
    )
    _emit_text(body, out_path=out_path)


@main.command("rank-gems")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--slot", "slot_idx", type=int, help="1-based gem slot index to replace.")
@click.option("--add-link", is_flag=True, help="Rank as an additional support link instead of replacing a slot.")
@click.option("--field", default="CombinedDPS", show_default=True, help="DPS field to sort by (CombinedDPS, FullDPS, TotalDPS, etc.).")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top gems to return.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_rank_gems(
    group: str,
    slot_idx: int | None,
    add_link: bool,
    field: str,
    top: int,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Rank support/active candidate gems for a skill group by DPS gain."""
    from .formatters import fmt_num, render_table, strip_pob_escapes

    res = _daemon_call(
        "rank_gems",
        {
            "target": target,
            "group": group,
            "slot_idx": slot_idx,
            "add_link": add_link,
            "field": field,
            "top": top,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    cands = res.get("candidates", [])
    has_caveats = any(gm.get("caveat") for gm in cands)
    rows = []
    for idx, gm in enumerate(cands, start=1):
        pct = gm.get("pct")
        pct_str = f"{float(pct):+.1f}%" if pct is not None else "-"
        row_g: list[Any] = [
            idx,
            strip_pob_escapes(gm.get("nameZh") or gm.get("name")),
            strip_pob_escapes(gm.get("name")),
            fmt_num(gm.get("dps")),
            fmt_num(gm.get("delta")),
            pct_str,
        ]
        if has_caveats:
            row_g.append(str(gm.get("caveat") or "-"))
        rows.append(row_g)
    headers_g = ["#", "Gem (ZH)", "Gem (EN)", "New DPS", "Delta", "Gain %"] + (["Caveat"] if has_caveats else [])
    body = (
        f"### Candidate Gem Ranking — Group #{res.get('groupIndex')} `{strip_pob_escapes(res.get('groupLabel'))}` ({res.get('mode')}, Base `{res.get('dpsField')}`: `{fmt_num(res.get('baseDps'))}`)\n"
        + render_table(headers_g, rows)
    )
    skipped = res.get("filteredArchetypeSwaps") or []
    if skipped:
        body += f"\n_Filtered archetype-changing / invalid supports: {', '.join(skipped[:8])}_"
    _emit_text(body, out_path=out_path)


@main.command("compare")
@click.argument("ref_source", required=False)
@_target_option(allow_slot_alias=True)
@click.option(
    "--ref-target",
    "ref_target",
    type=_SLOT_SLUG_TYPE,
    default="ref",
    show_default=True,
    help="Reference build slot slug (default: 'ref').",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_compare(
    ref_source: str | None,
    target: str,
    ref_target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Compare target build vs reference build across 3 baselines + tree/skill/equipment diffs."""
    from .formatters import render_table, strip_pob_escapes

    params: dict[str, Any] = {"target": target, "ref_target": ref_target}
    if ref_source:
        resolved_src, resolved_xml_code = _prepare_load_source(ref_source)
        if resolved_xml_code is not None:
            params["ref_xml_code"] = resolved_xml_code
        else:
            params["ref_source"] = resolved_src

    res = _daemon_call("compare", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    m_info = res.get("mainBuildInfo") or {}
    r_info = res.get("refBuildInfo") or {}
    lines = [
        f"### A/B Build Comparison: `{target}` (Lv.{m_info.get('level')} {m_info.get('ascendClassNameZh') or m_info.get('ascendClassName')}) vs `{ref_target}` (Lv.{r_info.get('level')} {r_info.get('ascendClassNameZh') or r_info.get('ascendClassName')})",
    ]

    baselines = res.get("baselines") or {}
    for title, rows in (
        ("Baseline 1: Original Panels (As Configured)", baselines.get("original", [])),
        (f"Baseline 2: External Target Environment Aligned to `{target}`", (baselines.get("envAlignedToMain") or {}).get("rows", [])),
        ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", (baselines.get("pinnacleDewatered") or {}).get("rows", [])),
    ):
        lines.append(f"\n#### {title}")
        t_rows = [[r["label"], r["mainStr"], r["refStr"], r["diffRefVsMainStr"]] for r in rows]
        lines.append(render_table(["Metric", target, ref_target, f"{ref_target} vs {target}"], t_rows))

    td = res.get("treeDiff") or {}
    ks = td.get("keystones") or {}
    lines.append("\n#### Passive Tree Diff")
    lines.append(
        f"- **Keystones Only in `{target}`**: {', '.join(x['name'] for x in ks.get('onlyInMain', [])) or '-'}\n"
        f"- **Keystones Only in `{ref_target}`**: {', '.join(x['name'] for x in ks.get('onlyInRef', [])) or '-'}\n"
        f"- **Notables Shared**: `{(td.get('notables') or {}).get('sharedCount', 0)}` | **Only in `{target}`**: `{len((td.get('notables') or {}).get('onlyInMain', []))}` | **Only in `{ref_target}`**: `{len((td.get('notables') or {}).get('onlyInRef', []))}`"
    )

    sd = res.get("skillDiff") or {}
    m_mg = sd.get("mainGroupMain") or {}
    r_mg = sd.get("refGroupMain") or {}
    m_mg_lbl = strip_pob_escapes(m_mg.get("displayLabelZh") or m_mg.get("displayLabel") or "-")
    r_mg_lbl = strip_pob_escapes(r_mg.get("displayLabelZh") or r_mg.get("displayLabel") or "-")
    lines.append("\n#### Skill & Aura Diff")
    lines.append(
        f"- **`{target}` Primary Skill Group**: `#{m_mg.get('index', '-')}` {m_mg_lbl}\n"
        f"- **`{ref_target}` Primary Skill Group**: `#{r_mg.get('index', '-')}` {r_mg_lbl}\n"
        f"- **Active Skills Only in `{target}`**: {', '.join(sd.get('activeSkillsOnlyInMain', [])) or '-'}\n"
        f"- **Active Skills Only in `{ref_target}`**: {', '.join(sd.get('activeSkillsOnlyInRef', [])) or '-'}\n"
        f"- **Shared Active Skills**: {', '.join(sd.get('sharedActiveSkills', [])) or '-'}"
    )

    lines.append("\n#### Equipment Slot Diff")
    slot_rows = [[s["slot"], s["mainItem"], s["refItem"]] for s in res.get("itemSlotDiff", [])]
    lines.append(render_table(["Slot", f"{target} Item", f"{ref_target} Item"], slot_rows))

    _emit_text("\n".join(lines), out_path=out_path)


@main.command("borrow")
@click.option("--item-slot", "item_slot", help="Comma-separated equipment slot(s) to borrow from reference into target (e.g. 'Weapon 1,Amulet').")
@click.option("--skill-group", "skill_group", help="Skill group ('main', index, or label) to borrow from reference into target.")
@click.option("--apply", is_flag=True, help="Persist borrowed items/skills into target build.")
@_target_option(allow_slot_alias=True)
@click.option(
    "--ref-target",
    "ref_target",
    type=_SLOT_SLUG_TYPE,
    default="ref",
    show_default=True,
    help="Reference build slot slug (default: 'ref').",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_borrow(
    item_slot: str | None,
    skill_group: str | None,
    apply: bool,
    target: str,
    ref_target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Borrow items and/or a skill group from reference slot into target slot and compute exact delta."""
    slots = [x.strip() for x in item_slot.split(",") if x.strip()] if item_slot else []
    res = _daemon_call(
        "borrow",
        {
            "target": target,
            "ref_target": ref_target,
            "item_slots": slots,
            "skill_group": skill_group,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    notices = (res.get("detail") or {}).get("notices") or []
    report = _render_mutation_report(f"Borrow from Reference Build (`{ref_target}` -> `{target}`)", res)
    if notices:
        notice_block = "\n#### Transplant Notices\n" + "\n".join(f"- {n}" for n in notices)
        report += notice_block
    _emit_text(report, out_path=out_path)


# --- 4.5 Local Offline Game Data & External Economy/PoEDB/Wiki Queries ---


def _render_single_data_markdown(res: dict[str, Any], kind: str, query: str) -> str:
    from .formatters import render_table

    kind_norm = res.get("kind", kind.lower())
    results = res.get("results", [])
    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            canon = f" (canonical: {w['canonicalTerm']})" if w.get("canonicalTerm") else ""
            lines.append(f"> {tag} `{w.get('trigger', '')}`{canon}: {w.get('warning', '')}")
    alias = res.get("resolvedAlias")
    alias_note = f" (alias `{alias['from']}` -> `{alias['to']}`)" if isinstance(alias, dict) and alias.get("to") else ""
    lines.append(
        f"### Local Game Data (`{kind_norm}`): `{query}`{alias_note} — `{res.get('count', len(results))}` hits "
        f"(PoB `{res.get('pobVersion')}` / Data `{res.get('dataVersion')}`)"
    )
    if not results:
        lines.append("_(no matches)_")
        sugs = res.get("suggestions") or []
        if sugs:
            s_rows = [
                [s.get("name") or "-", s.get("zh_name") or "-", f"{float(s.get('score') or 0):.2f}"]
                for s in sugs
            ]
            lines.append("\n#### Did You Mean")
            lines.append(render_table(["Name (EN)", "Name (ZH)", "Score"], s_rows))
        if res.get("hint"):
            lines.append(f"\n> {res['hint']}")
        return "\n".join(lines)

    if kind_norm == "translate":
        rows = [[r.get("en"), r.get("zh") or "-", r.get("category") or "-", r.get("match_type") or "-"] for r in results]
        lines.append(render_table(["English", "Chinese", "Category", "Match"], rows))
    elif kind_norm == "gem":
        for r in results:
            zh_part = f" ({r['zh_name']})" if r.get("zh_name") else ""
            kind_lbl = "Support" if r.get("is_support") else "Active"
            legacy_tag = f" **[LEGACY / REMOVED IN {r.get('removedInPatch') or '3.25'}]**" if r.get("legacyOnly") else ""
            lines.append(f"\n#### {r.get('display_name') or r.get('name')}{zh_part} — `{r.get('color') or '-'}` {kind_lbl}{legacy_tag}")
            if r.get("legacyNote"):
                lines.append(f"> [POE1-PATCH-WARN] {r['legacyNote']}")
            meta_bits = []
            if r.get("tag_string"):
                meta_bits.append(f"Tags: {r['tag_string']}")
            if r.get("cast_time") is not None:
                meta_bits.append(f"Cast: {r['cast_time']}s")
            if r.get("crit_chance") is not None:
                meta_bits.append(f"Crit: {r['crit_chance']}%")
            if r.get("damage_effectiveness") is not None:
                meta_bits.append(f"DmgEff: {r['damage_effectiveness']}")
            if r.get("mana_multiplier") is not None:
                meta_bits.append(f"CostMult: {r['mana_multiplier']}%")
            if meta_bits:
                lines.append("- " + " | ".join(meta_bits))

            lvls = r.get("levels") or {}
            if lvls:
                lvl_rows = []
                for lk in sorted(lvls.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
                    lv = lvls[lk]
                    cost_s = ", ".join(f"{k}:{v}" for k, v in (lv.get("cost") or {}).items()) or "-"
                    stats_s = "; ".join(f"{k}={v}" for k, v in list((lv.get("stats") or {}).items())[:6]) or "-"
                    lvl_rows.append(
                        [
                            f"Lv.{lk}",
                            lv.get("levelRequirement") if lv.get("levelRequirement") is not None else "-",
                            cost_s,
                            lv.get("critChance") if lv.get("critChance") is not None else "-",
                            lv.get("damageEffectiveness") if lv.get("damageEffectiveness") is not None else "-",
                            stats_s[:100],
                        ]
                    )
                lines.append(render_table(["Lv", "ReqLv", "Cost", "Crit", "DmgEff", "Key Stats"], lvl_rows))

            q_stats = r.get("quality_stats") or []
            if q_stats:
                q_parts = []
                for q in q_stats[:4]:
                    pq = q.get("per_quality")
                    at20 = q.get("at_20_quality")
                    at40 = q.get("at_40_quality")
                    lbl = q.get("label")
                    if lbl:
                        q_parts.append(f"{lbl} [`{q['stat']}`, {pq}/1%, @20%={at20}, @40%={at40}]")
                    else:
                        q_parts.append(f"{q['stat']}={at20} (@20%, {pq}/1%, @40%={at40})")
                lines.append(f"- **Quality**: {'; '.join(q_parts)}")
            variants = r.get("variants") or []
            if variants:
                v_parts = []
                for v in variants[:8]:
                    v_zh = f" ({v['zh_name']})" if v.get("zh_name") else ""
                    v_parts.append(f"{v.get('name')}{v_zh} [{v.get('relation')}]")
                lines.append(f"- **Variants**: {', '.join(v_parts)}")
    elif kind_norm == "unique":
        for r in results:
            zh_part = f" ({r['zh_name']})" if r.get("zh_name") else ""
            base_zh = f" ({r['base_type_zh']})" if r.get("base_type_zh") else ""
            lines.append(f"\n#### {r.get('name')}{zh_part} — {r.get('base_type')}{base_zh} (`{r.get('item_class')}`)")
            meta = [x for x in [f"ReqLv {r['req_level']}" if r.get("req_level") else "", r.get("league") or "", r.get("source") or ""] if x]
            if meta:
                lines.append("- " + " | ".join(meta))
            matched_mods = r.get("matched_mods") or []
            if matched_mods:
                for mm in matched_mods[:4]:
                    lines.append(f"- **[Match]** {mm}")
            for imp, imp_zh in zip(r.get("implicits") or [], (r.get("implicits_zh") or []) + [None] * 10):
                lines.append(f"- [Implicit] {imp}" + (f" / {imp_zh}" if imp_zh else ""))
            for exp, exp_zh in zip(r.get("explicits") or [], (r.get("explicits_zh") or []) + [None] * 30):
                lines.append(f"- {exp}" + (f" / {exp_zh}" if exp_zh else ""))
            fb_mods = r.get("foulborn_mods") or []
            if fb_mods:
                lines.append("- **Foulborn Mutations**:")
                for fb in fb_mods[:8]:
                    st_en = "; ".join(fb.get("stats") or [])
                    st_zh = "; ".join(x for x in (fb.get("stats_zh") or []) if x)
                    lines.append(f"  - {st_en}" + (f" / {st_zh}" if st_zh else ""))
    elif kind_norm == "mod":
        rows = []
        for r in results:
            en_s = "; ".join(r.get("stats") or [])
            zh_s = "; ".join(x for x in (r.get("stats_zh") or []) if x)
            stat_disp = f"{en_s} / {zh_s}" if zh_s else en_s
            weights = r.get("weights") or {}
            bench = r.get("bench_info")
            if bench:
                cost = bench.get("cost")
                cost_s = ", ".join(f"{k}:{v}" for k, v in cost.items()) if isinstance(cost, dict) else str(cost or "")
                types_s = ",".join((bench.get("types") or [])[:5])
                w_disp = f"Bench[{types_s}]" + (f" ({cost_s})" if cost_s else "")
            else:
                w_disp = ", ".join(f"{k}:{v}" for k, v in list(weights.items())[:5]) or "-"
            affix_grp = f"{r.get('affix') or '-'} ({r.get('group') or '-'})"
            rows.append(
                [
                    r.get("source") or "-",
                    r.get("generation_type") or "-",
                    affix_grp[:40],
                    r.get("level") if r.get("level") is not None else "-",
                    stat_disp[:120],
                    w_disp[:60],
                ]
            )
        lines.append(render_table(["Source", "Type", "Affix (Group)", "iLvl", "Stats", "Weights / Bench"], rows))
    elif kind_norm == "base":
        rows = []
        for r in results:
            name_disp = f"{r.get('name')} ({r['zh_name']})" if r.get("zh_name") else r.get("name")
            req = r.get("req") or {}
            req_s = f"Lv{req.get('level', 0)}/{req.get('str', 0)}/{req.get('dex', 0)}/{req.get('int', 0)}"
            props = r.get("properties") or {}
            prop_bits = []
            for pk, pv in props.items():
                if isinstance(pv, dict):
                    prop_bits.append(f"{pk}: " + ",".join(f"{k}={v}" for k, v in list(pv.items())[:4]))
            imp_s = "; ".join(r.get("implicits") or []) or "-"
            sub_part = f" ({r['sub_type']})" if r.get("sub_type") else ""
            rows.append(
                [
                    name_disp,
                    f"{r.get('type') or '-'}{sub_part}",
                    req_s,
                    "; ".join(prop_bits)[:60] or "-",
                    imp_s[:80],
                ]
            )
        lines.append(render_table(["Base", "Class", "Req (Lv/S/D/I)", "Properties", "Implicits"], rows))

    return "\n".join(lines)


def _render_gem_quality_breakpoints_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            lines.append(f"> {tag} `{w.get('trigger', '')}`: {w.get('warning', '')}")
    if res.get("qualityRule"):
        lines.append(f"> **[PoE1 3.30 Gem Quality Rule]** {res['qualityRule']}")

    scope_lbl = "Active Skill Gems Only" if res.get("activeOnly") else "All Gems (Active + Support)"
    q_lbl = f" | Search: `{res['query']}`" if res.get("query") else ""
    lines.append(
        f"\n### PoE1 3.30 Gem Quality Breakpoints ({scope_lbl}{q_lbl}) — "
        f"showing `{res.get('count', 0)}` of `{res.get('totalMatched', 0)}` "
        f"(PoB `{res.get('pobVersion')}` / `{res.get('elapsedMs')}ms`)"
    )
    results = res.get("results") or []
    if not results:
        lines.append("_(no matching gem quality stats)_")
        return "\n".join(lines)

    rows: list[list[Any]] = []
    for idx, g in enumerate(results, start=1):
        name_en = str(g.get("name") or "-")
        name_zh = str(g.get("zhName") or "-")
        kind_s = "Support" if g.get("isSupport") else "Active"
        q_list = g.get("qualityStats") or []
        q0 = q_list[0] if q_list else {}
        stat_desc = "; ".join(str(q.get("label") or q.get("stat") or "") for q in q_list[:2])
        bp_note = "; ".join(str(q.get("breakpointNote") or "") for q in q_list[:2] if q.get("breakpointNote"))
        rows.append(
            [
                idx,
                name_zh,
                name_en,
                kind_s,
                q0.get("category") or "-",
                stat_desc[:90],
                bp_note[:70] or "-",
            ]
        )
    lines.append(
        render_table(
            ["#", "Gem (ZH)", "Gem (EN)", "Type", "Tier", "Quality Effect (@20% -> @40%)", "Breakpoint / Step"],
            rows,
        )
    )
    return "\n".join(lines)


def _render_tree_data_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            canon = f" (canonical: {w['canonicalTerm']})" if w.get("canonicalTerm") else ""
            lines.append(f"> {tag} `{w.get('trigger', '')}`{canon}: {w.get('warning', '')}")

    filters_obj = res.get("filters") if isinstance(res.get("filters"), dict) else {}
    filters: list[str] = []
    if res.get("queries"):
        filters.append("Q=`" + ", ".join(str(x) for x in res["queries"]) + "`")
    if filters_obj.get("type"):
        filters.append(f"Type=`{filters_obj['type']}`")
    if filters_obj.get("asc"):
        filters.append(f"Asc=`{filters_obj['asc']}`")
    filter_s = " | ".join(filters) or "all"
    lines.append(
        f"### Local Passive Tree Data (`Tree {res.get('treeVersion') or '3_30'}`): {filter_s} — "
        f"`{res.get('count', 0)}` hits (`{res.get('elapsedMs')}ms`)"
    )
    aliases = res.get("resolvedAliases") or []
    if aliases:
        lines.append("- **Resolved Slang**: " + ", ".join(f"`{a['from']}` -> `{a['to']}`" for a in aliases))
    results = res.get("results") or []
    if not results:
        lines.append("_(no matching passive tree nodes)_")
        return "\n".join(lines)

    rows: list[list[Any]] = []
    for n in results:
        name_en = str(n.get("name") or "-")
        name_zh = str(n.get("zhName") or "")
        disp = f"{name_zh} ({name_en})" if (name_zh and name_zh != name_en) else name_en
        asc_en = str(n.get("ascName") or n.get("asc") or "")
        asc_zh = str(n.get("ascNameZh") or n.get("ascZh") or "")
        if asc_zh and asc_en and asc_zh != asc_en:
            asc_disp = f"{asc_zh} ({asc_en})"
        else:
            asc_disp = asc_zh or asc_en or "-"
        if n.get("isBloodline"):
            asc_disp = f"{asc_disp} [Bloodline]"
        stats_lines = n.get("stats") or []
        if not stats_lines and n.get("masteryEffects"):
            stats_lines = ["; ".join(me.get("stats") or []) for me in (n.get("masteryEffects") or [])[:3]]
        stats_txt = "; ".join(str(s) for s in stats_lines[:6]) or "-"
        rows.append([n.get("id"), disp, n.get("type") or "-", asc_disp, stats_txt])
    lines.append(render_table(["ID", "Node", "Type", "Ascendancy", "Live 3.30 Stats"], rows))
    return "\n".join(lines)


def _render_tick_data_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    cd = res.get("current") or {}
    nb = cd.get("nextBreakpoint") or {}
    preset = res.get("preset") or res.get("presetLabel")
    label = f" (`{preset}`)" if preset else ""
    base_cd = res.get("baseCdSec")
    cdr_val = cd.get("globalCdrPct", 0)
    spells = res.get("triggeredSpells", 1)
    lines = [
        f"### PoE1 Server Tick Cooldown Calculator (`0.033s = 33ms` tick){label}",
        "- **Formula**: `ticks = ceil((base_cd / (1 + cdr/100)) / 0.033)`, `effective_cd = ticks * 0.033s` (NOT `1/30 = 33.33ms`)",
    ]
    if cd:
        lines.append(
            f"- **Current (`base={base_cd}s`, `CDR={cdr_val:g}%`, `spells={spells}`)**: "
            f"raw `{cd.get('rawCdSec')}s` -> **`{cd.get('ticks')}` ticks = `{cd.get('effectiveCdSec')}s` (`{cd.get('effectiveCdMs')}ms`)**, "
            f"cap **`{cd.get('effectiveCapPerSec')}/s`**"
            + (
                f" | **Next breakpoint**: `{nb.get('requiredCdrPct')}%` CDR (`+{nb.get('cdrNeededPct')}%` more) -> "
                f"`{nb.get('targetTicks')}` ticks = `{nb.get('effectiveCdMs')}ms` (`{nb.get('effectiveCapPerSec')}/s`)"
                if nb
                else " | at minimum 1-tick cap"
            )
        )
    if cd.get("aps") is not None:
        if cd.get("overspeed"):
            lines.append(f"> **[OVERSPEED WARNING]** {cd.get('overspeedNote')}")
        else:
            lines.append(
                f"- **APS Check**: OK — `{cd['aps']:.2f}/s` <= cap `{cd.get('effectiveCapPerSec')}/s` "
                f"(ratio `{cd.get('apsRatioToCap')}`)"
            )
    if res.get("cwcNote"):
        lines.append(f"- **CwC Reminder**: {res['cwcNote']}")

    bp_rows = [
        [
            f"{bp.get('ticks')}t" + (" *" if cd and bp.get("ticks") == cd.get("ticks") else ""),
            f"{bp.get('effectiveCdSec')}s ({bp.get('effectiveCdMs')}ms)",
            f"{bp.get('minCdrPct')}%",
            f"{bp.get('maxApsCap') or bp.get('maxTriggersPerSec')}/s",
        ]
        for bp in (res.get("breakpoints") or [])
    ]
    if bp_rows:
        lines.append("\n#### Exact `0.033s` Tick Breakpoint Table")
        lines.append(render_table(["Ticks", "Effective CD", "Min CDR %", "Max Triggers/s"], bp_rows))
    return "\n".join(lines)


@main.command("data")
@click.argument("kind", type=click.Choice(["gem", "unique", "mod", "base", "translate", "tree", "tick"], case_sensitive=False))
@click.argument("queries", nargs=-1, required=False)
@click.option("--slot", help="Filter mods/uniques/bases by slot (e.g. 'body_armour', 'helmet', 'ring', 'wand').")
@click.option("--ilvl", type=int, help="Maximum item level / required level filter.")
@click.option("--mod", "mod_filter", type=str, default=None, help="[unique only] Filter uniques by explicit/implicit mod substring (EN or ZH).")
@click.option("--type", "node_type", type=str, default=None, help="[tree only] Filter passive tree nodes by type (Keystone, Notable, Mastery, Ascendancy, Bloodline, Socket).")
@click.option("--asc", "asc_filter", type=str, default=None, help="[tree only] Filter passive tree nodes by Ascendancy or Bloodline name (EN or ZH, e.g. Warden, Trickster, 守望者).")
@click.option("--cdr", "cdr_pct", type=float, default=0.0, show_default=True, help="[tick only] Total Cooldown Recovery Rate % (e.g. 14, 52).")
@click.option("--aps", "aps_rate", type=float, default=None, help="[tick only] Trigger attack/action rate per second for CoC overspeed check.")
@click.option("--spells", "triggered_spells", type=int, default=1, show_default=True, help="[tick only] Number of distinct triggered skills sharing the trigger.")
@click.option("--limit", type=int, default=25, show_default=True, help="Maximum number of results per query.")
@click.option("--quality-breakpoints", "quality_breakpoints", is_flag=True, help="[gem only] Scan 3.30 gems for 20%->40%+ quality breakpoints (integer steps & more multipliers).")
@click.option("--search-quality", "search_quality", type=str, default=None, help="[gem only] Search gems by quality stat keyword (EN or ZH, e.g. 'projectile', '连锁', 'more').")
@click.option("--active-only", "active_only", is_flag=True, help="[gem quality scan] Restrict to Active Skill Gems only (matching '+% to Quality of all Skill Gems').")
@click.option("--rebuild", is_flag=True, help="Force rebuild the local SQLite game/tree data index.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_data(
    kind: str,
    queries: tuple[str, ...],
    slot: str | None,
    ilvl: int | None,
    mod_filter: str | None,
    node_type: str | None,
    asc_filter: str | None,
    cdr_pct: float,
    aps_rate: float | None,
    triggered_spells: int,
    limit: int,
    quality_breakpoints: bool,
    search_quality: str | None,
    active_only: bool,
    rebuild: bool,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query local 0ms PoB Lua game/tree data & 0.033s tick calculator (`gem|unique|mod|base|translate|tree|tick`, daemon-free)."""
    from . import game_data as gd

    kind_low = kind.lower()
    if rebuild:
        if kind_low == "tree":
            gd.ensure_treedata_index(force_rebuild=True)
        elif kind_low != "tick":
            gd.ensure_gamedata_index(force_rebuild=True)

    q_list = [q.strip() for q in queries if q and q.strip()]

    if kind_low == "tick":
        if not q_list:
            raise click.UsageError(
                "Provide base cooldown seconds or trigger preset (e.g. `pob data tick 0.15 --cdr 14 --aps 8.5` or `pob data tick coc --cdr 52`)."
            )
        try:
            res = gd.query_tick_breakpoints(
                q_list[0],
                cdr_pct=cdr_pct,
                aps=aps_rate,
                triggered_spells=triggered_spells,
            )
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_tick_data_markdown(res), out_path=out_path)
        return

    if kind_low == "tree":
        if not q_list and not node_type and not asc_filter:
            raise click.UsageError(
                "Provide at least one node query or `--type` / `--asc` filter (e.g. `pob data tree --asc Warden` or `pob data tree Polymath`)."
            )
        res = gd.query_tree_data(
            q_list or None,
            node_type=node_type,
            asc=asc_filter,
            limit=limit,
        )
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_tree_data_markdown(res), out_path=out_path)
        return

    if mod_filter is not None and kind_low != "unique":
        raise click.UsageError("--mod is only supported for `pob data unique`.")

    if quality_breakpoints or search_quality is not None:
        if kind_low != "gem":
            raise click.UsageError("--quality-breakpoints and --search-quality are only supported for `pob data gem`.")
        eff_q = search_quality if search_quality is not None else (" ".join(q_list) or None)
        res = gd.query_gem_quality_breakpoints(
            eff_q,
            active_only=active_only,
            limit=limit,
        )
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_gem_quality_breakpoints_markdown(res), out_path=out_path)
        return

    if not q_list:
        if kind_low == "unique" and mod_filter and mod_filter.strip():
            res = gd.query_local_data(kind, "", slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit)
            if as_json:
                _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
                return
            _emit_text(_render_single_data_markdown(res, kind, f"--mod {mod_filter}"), out_path=out_path)
            return
        raise click.UsageError("Provide at least one non-empty query.")
    if len(q_list) == 1:
        res = gd.query_local_data(kind, q_list[0], slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit)
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        q_disp = f"{q_list[0]} (--mod {mod_filter})" if mod_filter else q_list[0]
        _emit_text(_render_single_data_markdown(res, kind, q_disp), out_path=out_path)
        return

    # Multi-query path: use game_data.query_local_data_multi / query_multi if available, else loop
    multi_res: dict[str, Any]
    if hasattr(gd, "query_local_data_multi"):
        multi_res = _call_with_supported_kwargs(
            gd.query_local_data_multi, kind, q_list, slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit
        )
    elif hasattr(gd, "query_multi"):
        import inspect

        sig = inspect.signature(gd.query_multi)
        first_param = next(iter(sig.parameters.keys()), "")
        if first_param in ("db", "conn", "db_path"):
            db_path = gd.ensure_gamedata_index()
            multi_res = _call_with_supported_kwargs(
                gd.query_multi, db_path, kind, q_list, slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit
            )
        else:
            multi_res = _call_with_supported_kwargs(
                gd.query_multi, kind, q_list, slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit
            )
    else:
        grouped = {q: gd.query_local_data(kind, q, slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit) for q in q_list}
        multi_res = {"kind": kind.lower(), "queries": q_list, "results": grouped}

    if as_json:
        _emit_json(multi_res, compact=compact, out_path=out_path, sanitize=False)
        return

    sections: list[str] = []
    res_map = multi_res.get("results")
    resp_queries = (
        [str(x) for x in multi_res["queries"]]
        if isinstance(multi_res, dict) and isinstance(multi_res.get("queries"), list)
        else q_list
    )
    if isinstance(res_map, dict):
        for q in resp_queries:
            q_key = q.strip()
            sub = res_map[q] if q in res_map else (res_map[q_key] if q_key in res_map else {})
            sections.append(_render_single_data_markdown(sub or {}, kind, q_key))
    elif isinstance(res_map, list):
        for idx, sub in enumerate(res_map):
            sub_dict = sub if isinstance(sub, dict) else {}
            lbl = str(sub_dict.get("query") or (resp_queries[idx] if idx < len(resp_queries) else "-"))
            sections.append(_render_single_data_markdown(sub_dict, kind, lbl))
    _emit_text("\n\n".join(sections), out_path=out_path)


def _render_single_poedb_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    if res.get("error"):
        return f"### PoEDB: `{res.get('query', '-')}` — Error: {res['error']}"

    lines = [
        f"### PoEDB: {res.get('title')} (`{res.get('slug')}`) — {res.get('url')}",
    ]
    if res.get("stale"):
        lines.append("> [WARN] Serving stale cached PoEDB response due to network error.")
    if res.get("category"):
        lines.append(f"- **Category**: {res['category']}")
    for prop in res.get("properties") or []:
        lines.append(f"- {prop}")
    if res.get("implicits"):
        lines.append("\n#### Implicits")
        for ln in res["implicits"]:
            lines.append(f"- {ln}")
    if res.get("explicits"):
        lines.append("\n#### Explicits")
        for ln in res["explicits"]:
            lines.append(f"- {ln}")

    vh = res.get("versionHistory") or []
    if vh:
        lines.append("\n#### Version History")
        v_rows = [[v.get("version"), "; ".join(v.get("changes") or [])[:140]] for v in vh[:10]]
        lines.append(render_table(["Version", "Changes"], v_rows))

    mods = res.get("mods")
    if mods is not None:
        pool_slug = res.get("modPoolSlug") or res.get("slug")
        total_m = res.get("modCountTotal", len(mods))
        lines.append(f"\n#### Mod Pool (`{pool_slug}` — showing {len(mods)} of {total_m} mods)")
        m_rows = []
        for m in mods:
            fam = ", ".join(m.get("family") or [])[:35] or "-"
            tags = ", ".join(m.get("fossilNo") or m.get("tags") or [])[:40] or "-"
            m_rows.append(
                [
                    m.get("side") or "-",
                    fam,
                    m.get("level") if m.get("level") is not None else "-",
                    (m.get("text") or "-")[:90],
                    m.get("dropChance") if m.get("dropChance") is not None else "-",
                    tags,
                ]
            )
        lines.append(render_table(["Side", "Family", "iLvl", "Mod Text", "Weight", "Tags (fossil_no)"], m_rows))

    return "\n".join(lines)


@main.command("poedb")
@click.argument("queries", nargs=-1, required=True)
@click.option("--mods", "include_mods", is_flag=True, help="Fetch item/base mod pool with DropChance weights & fossil tags.")
@click.option("--lang", type=click.Choice(["cn", "us", "en"], case_sensitive=False), default="cn", show_default=True, help="PoEDB language.")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--limit", type=int, default=60, show_default=True, help="Maximum mod pool rows to return.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_poedb(
    queries: tuple[str, ...],
    include_mods: bool,
    lang: str,
    proxy: str | None,
    limit: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query poedb.tw CN/US autocomplete index, properties, version history, and mod pool weights (cached)."""
    from . import poedb_client as pc

    q_list = [q.strip() for q in queries if q and q.strip()]
    if not q_list:
        raise click.UsageError("Provide at least one non-empty query.")
    if len(q_list) == 1:
        res = pc.query_poedb(q_list[0], include_mods=include_mods, lang=lang, proxy=proxy, limit_mods=limit)
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_single_poedb_markdown(res), out_path=out_path)
        return

    if hasattr(pc, "query_poedb_multi"):
        multi_res = _call_with_supported_kwargs(
            pc.query_poedb_multi, q_list, include_mods=include_mods, lang=lang, proxy=proxy, limit_mods=limit
        )
    else:
        grouped = {
            q: pc.query_poedb(q, include_mods=include_mods, lang=lang, proxy=proxy, limit_mods=limit)
            for q in q_list
        }
        multi_res = {"queries": q_list, "results": grouped}

    if as_json:
        _emit_json(multi_res, compact=compact, out_path=out_path, sanitize=False)
        return

    sections = []
    res_map = multi_res.get("results") if isinstance(multi_res, dict) else multi_res
    resp_queries = (
        [str(x) for x in multi_res["queries"]]
        if isinstance(multi_res, dict) and isinstance(multi_res.get("queries"), list)
        else q_list
    )
    if isinstance(res_map, dict):
        for q in resp_queries:
            q_key = q.strip()
            sub = res_map[q] if q in res_map else (res_map[q_key] if q_key in res_map else {"query": q_key, "error": "missing"})
            sections.append(_render_single_poedb_markdown(sub or {"query": q_key, "error": "missing"}))
    elif isinstance(res_map, list):
        for idx, item in enumerate(res_map):
            sub_dict = dict(item) if isinstance(item, dict) else {"error": "missing"}
            if not sub_dict.get("query") and idx < len(resp_queries):
                sub_dict["query"] = resp_queries[idx]
            sections.append(_render_single_poedb_markdown(sub_dict))
    _emit_text("\n\n".join(sections), out_path=out_path)


def _render_single_price_markdown(res: dict[str, Any], query: str) -> str:
    from .formatters import fmt_num, render_table

    if res.get("error"):
        return f"### poe.ninja Price Search: `{query}` — Error: {res['error']}"

    matches = res.get("matches", [])
    is_det = bool(res.get("detailed")) or any(
        m.get("gemLevel") is not None or m.get("corrupted") is not None for m in matches
    )
    div_rate = res.get("divineChaosRate")
    rate_str = f" | 1 Divine = `{fmt_num(div_rate, 1)}` Chaos" if div_rate else ""
    det_tag = " [Detailed]" if is_det else ""
    lines = [
        f"### poe.ninja Price Search{det_tag}: `{query}` (League: `{res.get('league')}`{rate_str}, {res.get('totalMatches', len(matches))} matches)"
    ]
    if res.get("stale"):
        lines.append("> [WARN] Serving stale cached poe.ninja price data due to network error.")

    if is_det:
        rows = []
        for m in matches:
            lv = m.get("gemLevel")
            ql = m.get("gemQuality")
            lv_q = f"{lv}/{ql or 0}" if lv is not None else "-"
            corr = "Yes" if m.get("corrupted") is True else ("No" if m.get("corrupted") is False else "-")
            links = f"{m['links']}L" if m.get("links") else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or "-",
                    lv_q,
                    corr,
                    links,
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        lines.append(
            render_table(
                ["Item / Currency", "Category", "Variant", "Lvl/Qual", "Corrupted", "Links", "Chaos", "Divine", "Listings"],
                rows,
            )
        )
    else:
        rows = []
        for m in matches:
            trend = m.get("trend24hPct")
            trend_str = f"{float(trend):+.1f}%" if trend is not None else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or (f"{m['links']}L" if m.get("links") else "-"),
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    trend_str,
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        lines.append(
            render_table(["Item / Currency", "Category", "Variant / Links", "Chaos", "Divine", "24h Trend", "Listings"], rows)
        )
    return "\n".join(lines)


@main.command("price")
@click.argument("queries", nargs=-1, required=True)
@click.option("--league", help="League name (defaults to current primary softcore trade league).")
@click.option("--category", default="all", show_default=True, help="Category (all, unique, gem, imbuedgem, currency, card, cluster, base, flask, scarab, runegraft, wombgift, etc.).")
@click.option("--variant", help="Filter matches by variant substring (e.g. 'Necromancer', '8 passives', '21/20').")
@click.option("--limit", type=int, default=20, show_default=True, help="Maximum number of matches to return.")
@click.option("--detailed", is_flag=True, help="Use detailed stash overview API (gem level/quality, corrupted, links, cluster passives).")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_price(
    queries: tuple[str, ...],
    league: str | None,
    category: str,
    variant: str | None,
    limit: int,
    detailed: bool,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query poe.ninja economy prices (15m local cache, 52 overview categories, optional --detailed/--variant)."""
    from . import external_data as ext

    q_list = [q.strip() for q in queries if q and q.strip()]
    if not q_list:
        raise click.UsageError("Provide at least one non-empty query.")
    if len(q_list) == 1:
        res = _call_with_supported_kwargs(
            ext.query_price,
            q_list[0],
            league=league,
            category=category,
            limit=limit,
            variant=variant,
            detailed=detailed,
            proxy=proxy,
        )
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_single_price_markdown(res, q_list[0]), out_path=out_path)
        return

    if hasattr(ext, "query_price_multi"):
        multi_res = _call_with_supported_kwargs(
            ext.query_price_multi,
            q_list,
            league=league,
            category=category,
            limit=limit,
            variant=variant,
            detailed=detailed,
            proxy=proxy,
        )
    else:
        grouped = {
            q: _call_with_supported_kwargs(
                ext.query_price,
                q,
                league=league,
                category=category,
                limit=limit,
                variant=variant,
                detailed=detailed,
                proxy=proxy,
            )
            for q in q_list
        }
        multi_res = {"queries": q_list, "results": grouped}

    if as_json:
        _emit_json(multi_res, compact=compact, out_path=out_path, sanitize=False)
        return

    sections = []
    res_map = multi_res.get("results") if isinstance(multi_res, dict) else multi_res
    resp_queries = (
        [str(x) for x in multi_res["queries"]]
        if isinstance(multi_res, dict) and isinstance(multi_res.get("queries"), list)
        else q_list
    )
    if isinstance(res_map, dict):
        for q in resp_queries:
            q_key = q.strip()
            sub = res_map[q] if q in res_map else (res_map[q_key] if q_key in res_map else {"error": "missing"})
            sections.append(_render_single_price_markdown(sub or {"error": "missing"}, q_key))
    elif isinstance(res_map, list):
        for idx, sub in enumerate(res_map):
            sub_dict = sub if isinstance(sub, dict) else {"error": "missing"}
            lbl = str(sub_dict.get("query") or (resp_queries[idx] if idx < len(resp_queries) else "-"))
            sections.append(_render_single_price_markdown(sub_dict, lbl))
    _emit_text("\n\n".join(sections), out_path=out_path)


@main.command("meta")
@click.option("--league", help="League name or slug (defaults to current primary challenge league).")
@click.option("--skill", help="Filter by main skill substring.")
@click.option("--class", "class_name", help="Filter by ascendancy/class substring.")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top entries to show.")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_meta(
    league: str | None,
    skill: str | None,
    class_name: str | None,
    top: int,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query poe.ninja league meta snapshot (total builds, ascendancy & main skill share)."""
    from . import external_data as ext
    from .formatters import render_table

    res = _call_with_supported_kwargs(
        ext.query_meta,
        league=league,
        skill=skill,
        class_name=class_name,
        top=top,
        proxy=proxy,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
        return

    lines = [
        f"### poe.ninja Meta Snapshot: `{res.get('league')}` — `{res.get('totalBuilds', 0):,}` total builds",
    ]
    asc_rows = [
        [
            a.get("class"),
            f"{a.get('percentage', 0):.2f}%",
            f"{a.get('estimatedBuilds', 0):,}",
            ", ".join((a.get("skills") or [])[:4]),
        ]
        for a in res.get("ascendancies", [])
    ]
    lines.append("\n#### Top Ascendancies")
    lines.append(render_table(["Ascendancy", "Share", "Est. Builds", "Top Skills"], asc_rows))

    sk_rows = [
        [
            s.get("skill"),
            f"{s.get('percentage', 0):.2f}%",
            f"{s.get('estimatedBuilds', 0):,}",
            ", ".join((s.get("classes") or [])[:4]),
        ]
        for s in res.get("topSkills", [])
    ]
    lines.append("\n#### Top Main Skills")
    lines.append(render_table(["Skill", "Share", "Est. Builds", "Top Classes"], sk_rows))

    _emit_text("\n".join(lines), out_path=out_path)


def _render_single_wiki_markdown(res: dict[str, Any], topic: str) -> str:
    if res.get("error"):
        return f"### PoE Wiki: `{topic}` — Error: {res['error']}"
    parts = [f"### PoE Wiki: {res.get('title') or topic} ({res.get('url') or '-'})"]
    if res.get("stale"):
        parts.append("> [WARN] Serving stale cached PoE Wiki response due to network error.")
    if res.get("summary"):
        parts.append(str(res["summary"]))
    return "\n\n".join(parts)


@main.command("wiki")
@click.argument("queries", nargs=-1, required=True)
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_wiki(
    queries: tuple[str, ...],
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query poewiki.net MediaWiki API for mechanics, uniques, and keystones (24h local cache)."""
    from . import external_data as ext

    q_list = [q.strip() for q in queries if q and q.strip()]
    if not q_list:
        raise click.UsageError("Provide at least one non-empty query.")
    if len(q_list) == 1:
        res = _call_with_supported_kwargs(ext.query_wiki, q_list[0], proxy=proxy)
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_single_wiki_markdown(res, q_list[0]), out_path=out_path)
        return

    if hasattr(ext, "query_wiki_multi"):
        multi_res = _call_with_supported_kwargs(ext.query_wiki_multi, q_list, proxy=proxy)
    else:
        grouped = {q: _call_with_supported_kwargs(ext.query_wiki, q, proxy=proxy) for q in q_list}
        multi_res = {"queries": q_list, "results": grouped}

    if as_json:
        _emit_json(multi_res, compact=compact, out_path=out_path, sanitize=False)
        return

    sections = []
    res_map = multi_res.get("results") if isinstance(multi_res, dict) else multi_res
    resp_queries = (
        [str(x) for x in multi_res["queries"]]
        if isinstance(multi_res, dict) and isinstance(multi_res.get("queries"), list)
        else q_list
    )
    if isinstance(res_map, dict):
        for q in resp_queries:
            q_key = q.strip()
            sub = res_map[q] if q in res_map else (res_map[q_key] if q_key in res_map else {"error": "missing"})
            sections.append(_render_single_wiki_markdown(sub or {"error": "missing"}, q_key))
    elif isinstance(res_map, list):
        for idx, sub in enumerate(res_map):
            sub_dict = sub if isinstance(sub, dict) else {"error": "missing"}
            lbl = str(sub_dict.get("query") or (resp_queries[idx] if idx < len(resp_queries) else "-"))
            sections.append(_render_single_wiki_markdown(sub_dict, lbl))
    _emit_text("\n\n".join(sections), out_path=out_path)


if __name__ == "__main__":
    main()
