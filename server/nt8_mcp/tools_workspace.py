# Portions of this file are derived from cli-nt-bridge
# (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
# MIT License. The full notice is in NOTICE at the repository root.
# (Derived: the `offscreen` heuristic and its minimized exclusion, nt8bridge/windows.py:39-59.)
"""Desktop tools: the loaded workspace, the Control Center strategy grid, window screenshots,
and workspace switch / open / close / save-as.

Thin wrappers over the AddOn's HTTP API on :7891 (contract: ../../docs/api/workspace.md).
The first three are reads; the only thing they write is one PNG file. The four workspace
operations call NinjaTrader's own workspace methods inside the AddOn: no menu, no dialog, no
mouse, and nothing is ever deleted. Nothing here enables, disables, starts or stops a strategy.
"""

import os
import tempfile

from nt8_mcp import app
from nt8_mcp.app import Image, _addon_get, _addon_post, mcp

# ponytail: fixed budgets rather than a caller-supplied one (nt_playback's pattern) — nothing here
# has a natural per-call knob like a coverage scan does. Raise further if a slower desktop needs it.
_WORKSPACE_BUDGET_S = 65   # one Ui() hop per window, each bounded at 5s on the AddOn side
_RUNNING_BUDGET_S = 15     # one Ui() hop bounded at Ws_GridTimeout (10s) on the AddOn side
# Opening a workspace builds every window in it on NinjaTrader's side, then the AddOn re-reads
# for up to ~5 s; a started UI call is waited for, so the budget covers a heavy workspace.
_WORKSPACE_OP_BUDGET_S = 120

# Windows parks a minimized window at (-32000, -32000) — well inside the "lost" range below — while
# it still reports as a normal window. Without the exclusion every minimized window is a false
# positive in the one list whose only job is to be short enough to act on (measured against a live
# NinjaTrader: 1 of 67 windows). The taskbar is how you reach a minimized window; it is not lost.
_LOST = 30000


def _offscreen(win: dict) -> bool:
    """Heuristic: is this window's title bar unreachable with the mouse?

    Deliberately generous — it catches the pathological case (a window parked at short.MinValue by a
    runaway move), not a window hanging slightly off an edge, which is normal. A window with no
    handle, and a minimized one, make no claim and answer False.
    """
    # ponytail: coordinate test only, no virtual-screen bounds. Add the bounds test if a multi-monitor
    # layout ever parks a window just past the last monitor instead of at short.MinValue.
    if win.get("isMinimized"):
        return False
    left, top = win.get("left"), win.get("top")
    if left is None or top is None:
        return False
    return left <= -_LOST or top <= -_LOST or left >= _LOST or top >= _LOST


@mcp.tool(name="nt_workspace")
def nt_workspace() -> dict:
    """The loaded NinjaTrader workspace and every window the AddOn can see, in one read: `name` (the
    active workspace, null if NT8 reports none) and `windows`, one row per window including owned
    dialogs and message boxes. Each row carries id (the chart id, or null), kind, type, title, owned,
    screen geometry (hwnd/left/top/width/height/isMinimized/screen, all null when the window has no
    handle) and `offscreen`, a hint that the window is parked where the mouse cannot reach it. A chart
    row also carries details = {instrument, period, barsCount, indicators, strategies}, each script
    with NinjaTrader's own State (a silently disabled strategy is what this call is for); any other
    window has details null plus a note. details null with a note means the window could not be read,
    which is not the same claim as an empty chart. Precondition: NT8 open with the AddOn running. This
    is a read and touches no window state — do not poll it, every chart is visited on its own UI
    thread.
    """
    # _addon_get's default deadline is 5s; nt_workspace makes one bounded Ui() hop PER window plus one
    # OnChart() hop per chart, and a real desktop has dozens of windows — 5s is barely enough for one of
    # them. This one call gets its own deadline; the process-wide app.HTTP_TIMEOUT is never moved.
    result = _addon_get("/workspace", _timeout=max(app.HTTP_TIMEOUT, _WORKSPACE_BUDGET_S))
    if isinstance(result, dict) and isinstance(result.get("windows"), list):
        for win in result["windows"]:
            if isinstance(win, dict):
                win["offscreen"] = _offscreen(win)
    return result


@mcp.tool(name="nt_strategies_running")
def nt_strategies_running(materialize: bool = False) -> dict:
    """Read the Control Center's Strategies grid: the strategy population that is NOT on a chart and
    that nt_charts/nt_workspace therefore cannot see. Returns {gridResolved, strategies, notes};
    `strategies` is a list of rows (name, parent, type, enabled, state, strategyId, liveState,
    instanceAlive, startedByBridge, account, instrument, connected, connection, dataSeries, position,
    accountPosition, averagePrice, realized, unrealized, trades, parameters, workspace), with one row
    per master entry plus one per per-instrument child (its `parent` names the master).
    `strategyId` is NinjaTrader's strategy Id (a string) — the handle nt_grid_remove and
    nt_grid_enable take. NinjaTrader runs a clone sharing that Id, so `liveState` is the most alive
    state over every instance with it and `instanceAlive` is true when one is in Configure..Realtime;
    `startedByBridge` is true for a run nt_strategy_start made. `enabled` is grid state and is NOT proof a strategy is running —
    believe `state`. When the grid could not be read, gridResolved is false and `strategies` is null,
    never [] — "we could not read it" and "there are none" are different claims. The grid is read from
    the Control Center's visual tree when the Strategies tab is selected and from its logical tree when
    it is not (`notes` says which; neither touches the window). Only if both miss does `notes` say to
    retry with materialize=True, which briefly cycles the Control Center's tabs and puts the original
    one back. Precondition: NT8 open with the AddOn running. This
    call never enables, disables or otherwise writes to the grid.
    """
    # The AddOn bounds its own grid read at Ws_GridTimeout (10s); the default 5s client deadline
    # would report a slow-but-healthy AddOn as unreachable before the AddOn's own 504 ever arrives.
    return _addon_get("/strategies/running",
                      _timeout=max(app.HTTP_TIMEOUT, _RUNNING_BUDGET_S),
                      materialize=1 if materialize else None)


@mcp.tool(name="nt_window_shot")
def nt_window_shot(window: str = "", chart: str = "", hwnd: int = 0, path: str = ""):
    """PNG of one NinjaTrader window, captured in-process with PrintWindow and returned as an image.
    Name the target with exactly one of: `window` (a case-insensitive substring of the window's title),
    `chart` (a chart id from nt_charts, or 'first'), or `hwnd`; with none of them it captures the whole
    virtual screen. Precedence if several are given: hwnd, then chart, then window. `path` chooses
    where the PNG is written on the NinjaTrader machine (default: a temp file). Precondition: NT8 open
    with the AddOn running, and the target window not minimized — this call never restores, fronts,
    moves or resizes a window, so a minimized target is an error you fix by restoring it yourself. A
    window covered by other windows still comes back whole (PrintWindow); a chart's canvas is a second
    window owned by NinjaTrader's render form and is merged in. Only the rare screen-blit fallback
    returns an occluded window occluded. Returns the image, or
    {"error": ...} when the window was not found, was minimized, or the file could not be written.
    """
    out = path or os.path.join(tempfile.gettempdir(), "nt8bridge", "window-shot.png")
    body = {"path": out}
    if window:
        body["window"] = window
    if chart:
        body["chart"] = chart
    if hwnd:
        body["hwnd"] = hwnd
    result = _addon_post("/screenshot", body)
    if not isinstance(result, dict) or not result.get("path"):
        return result  # {"error": ...}
    return Image(path=result["path"])


_WS_NOTE = ("No UI is shown: the AddOn calls NinjaTrader's own WorkspaceOptions methods on the "
            "Control Center's thread, opens no menu and no dialog. Nothing is deleted.")


def _ws_op(op: str, body: dict) -> dict:
    return _addon_post(f"/workspace/{op}", body,
                       _timeout=max(app.HTTP_TIMEOUT, _WORKSPACE_OP_BUDGET_S))


@mcp.tool(name="nt_workspace_switch")
def nt_workspace_switch(name: str) -> dict:
    """Make an OPEN NinjaTrader workspace the active one (WorkspaceOptions.SwitchToWorkspace).
    No UI is shown: no menu, no dialog, no mouse; nothing is deleted. A workspace that is not open is
    refused (409) — use nt_workspace_open. Refused (409) while a modal message box is open. Returns
    the read-back after the call: {ok, observed, op, name, alreadyActive, active, open,
    standingModal}; `observed`/`ok` are true only when the active workspace read back is `name`.
    Precondition: NT8 open with the AddOn running."""
    return _ws_op("switch", {"name": name})


@mcp.tool(name="nt_workspace_open")
def nt_workspace_open(name: str, activate: bool = True) -> dict:
    """Open a workspace FILE from NinjaTrader's workspaces folder (WorkspaceOptions.OpenWorkspace),
    also one written after NinjaTrader started that its Workspaces menu does not list. `name` is
    the file name without `.xml`; a path or separator is refused (400), a missing file is 404.
    activate=True makes it the active workspace. Already open: a switch when activate=True, else
    nothing. No UI is shown: no menu, no dialog, no mouse; nothing is deleted. Refused (409) while a
    modal message box is open. Returns the read-back: {ok, observed, name, alreadyOpen, returned,
    addedToSavedList, active, open, standingModal}. Precondition: NT8 open with the AddOn running."""
    return _ws_op("open", {"name": name, "activate": activate})


@mcp.tool(name="nt_workspace_close")
def nt_workspace_close(name: str, save: bool = False) -> dict:
    """Close an open workspace with no click needed (WorkspaceOptions.CloseWorkspace,
    removing=False: the file stays on disk, nothing is deleted). NinjaTrader's own "Do you want to
    save workspace '<name>'?" prompt is answered by the AddOn inside NinjaTrader: No for save=False,
    Yes for save=True (Yes is NinjaTrader's save). Cancel is never pressed and no other dialog is
    touched. No UI is driven from outside: no menu, no mouse, no keys. Refused (409) when it is the
    only open workspace, when the Strategies grid files an enabled or running strategy under it, or
    while a modal message box is already open; 503 when the grid cannot be read. 504 with
    `standingModal` when a prompt could not be found or answered in ~10 s: it is left for the user.
    Returns the read-back: {ok, observed, name, prompt ("answered No" / "answered Yes" / "none"),
    saved, returned, wasActive, active, open, standingModal}; `observed` = the name is gone from the
    open list. Precondition: NT8 open with the AddOn running."""
    return _ws_op("close", {"name": name, "save": save})


@mcp.tool(name="nt_workspace_save_as")
def nt_workspace_save_as(name: str, new_name: str, overwrite: bool = False) -> dict:
    """Save an OPEN workspace under a new name with no dialog (WorkspaceOptions.SaveWorkspaceAs).
    An existing `new_name.xml` is refused (409) unless overwrite=True; a `new_name` that is open is
    always refused. No UI is shown: no menu, no dialog, no mouse; nothing is deleted. Like
    NinjaTrader's own Save As it RENAMES the open workspace: afterwards `new_name` is open and
    `name` is not (`name.xml` stays on disk; `renamed` says so). Re-open `name` if you need both.
    Returns {ok, observed, name, newName, file, overwrote, renamed, returned, active, open, standingModal};
    `observed` = the file was written by this call. Precondition: NT8 open with the AddOn running."""
    return _ws_op("saveAs", {"name": name, "newName": new_name, "overwrite": overwrite})
