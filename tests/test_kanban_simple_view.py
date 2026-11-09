"""Simple five-column board view: mapping, full-view parity, read-only guarantees.

The simple view is a read-only projection of the same card list the full board
renders. ``group_simple`` must be total (every card lands in exactly one
column) and must never touch the database or mutate the caller's cards.
"""

from __future__ import annotations

import copy
import importlib
import re
import sys
import types
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


# Every status the board can carry, plus one unknown value and the empty
# string, so the mapping is proven total rather than merely correct.
ALL_STATUSES = [
    "triage", "todo", "scheduled", "ready", "running", "review", "blocked",
    "done", "archived", "mystery-status", "",
]
# Every block_kind the dispatcher can write, plus NULL and the empty string.
ALL_BLOCK_KINDS = [None, "", "needs_input", "transient", "dependency", "capability", "unknown"]


def _expected_column(status, block_kind):
    """The decision table, written out independently of the implementation."""
    if status == "blocked":
        return "needs_you" if block_kind == "needs_input" else "queued"
    if status in ("running", "review"):
        return "working"
    if status in ("todo", "ready", "scheduled"):
        return "queued"
    if status == "done":
        return "done"
    if status == "triage":
        return "inbox"
    return "queued"


def _card(task_id, status, block_kind=None, **extra):
    card = {"id": task_id, "title": task_id, "status": status, "block_kind": block_kind}
    card.update(extra)
    return card


def _bridge():
    import api.kanban_bridge as bridge

    return bridge


@pytest.mark.parametrize("block_kind", ALL_BLOCK_KINDS)
@pytest.mark.parametrize("status", ALL_STATUSES)
def test_simple_view_mapping(status, block_kind):
    """Every (status, block_kind) pair lands in exactly one simple column."""
    bridge = _bridge()
    card = _card("t_x", status, block_kind)

    groups = bridge.group_simple([card])

    placements = [name for name, cards in groups.items() if cards]
    assert placements == [_expected_column(status, block_kind)]
    # Exactly one column owns the card, and it is one of the five.
    assert len(placements) == 1
    assert placements[0] in bridge.SIMPLE_VIEW_COLUMNS
    # The full column list is untouched by the simple view.
    assert list(groups) == bridge.SIMPLE_VIEW_COLUMNS


@pytest.mark.parametrize(
    "status,block_kind,column,badge",
    [
        ("triage", None, "inbox", None),
        ("todo", None, "queued", None),
        ("scheduled", None, "queued", None),
        ("ready", None, "queued", None),
        ("running", None, "working", None),
        ("review", None, "working", None),
        ("done", None, "done", None),
        ("blocked", "needs_input", "needs_you", None),
        ("blocked", "transient", "queued", "transient"),
        ("blocked", "dependency", "queued", "dependency"),
        ("blocked", "capability", "queued", "capability"),
        ("blocked", None, "queued", "unknown"),
        ("blocked", "", "queued", "unknown"),
        ("archived", None, "queued", "unknown"),
        ("mystery-status", None, "queued", "unknown"),
        ("", None, "queued", "unknown"),
    ],
)
def test_simple_view_mapping_matches_decision_table(status, block_kind, column, badge):
    bridge = _bridge()

    groups = bridge.group_simple([_card("t_y", status, block_kind)])

    assert [name for name, cards in groups.items() if cards] == [column]
    card = groups[column][0]
    assert card["id"] == "t_y"
    assert card.get("simple_badge") == badge


def test_simple_view_grouping_never_mutates_cards():
    bridge = _bridge()
    cards = [
        _card("t_1", "blocked", "transient"),
        _card("t_2", "blocked", None),
        _card("t_3", "ready"),
        _card("t_4", "done", completed_at=10),
    ]
    snapshot = copy.deepcopy(cards)

    bridge.group_simple(cards)

    assert cards == snapshot, "group_simple must not mutate the caller's cards"


def test_simple_view_grouping_never_touches_the_database(monkeypatch):
    bridge = _bridge()

    def _boom():  # pragma: no cover - only reached on a regression
        raise AssertionError("group_simple must not open the kanban database")

    monkeypatch.setattr(bridge, "_kb", _boom)
    monkeypatch.setattr(bridge, "_conn", _boom)

    groups = bridge.group_simple([_card("t_1", "ready"), _card("t_2", "blocked", "needs_input")])

    assert len(groups["queued"]) == 1
    assert len(groups["needs_you"]) == 1


def test_simple_view_done_is_newest_first_and_capped():
    bridge = _bridge()
    done = [
        _card(f"t_{n}", "done", completed_at=n * 100)
        for n in range(bridge.SIMPLE_VIEW_DONE_LIMIT + 5)
    ]

    groups = bridge.group_simple(done)

    assert len(groups["done"]) == bridge.SIMPLE_VIEW_DONE_LIMIT
    stamps = [int(card["completed_at"]) for card in groups["done"]]
    assert stamps == sorted(stamps, reverse=True)


def test_simple_view_payload_columns_have_counts():
    bridge = _bridge()
    cards = [
        _card("t_1", "blocked", "needs_input"),
        _card("t_2", "running"),
        _card("t_3", "ready"),
        _card("t_4", "done"),
        _card("t_5", "triage"),
    ]

    payload = bridge.simple_view_payload(cards)

    assert payload["view"] == "simple"
    assert [column["name"] for column in payload["columns"]] == bridge.SIMPLE_VIEW_COLUMNS
    assert payload["counts"] == {name: 1 for name in bridge.SIMPLE_VIEW_COLUMNS}
    for column in payload["columns"]:
        assert column["count"] == len(column["tasks"])


# ── Bridge-level: the payload contract and the full view's parity ────────────


@dataclass
class FakeTask:
    id: str
    title: str
    status: str = "ready"
    block_kind: str | None = None
    assignee: str | None = None
    tenant: str | None = None
    priority: int = 0
    body: str | None = None
    created_at: int = 0
    completed_at: int | None = None


class FakeConn:
    def __init__(self, tasks):
        self.tasks = tasks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        if "MAX(id)" in sql:
            return SimpleNamespace(fetchone=lambda: {"latest": 0})
        if "FROM task_links" in sql:
            return SimpleNamespace(fetchall=lambda: [])
        if "FROM task_comments" in sql:
            return SimpleNamespace(fetchall=lambda: [])
        raise AssertionError(f"unexpected SQL: {sql}")


class FakeKanbanDB:
    def __init__(self, tasks=None):
        self.tasks = tasks or []

    def init_db(self, *, board=None):
        return None

    def connect(self, *, board=None):
        return FakeConn(self.tasks)

    def task_age(self, task):
        return 42

    def list_tasks(self, conn, tenant=None, assignee=None, include_archived=False, **_kwargs):
        return list(conn.tasks)


def _load_bridge(monkeypatch, tasks=None):
    fake_kanban = FakeKanbanDB(tasks)
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.kanban_db = fake_kanban
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_kanban)
    import api.kanban_bridge as bridge

    return importlib.reload(bridge)


def _parsed(query=""):
    return SimpleNamespace(path="/api/kanban/board", query=query)


def _fake_board_tasks():
    return [
        FakeTask("t_need", "Needs you", "blocked", "needs_input"),
        FakeTask("t_run", "Working", "running"),
        FakeTask("t_rev", "In review", "review"),
        FakeTask("t_ready", "Queued ready", "ready"),
        FakeTask("t_block", "Queued blocked", "blocked", "transient"),
        FakeTask("t_done", "Finished", "done", completed_at=99),
        FakeTask("t_idea", "Idea", "triage"),
    ]


def test_board_payload_simple_view_returns_five_groups_with_counts(monkeypatch):
    bridge = _load_bridge(monkeypatch, _fake_board_tasks())

    data = bridge._board_payload(_parsed(query="view=simple"))

    assert data["view"] == "simple"
    assert [column["name"] for column in data["columns"]] == bridge.SIMPLE_VIEW_COLUMNS
    assert data["counts"] == {
        column["name"]: len(column["tasks"]) for column in data["columns"]
    }
    by_name = {column["name"]: column for column in data["columns"]}
    assert [task["id"] for task in by_name["needs_you"]["tasks"]] == ["t_need"]
    assert [task["id"] for task in by_name["working"]["tasks"]] == ["t_run", "t_rev"]
    assert {task["id"] for task in by_name["queued"]["tasks"]} == {"t_ready", "t_block"}
    assert [task["id"] for task in by_name["done"]["tasks"]] == ["t_done"]
    assert [task["id"] for task in by_name["inbox"]["tasks"]] == ["t_idea"]
    blocked = next(task for task in by_name["queued"]["tasks"] if task["id"] == "t_block")
    assert blocked["simple_badge"] == "transient"


def test_board_payload_simple_view_does_not_write(monkeypatch):
    tasks = _fake_board_tasks()
    bridge = _load_bridge(monkeypatch, tasks)
    before = [asdict(task) for task in tasks]

    bridge._board_payload(_parsed(query="view=simple"))

    assert [asdict(task) for task in tasks] == before


def test_board_payload_full_view_is_unchanged(monkeypatch):
    bridge = _load_bridge(monkeypatch, _fake_board_tasks())

    data = bridge._board_payload(_parsed())

    assert [column["name"] for column in data["columns"]] == bridge.BOARD_COLUMNS
    assert "view" not in data
    assert all("count" not in column for column in data["columns"])
    assert [task["id"] for task in data["columns"][5]["tasks"]] == ["t_need", "t_block"]


# ── Frontend wiring (source assertions, matching tests/test_kanban_view_toggle.py)


def test_simple_view_toggle_persists_in_localstorage_with_url_override():
    assert "KANBAN_VIEW_STORAGE_KEY = 'hermes-webui.view'" in PANELS_JS
    assert "localStorage.setItem(KANBAN_VIEW_STORAGE_KEY" in PANELS_JS
    assert "localStorage.getItem(KANBAN_VIEW_STORAGE_KEY" in PANELS_JS
    assert "get('view')" in PANELS_JS
    assert "function _kanbanResolveView()" in PANELS_JS
    assert "'simple'" in PANELS_JS and "'full'" in PANELS_JS


def test_simple_view_toggle_button_is_wired_in_the_kanban_header():
    match = re.search(r'<button[^>]+id="btnKanbanSimpleViewToggle"[^>]*>', INDEX_HTML, re.S)
    assert match, "Kanban header must expose a simple/full view toggle"
    tag = match.group(0)
    assert 'onclick="toggleKanbanSimpleView()"' in tag
    assert 'aria-pressed=' in tag
    assert 'data-i18n-title="kanban_simple_view"' in tag
    assert 'data-i18n-aria-label="kanban_simple_view"' in tag

    kanban_start = INDEX_HTML.index('id="mainKanban"')
    simple_idx = INDEX_HTML.index('id="btnKanbanSimpleViewToggle"', kanban_start)
    preview_idx = INDEX_HTML.index('id="btnKanbanPreviewDispatcher"', kanban_start)
    assert simple_idx < preview_idx


def test_simple_view_renders_five_groups_with_counts():
    assert "function _kanbanRenderSimpleColumn" in PANELS_JS
    assert "data-simple-column=" in PANELS_JS
    assert "data-kanban-count=" in PANELS_JS
    assert "_kanbanBoard.view === 'simple'" in PANELS_JS
    assert "params.set('view', _kanbanSimpleView ? 'simple' : 'full')" in PANELS_JS


def test_simple_view_columns_are_read_only():
    start = PANELS_JS.index("function _kanbanRenderSimpleColumn")
    end = PANELS_JS.index("function _kanbanRenderBoard", start)
    body = PANELS_JS[start:end]
    assert "readonly: true" in body
    assert "ondrop" not in body
    assert "dropKanbanTask" not in body
    assert "draggable" not in body


def test_simple_view_labels_and_badge_css_exist():
    for key in (
        "kanban_simple_view",
        "kanban_full_view",
        "kanban_view_saved",
        "kanban_simple_needs_you",
        "kanban_simple_working",
        "kanban_simple_queued",
        "kanban_simple_done",
        "kanban_simple_inbox",
    ):
        assert f"{key}:" in I18N_JS
    assert ".kanban-badge.simple-kind" in STYLE_CSS
