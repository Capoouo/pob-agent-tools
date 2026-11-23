from __future__ import annotations

import pytest

from pob_cli import cli_trade


@pytest.fixture(autouse=True)
def _no_live_currency_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """`trade search` looks up currency rates online; tests must stay offline."""
    monkeypatch.setattr(cli_trade, "_chaos_rates", lambda *_a, **_kw: {"chaos": 1.0})
