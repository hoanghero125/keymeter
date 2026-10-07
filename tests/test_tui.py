from conftest import Scripted, ok
from rich.console import Console

from keymeter.monitor import Monitor
from keymeter.tui import render_burn

T0 = 1_800_000_000.0


def text_of(renderable):
    console = Console(width=120, record=True)
    console.print(renderable)
    return console.export_text()


def test_used_up_budget_says_so(args):
    mon = Monitor(Scripted(ok(T0, spend=9.0, max_budget=10), ok(T0 + 600, spend=10.0, max_budget=10)), args)
    mon.poll()
    mon.poll()
    out = text_of(render_burn(mon.summary(), args, T0 + 600))
    assert "used up" in out and "not at this rate" not in out
