from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from agent_observability_tui.app import ObservabilityApp
from agent_observability_tui.observatory import Observatory

DEMO_TRACE = Path(__file__).parents[1] / "examples" / "demo-trace.jsonl"


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_dashboard_renders_sessions_timeline_and_metrics(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    observatory = Observatory.open(tmp_path / f"sessions-{size[0]}.sqlite3")
    session_id = observatory.import_path(DEMO_TRACE, adapter="native", session_id="demo")
    app = ObservabilityApp(observatory, session_id=session_id)

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.query_one("#sessions", DataTable).row_count == 1
        assert app.query_one("#events", DataTable).row_count == 7
        assert "1240 in / 188 out" in str(app.query_one("#metrics", Static).render())

        app.query_one("#events", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "agenttrace.event/1" in str(app.query_one("#detail", Static).render())


async def test_pause_and_filter_only_change_ui_projection(tmp_path: Path) -> None:
    observatory = Observatory.open(tmp_path / "sessions.sqlite3")
    session_id = observatory.import_path(DEMO_TRACE, adapter="native", session_id="demo")
    app = ObservabilityApp(observatory, session_id=session_id)

    async with app.run_test() as pilot:
        await pilot.press("p")
        assert app.paused is True
        await pilot.press("/")
        await pilot.press(*"evidence")
        await pilot.pause()
        assert app.query_one("#events", DataTable).row_count == 1
        assert len(tuple(observatory.replay(session_id))) == 7
