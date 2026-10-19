"""
AHK-MCP: AutoHotkey v2 computer-use tools via MCP.

Cursed but token-efficient alternative to screenshot-based computer use.
Each tool generates a temporary AHK v2 script, executes it, and returns
structured text results instead of images.

Token cost per action: ~200-700 tokens (vs ~2000-3500 for screenshot-based).
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

import cdp

# --- Configuration ---

AHK_EXE = os.environ.get(
    "AHK_EXE",
    str(Path.home() / "AppData/Local/Programs/AutoHotkey/v2/AutoHotkey64.exe"),
)
AHK_TIMEOUT = int(os.environ.get("AHK_TIMEOUT", "10"))
TEMP_DIR = Path(tempfile.gettempdir()) / "ahk-mcp"
TEMP_DIR.mkdir(exist_ok=True)


# Runtime errors would otherwise show a modal dialog (on the user's screen,
# until the timeout); report them on stderr instead. Prepended as one line, so
# reported line numbers are shifted back by one.
ERROR_HANDLER = ('OnError((e, *) => (FileAppend(e.Message (e.Extra = "" ? "" : ": " '
                 'e.Extra) " (line " (e.Line - 1) ")", "**"), ExitApp(2), 1))\n')


def run_ahk(script: str, timeout: int | None = None) -> str:
    """Write and execute an AHK v2 script, return its stdout."""
    timeout = timeout or AHK_TIMEOUT
    script_path = TEMP_DIR / f"cmd_{int(time.time()*1000)}.ahk"
    script_path.write_text(ERROR_HANDLER + script, encoding="utf-8")
    try:
        # /ErrorStdOut: script errors come back on stderr instead of as a
        # modal dialog that sits on the user's screen until the timeout.
        result = subprocess.run(
            [AHK_EXE, "/ErrorStdOut=UTF-8", "/CP65001", str(script_path)],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            # Syntax errors name the temp file and count the handler line.
            err = re.sub(r"^.*?\.ahk \((\d+)\) : ==>", lambda m: f"line {int(m[1]) - 1}:",
                         result.stderr.strip())
            output += f"\n[AHK Error] {err}"
        return output.strip()
    except subprocess.TimeoutExpired:
        return "[AHK Error] Script timed out"
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


# --- AHK script templates ---

SCRIPT_WINDOWS = r"""
#Requires AutoHotkey v2.0
out := ""
for hwnd in WinGetList() {
    try {
        title := WinGetTitle(hwnd)
        if (title = "")
            continue
        cls := WinGetClass(hwnd)
        pid := WinGetPID(hwnd)
        pos := ""
        try {
            WinGetPos(&x, &y, &w, &h, hwnd)
            pos := x . "," . y . "," . w . "," . h
        }
        out .= hwnd . "`t" . title . "`t" . cls . "`t" . pid . "`t" . pos . "`n"
    }
}
FileAppend out, "*"
ExitApp
"""

SCRIPT_FOCUS = r"""
#Requires AutoHotkey v2.0
target := "{target}"
try {{
    WinActivate target
    WinWaitActive target,, 3
    title := WinGetTitle("A")
    FileAppend "Focused: " . title, "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

# Focus and the action must happen inside one script: tool calls are seconds
# apart, and the terminal running the agent reclaims focus in between, so a
# separate ahk_focus call followed by ahk_type sends keys to the terminal.
ACTIVATE = r"""
win := "{win}"
if (win != "A") {{
    try WinActivate win
    if !WinWaitActive(win,, 2) {{
        FileAppend "[Error] Could not activate target window (active is: " . WinGetTitle("A") . ")", "*"
        ExitApp
    }}
    Sleep 100
}}
"""

# Keys go one at a time (Event mode, minimal delay, ~16 ms each). At full
# speed some apps drop or garble input: Win11 Notepad repeats one character
# for the rest of the text after the first word, and loses Shift states.
KEY_PACING = 'SendMode "Event"\nSetKeyDelay 1, 1\n'

SCRIPT_SEND = "#Requires AutoHotkey v2.0\n" + KEY_PACING + ACTIVATE + r"""
keys := "{keys}"
try {{
    title := WinGetTitle("A")
    exe := WinGetProcessName("A")
    Send keys
    FileAppend "Target: " . title . " [" . exe . "]`nSent: " . keys, "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

SCRIPT_SEND_TEXT = "#Requires AutoHotkey v2.0\n" + KEY_PACING + ACTIVATE + r"""
text := "{text}"
try {{
    title := WinGetTitle("A")
    exe := WinGetProcessName("A")
    SendText text
    FileAppend "Target: " . title . " [" . exe . "]`nTyped: " . StrLen(text) . " chars", "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

# Long text is pasted: paced typing runs ~60 chars/s. The clipboard is put
# back afterwards; apps read it asynchronously, hence the wait before that.
SCRIPT_PASTE_TEXT = "#Requires AutoHotkey v2.0\n" + ACTIVATE + r"""
text := "{text}"
try {{
    title := WinGetTitle("A")
    exe := WinGetProcessName("A")
    saved := ClipboardAll()
    A_Clipboard := ""
    A_Clipboard := text
    if !ClipWait(2)
        throw Error("the clipboard could not be set")
    Send "^v"
    Sleep 400
    A_Clipboard := saved
    FileAppend "Target: " . title . " [" . exe . "]`nPasted: " . StrLen(text) . " chars (clipboard restored)", "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""
PASTE_OVER = 80  # chars; ahk_type mode=auto pastes text longer than this

SCRIPT_CLICK = "#Requires AutoHotkey v2.0\n" + ACTIVATE + r"""
x := {x}
y := {y}
button := "{button}"
try {{
    prev := WinGetTitle("A")
    Click x, y, button
    Sleep 50
    post := WinGetTitle("A")
    FileAppend "Before: " . prev . "`nClicked: " . x . "," . y . " " . button . "`nAfter: " . post, "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

# Background input via posted window messages. Never touches focus or the
# cursor, but only classic Win32 controls act on these messages; browsers,
# WinUI/UWP apps (Win11 Notepad, Settings) and most Electron apps ignore them.
SCRIPT_CONTROL_SEND = r"""
#Requires AutoHotkey v2.0
win := "{win}"
ctl := "{control}"
keys := "{keys}"
try {{
    if (ctl = "")
        {send_cmd} keys,, win
    else
        {send_cmd} keys, ctl, win
    FileAppend "Target: " . WinGetTitle(win) . (ctl = "" ? "" : " / " . ctl) . "`nSent ({mode}): " . StrLen(keys) . " chars (background, focus unchanged: " . WinGetTitle("A") . ")", "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

SCRIPT_CONTROL_CLICK = r"""
#Requires AutoHotkey v2.0
win := "{win}"
ctl := "{control}"
try {{
    if (ctl != "")
        ControlClick ctl, win,, "{button}", {count}, "NA"
    else
        ControlClick "x{x} y{y}", win,, "{button}", {count}, "NA"
    FileAppend "Target: " . WinGetTitle(win) . "`nClicked: " . (ctl != "" ? ctl : "client {x},{y}") . " {button} x{count} (background, focus unchanged: " . WinGetTitle("A") . ")", "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

SCRIPT_READ = r"""
#Requires AutoHotkey v2.0
try {{
    title := WinGetTitle("A")
    text := WinGetText("A")
    ; Truncate to avoid enormous output
    if (StrLen(text) > 8000)
        text := SubStr(text, 1, 8000) . "`n... (truncated)"
    FileAppend "Window: " . title . "`n---`n" . text, "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

SCRIPT_READ_TARGET = r"""
#Requires AutoHotkey v2.0
target := "{target}"
try {{
    title := WinGetTitle(target)
    text := WinGetText(target)
    if (StrLen(text) > 8000)
        text := SubStr(text, 1, 8000) . "`n... (truncated)"
    FileAppend "Window: " . title . "`n---`n" . text, "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

SCRIPT_CLIPBOARD_GET = r"""
#Requires AutoHotkey v2.0
FileAppend A_Clipboard, "*"
ExitApp
"""

SCRIPT_CLIPBOARD_SET = r"""
#Requires AutoHotkey v2.0
A_Clipboard := "{content}"
FileAppend "Clipboard set: " . StrLen(A_Clipboard) . " chars", "*"
ExitApp
"""

SCRIPT_RUN = r"""
#Requires AutoHotkey v2.0
target := "{target}"
try {{
    Run target
    FileAppend "Launched: " . target, "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

SCRIPT_MSGBOX = r"""
#Requires AutoHotkey v2.0
MsgBox "{message}", "{title}", "OK"
ExitApp
"""

SCRIPT_WINDOW_INFO = r"""
#Requires AutoHotkey v2.0
win := "{win}"
try {{
    title := WinGetTitle(win)
    cls := WinGetClass(win)
    pid := WinGetPID(win)
    exe := WinGetProcessName(win)
    WinGetPos(&x, &y, &w, &h, win)
    out := "Title: " . title
    out .= "`nClass: " . cls
    out .= "`nPID: " . pid
    out .= "`nProcess: " . exe
    out .= "`nPosition: " . x . "," . y . " Size: " . w . "x" . h

    ; List controls
    try {{
        controls := WinGetControls(win)
        out .= "`n`nControls (" . controls.Length . "):"
        limit := Min(controls.Length, 50)
        loop limit {{
            ctl := controls[A_Index]
            try {{
                ctlText := ControlGetText(ctl, win)
                if (StrLen(ctlText) > 80)
                    ctlText := SubStr(ctlText, 1, 80) . "..."
                out .= "`n  " . ctl . ": " . ctlText
            }} catch {{
                out .= "`n  " . ctl
            }}
        }}
        if (controls.Length > 50)
            out .= "`n  ... (" . (controls.Length - 50) . " more)"
    }}

    FileAppend out, "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""


SCRIPT_SCREENSHOT = r"""
#Requires AutoHotkey v2.0
target := "{target}"
filepath := "{filepath}"
rx := {rx}
ry := {ry}
rw := {rw}
rh := {rh}

; Load GDI+
DllCall("LoadLibrary", "str", "gdiplus")
pToken := 0
si := Buffer(24, 0)
NumPut("uint", 1, si)
DllCall("gdiplus\GdiplusStartup", "ptr*", &pToken, "ptr", si, "ptr", 0)

; Determine capture mode
useScreen := false
if (target = "screen") {{
    useScreen := true
    cx := SysGet(76)  ; SM_XVIRTUALSCREEN
    cy := SysGet(77)  ; SM_YVIRTUALSCREEN
    cw := SysGet(78)  ; SM_CXVIRTUALSCREEN
    ch := SysGet(79)  ; SM_CYVIRTUALSCREEN
    wtitle := "Full Screen"
}} else {{
    if (target = "")
        hwnd := WinExist("A")
    else
        hwnd := WinExist(target)
    if (!hwnd) {{
        FileAppend "[Error] Window not found: " . target, "*"
        ExitApp
    }}
    WinGetPos(&cx, &cy, &cw, &ch, hwnd)
    wtitle := WinGetTitle(hwnd)
}}

; Apply region crop if specified
if (rw > 0 && rh > 0) {{
    cx := cx + rx
    cy := cy + ry
    cw := rw
    ch := rh
    wtitle .= " [region " . rx . "," . ry . " " . rw . "x" . rh . "]"
}}

hdcScreen := DllCall("GetDC", "ptr", 0, "ptr")
hdcMem := DllCall("CreateCompatibleDC", "ptr", hdcScreen, "ptr")
hBitmap := DllCall("CreateCompatibleBitmap", "ptr", hdcScreen, "int", cw, "int", ch, "ptr")
DllCall("SelectObject", "ptr", hdcMem, "ptr", hBitmap)

if (!useScreen && rw = 0) {{
    ; PrintWindow with PW_RENDERFULLCONTENT for proper window capture
    ; This captures the window's own rendering even when occluded
    DllCall("PrintWindow", "ptr", hwnd, "ptr", hdcMem, "uint", 0x2)
}} else {{
    ; BitBlt for full screen or region captures
    DllCall("BitBlt", "ptr", hdcMem, "int", 0, "int", 0, "int", cw, "int", ch
        , "ptr", hdcScreen, "int", cx, "int", cy, "uint", 0xCC0020)
}}

; Create GDI+ bitmap from HBITMAP
pBitmap := 0
DllCall("gdiplus\GdipCreateBitmapFromHBITMAP", "ptr", hBitmap, "ptr", 0, "ptr*", &pBitmap)

; PNG encoder CLSID: {{557CF406-1A04-11D3-9A73-0000F81EF32E}}
pCodec := Buffer(16)
NumPut("uint", 0x557CF406, pCodec, 0)
NumPut("ushort", 0x1A04, pCodec, 4)
NumPut("ushort", 0x11D3, pCodec, 6)
NumPut("uchar", 0x9A, pCodec, 8)
NumPut("uchar", 0x73, pCodec, 9)
NumPut("uchar", 0x00, pCodec, 10)
NumPut("uchar", 0x00, pCodec, 11)
NumPut("uchar", 0xF8, pCodec, 12)
NumPut("uchar", 0x1E, pCodec, 13)
NumPut("uchar", 0xF3, pCodec, 14)
NumPut("uchar", 0x2E, pCodec, 15)

DllCall("gdiplus\GdipSaveImageToFile", "ptr", pBitmap, "wstr", filepath, "ptr", pCodec, "ptr", 0)

; Cleanup
DllCall("gdiplus\GdipDisposeImage", "ptr", pBitmap)
DllCall("DeleteObject", "ptr", hBitmap)
DllCall("DeleteDC", "ptr", hdcMem)
DllCall("ReleaseDC", "ptr", 0, "ptr", hdcScreen)
DllCall("gdiplus\GdiplusShutdown", "ptr", pToken)

FileAppend "Captured: " . wtitle . " (" . cw . "x" . ch . ")`n" . filepath, "*"
ExitApp
"""


def escape_ahk(s: str) -> str:
    """Escape a string for embedding in AHK double-quoted strings."""
    return s.replace("\\", "\\\\").replace('"', '`"').replace("`", "``").replace("\n", "`n").replace("\r", "`r").replace("\t", "`t")


# --- UI Automation (Python-native) ---
#
# Browser page content sits 10-35 levels below the window root, and Firefox
# exposes every open tab's document at once (background tabs are offscreen
# with an empty rect). So searches use native FindAll calls with no depth cap,
# scoped by default to the active tab's document, and tree dumps collapse the
# unnamed wrapper nodes that make up most of that depth.

CONTROL_TYPE_NAMES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox",
    50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink",
    50006: "Image", 50007: "ListItem", 50008: "List",
    50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar",
    50018: "Tab", 50019: "TabItem", 50020: "Text",
    50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem",
    50030: "Document", 50031: "SplitButton", 50032: "Window",
    50033: "Pane", 50034: "Header", 50035: "HeaderItem",
    50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}
DOCUMENT_TYPE = 50030
# Shown in tree dumps even when unnamed: things an agent can act on.
INTERACTIVE_TYPES = {50000, 50002, 50003, 50004, 50005, 50007, 50011, 50013,
                     50015, 50016, 50019, 50024, 50031}
# Wrapper types that are dropped from tree dumps when unnamed.
WRAPPER_TYPES = {50006, 50020, 50021, 50025, 50026, 50033}

UIA_NAME = 30005
UIA_CONTROL_TYPE = 30003
UIA_RECT = 30001
UIA_OFFSCREEN = 30022
UIA_VALUE = 30045
UIA_RUNTIME_ID = 30000
TREE_SCOPE_DESCENDANTS = 4
TREE_SCOPE_SUBTREE = 7


def _ia():
    from uiautomation.uiautomation import _AutomationClient
    return _AutomationClient.instance().IUIAutomation


def _cache_request(subtree: bool = False):
    cr = _ia().CreateCacheRequest()
    for pid in (UIA_NAME, UIA_CONTROL_TYPE, UIA_RECT, UIA_OFFSCREEN, UIA_VALUE, UIA_RUNTIME_ID):
        cr.AddProperty(pid)
    if subtree:
        cr.TreeScope = TREE_SCOPE_SUBTREE
    return cr


ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def _norm_title(s: str) -> str:
    # Edge titles read "... - Microsoft​ Edge"; nobody types the ZWSP.
    return " ".join(s.translate(ZERO_WIDTH).split()).casefold()


def _find_window(target: str = ""):
    """Window by exact then partial title (case- and zero-width-insensitive),
    or the foreground window. None if absent."""
    import uiautomation as auto

    if not target:
        return auto.GetForegroundControl()
    want = _norm_title(target)
    windows = [w for w in auto.GetRootControl().GetChildren() if w.Name]
    for w in windows:
        if _norm_title(w.Name) == want:
            return w
    for w in windows:
        if want in _norm_title(w.Name):
            return w
    tokens = want.split()
    for w in windows:
        if all(tok in _norm_title(w.Name) for tok in tokens):
            return w
    return None


def _page_roots(win) -> list:
    """The active tab's top-level Document element(s), cached. Empty if none."""
    ia = _ia()
    cond = ia.CreatePropertyCondition(UIA_CONTROL_TYPE, DOCUMENT_TYPE)
    docs = win.Element.FindAllBuildCache(TREE_SCOPE_DESCENDANTS, cond, _cache_request())
    roots, rects = [], []
    for i in range(docs.Length if docs else 0):
        d = docs.GetElement(i)
        r = d.CachedBoundingRectangle
        if d.GetCachedPropertyValue(UIA_OFFSCREEN) or r.right <= r.left:
            continue
        # FindAll returns preorder, so an iframe's document comes after its
        # parent's and lies inside its rect; it's searched as part of the parent.
        if any(p.left <= r.left and p.top <= r.top and r.right <= p.right
               and r.bottom <= p.bottom for p in rects):
            continue
        roots.append(d)
        rects.append(r)
    return roots


def _scope_roots(win, scope: str) -> tuple[list, str]:
    """Resolve scope='page'|'window' to (root elements, label for output)."""
    if scope != "window":
        roots = _page_roots(win)
        if roots:
            return roots, "page"
    return [win.Element], "window"


def _el_type(el) -> int:
    return el.GetCachedPropertyValue(UIA_CONTROL_TYPE) or 0


def _el_value(el) -> str:
    v = el.GetCachedPropertyValue(UIA_VALUE)
    return v if isinstance(v, str) else ""


def _clip(s: str, n: int = 80) -> str:
    s = " ".join(s.split())
    return s[:n] + "..." if len(s) > n else s


def _describe(el, with_pos: bool = True) -> str:
    """One-line element summary: Type: "Name" @x,y,wxh = value (offscreen)."""
    line = CONTROL_TYPE_NAMES.get(_el_type(el), "Unknown")
    name = _clip(el.CachedName or "")
    if name:
        line += f': "{name}"'
    if with_pos:
        r = el.CachedBoundingRectangle
        if r.right > r.left:
            line += f" @{r.left},{r.top},{r.right - r.left}x{r.bottom - r.top}"
    value = _el_value(el)
    if value and value != (el.CachedName or ""):
        line += f" = {_clip(value, 100)}"
    if with_pos and el.GetCachedPropertyValue(UIA_OFFSCREEN):
        line += " (offscreen)"
    return line


def _type_ids(control_type: str) -> set[int]:
    """Control type ids whose name contains the filter ('link' -> Hyperlink)."""
    f = control_type.lower().removesuffix("control")
    return {tid for tid, n in CONTROL_TYPE_NAMES.items() if f in n.lower()}


def _name_matches(el, name: str) -> bool:
    """Substring match, or all words present in any order, over Name and Value."""
    haystack = f"{el.CachedName or ''} {_el_value(el)}".lower()
    needle = name.lower()
    return needle in haystack or all(tok in haystack for tok in needle.split())


def _find_elements(win, name: str = "", control_type: str = "", scope: str = "page",
                   max_results: int = 30, wait: float = 2.0) -> tuple[list, str]:
    """Search the scoped subtree; polls up to `wait` seconds for a match.

    Returns (cached elements, scope label). The elements are live, so patterns
    (Invoke, Value, ...) can be called on them directly.
    """
    ia = _ia()
    ids = _type_ids(control_type) if control_type else set()
    if control_type and not ids:
        raise ValueError(f"Unknown control type: {control_type}")
    # Filter by type natively when it's unambiguous; that's most of the cost saved.
    cond = (ia.CreatePropertyCondition(UIA_CONTROL_TYPE, next(iter(ids)))
            if len(ids) == 1 else ia.CreateTrueCondition())
    cr = _cache_request()
    deadline = time.time() + wait
    while True:
        roots, label = _scope_roots(win, scope)
        found = []
        for root in roots:
            arr = root.FindAllBuildCache(TREE_SCOPE_DESCENDANTS, cond, cr)
            for i in range(arr.Length if arr else 0):
                el = arr.GetElement(i)
                if ids and _el_type(el) not in ids:
                    continue
                if name and not _name_matches(el, name):
                    continue
                found.append(el)
                if len(found) >= max_results:
                    return found, label
        if found or time.time() >= deadline:
            return found, label
        time.sleep(0.3)


# --- Session element refs ---
#
# Every element a find, tree or wait reports gets a ref (e1, e2, ...) that
# later calls and batch steps can act on without searching again. The same
# element keeps its ref across searches (keyed by UIA RuntimeId). Refs hold
# the live element; once its UI is gone, using the ref says so.

REF_LIMIT = 2000
_refs: dict[str, tuple] = {}      # "e12" -> (element, window handle)
_ref_by_rid: dict[tuple, str] = {}
_next_ref = 1


def _remember(el, hwnd: int) -> str:
    global _next_ref
    rid = el.GetCachedPropertyValue(UIA_RUNTIME_ID)
    rid = tuple(rid) if rid else None
    if rid and rid in _ref_by_rid and _ref_by_rid[rid] in _refs:
        ref = _ref_by_rid[rid]
    else:
        ref = f"e{_next_ref}"
        _next_ref += 1
        if rid:
            _ref_by_rid[rid] = ref
    _refs[ref] = (el, hwnd)
    while len(_refs) > REF_LIMIT:
        del _refs[next(iter(_refs))]
    return ref


def _window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _resolve_ref(ref: str):
    """(uiautomation Control, description, window handle) for a ref like e12."""
    import uiautomation as auto

    key = ref.strip().lower()
    if key not in _refs:
        raise LookupError(f"Unknown ref {ref} (refs come from ahk_uia_find/tree/wait)")
    el, hwnd = _refs[key]
    try:
        el.CurrentControlType  # raises once the element's UI is gone
    except Exception:
        raise LookupError(f"Ref {ref} is stale (its element is gone); find it again") from None
    return auto.Control.CreateControlFromElement(el), _describe(el), hwnd


def uia_tree(target: str = "", max_depth: int = 8, max_nodes: int = 200,
             scope: str = "page") -> str:
    """Dump the accessibility tree, collapsing unnamed wrapper nodes."""
    win = _find_window(target)
    if win is None:
        return f"[Error] Window not found: {target}"
    roots, label = _scope_roots(win, scope)
    lines = [f"Window: {win.Name}  (scope: {label}; eN = ref)"]
    hwnd = win.NativeWindowHandle
    count = 0

    def shown(el, parent_name: str) -> bool:
        ctype = _el_type(el)
        name = (el.CachedName or "").strip()
        if ctype in INTERACTIVE_TYPES or _el_value(el):
            return True
        if not name:
            return ctype not in WRAPPER_TYPES
        # Link/button labels usually repeat as a Text child; drop the echo.
        return not (ctype == 50020 and name == parent_name)

    def walk(el, depth: int, parent_name: str):
        nonlocal count
        kids = el.GetCachedChildren()
        for i in range(kids.Length if kids else 0):
            if count >= max_nodes:
                return
            child = kids.GetElement(i)
            if shown(child, parent_name):
                count += 1
                # Refs only on things worth acting on, to keep reads cheap.
                ref = (_remember(child, hwnd) + " " if _el_type(child) in INTERACTIVE_TYPES
                       else "")
                lines.append("  " * depth + ref + _describe(child, with_pos=False))
                if depth < max_depth:
                    walk(child, depth + 1, (child.CachedName or "").strip())
            else:
                walk(child, depth, parent_name)

    cr = _cache_request(subtree=True)
    for root in roots:
        root = root.BuildUpdatedCache(cr)
        if label == "page":
            count += 1
            lines.append("  " + _describe(root, with_pos=False))
        walk(root, 2 if label == "page" else 1, "")
    if count >= max_nodes:
        lines.append(f"... (truncated at {max_nodes} nodes; use ahk_uia_find to search)")
    return "\n".join(lines)


def uia_find(target: str = "", name: str = "", control_type: str = "", max_results: int = 30,
             scope: str = "page", wait: float = 2.0) -> str:
    """Find UI elements by name and/or control type."""
    if not name and not control_type:
        return "[Error] Give name and/or control_type"
    win = _find_window(target)
    if win is None:
        return f"[Error] Window not found: {target}"
    found, label = _find_elements(win, name, control_type, scope, max_results, wait)
    header = f"Window: {win.Name}  (scope: {label})"
    if not found:
        return f"{header}\nNo elements found matching name='{name}' type='{control_type}'"
    hwnd = win.NativeWindowHandle
    lines = [f"{header}\nFound {len(found)} element(s) (eN = ref):"]
    lines += [f"{_remember(el, hwnd)} {_describe(el)}" for el in found]
    return "\n".join(lines)


def uia_url() -> str:
    """Get the URL from the active browser's address bar."""
    import uiautomation as auto

    win = auto.GetForegroundControl()
    title = win.Name or ""

    # Look for address bar patterns across browsers
    # Firefox: ToolBar > ComboBox (with URL)
    # Chrome/Edge: Edit control named "Address and search bar"
    for search_name in ["Address and search bar", "Search or enter address", "Search with Google or enter address"]:
        try:
            edit = win.EditControl(Name=search_name, searchDepth=8, searchInterval=0.3)
            if edit.Exists(0.5):
                vp = edit.GetValuePattern()
                if vp and vp.Value:
                    return f"Window: {title}\nURL: {vp.Value}"
        except Exception:
            continue

    # Fallback: find any Edit control with a URL-like value
    try:
        def find_url(element, depth):
            if depth > 6:
                return None
            try:
                children = element.GetChildren()
            except Exception:
                return None
            for child in children:
                if child.ControlTypeName in ("Edit", "ComboBox"):
                    try:
                        vp = child.GetValuePattern()
                        if vp and vp.Value and ("://" in vp.Value or "www." in vp.Value):
                            return vp.Value
                    except Exception:
                        pass
                result = find_url(child, depth + 1)
                if result:
                    return result
            return None

        url = find_url(win, 0)
        if url:
            return f"Window: {title}\nURL: {url}"
    except Exception as e:
        return f"[Error] {e}"

    return f"Window: {title}\nURL: (not found — may not be a browser, or address bar not accessible)"


def ahk_win(target: str = "") -> str:
    """AHK WinTitle for a target: 'A' for the active window, else 'ahk_id N'.

    Resolving in Python keeps matching identical across tools (case- and
    zero-width-insensitive, word order free) instead of AHK's own rules.
    """
    if not target:
        return "A"
    win = _find_window(target)
    if win is None:
        raise LookupError(f"Window not found: {target}")
    return f"ahk_id {win.NativeWindowHandle}"


# --- UIA actions ---
#
# Invoke/Value/Toggle work on occluded windows and on elements scrolled out of
# view, with no coordinates or cursor. But apps raise their own window when an
# action focuses an element (browsers always do, Win32 apps on SetValue), so
# by default a focus guard puts the previous foreground window back.

def _focus_guard(prev: int) -> str:
    """Restore `prev` as foreground if an action stole it. Returns a note."""
    user32 = ctypes.windll.user32
    stolen = False
    deadline = time.time() + 0.6  # activation can land after the call returns
    while time.time() < deadline:
        if user32.GetForegroundWindow() != prev:
            stolen = True
            cdp._set_foreground(prev)
        time.sleep(0.05)
    return "\nFocus guard: target raised itself; previous window restored" if stolen else ""


def _pick(win, name: str, control_type: str, index: int, scope: str, wait: float):
    import uiautomation as auto

    if control_type:
        found, _ = _find_elements(win, name, control_type, scope, index + 1, wait)
    else:
        # Without a type filter a label or wrapper often matches first; act on
        # the interactive element it describes instead (the sort is stable).
        found, _ = _find_elements(win, name, "", scope, max(index + 1, 30), wait)
        found.sort(key=lambda el: _el_type(el) not in INTERACTIVE_TYPES)
    if len(found) <= index:
        return None, (f"[Error] No element [{index}] matching name='{name}' type='{control_type}' "
                      f"({len(found)} found) in: {win.Name}")
    return auto.Control.CreateControlFromElement(found[index]), _describe(found[index])


def _target_element(target: str, ref: str, name: str, control_type: str, index: int,
                    scope: str, wait: float):
    """(Control, description, window title) by ref or by search; Control is
    None on failure, with the error message as the description."""
    if ref:
        try:
            ctl, desc, hwnd = _resolve_ref(ref)
        except LookupError as e:
            return None, f"[Error] {e}", ""
        return ctl, desc, _window_title(hwnd)
    if not name and not control_type:
        return None, "[Error] Give ref, or name and/or control_type", ""
    win = _find_window(target)
    if win is None:
        return None, f"[Error] Window not found: {target}", ""
    ctl, desc = _pick(win, name, control_type, index, scope, wait)
    return ctl, desc, win.Name


def uia_invoke(target: str = "", name: str = "", control_type: str = "", index: int = 0,
               action: str = "auto", scope: str = "page", wait: float = 2.0,
               keep_focus: bool = False, ref: str = "") -> str:
    import uiautomation as auto

    ctl, desc, win_name = _target_element(target, ref, name, control_type, index, scope, wait)
    if ctl is None:
        return desc

    def expand_toggle(p):
        # ExpandCollapseState: 0 collapsed, 1 expanded, 2 partial, 3 leaf
        return p.Collapse() if p.ExpandCollapseState == 1 else p.Expand()

    pid = auto.PatternId
    attempts = {
        "invoke": [("Invoke", pid.InvokePattern, lambda p: p.Invoke())],
        "toggle": [("Toggle", pid.TogglePattern, lambda p: p.Toggle())],
        "select": [("Select", pid.SelectionItemPattern, lambda p: p.Select())],
        "expand": [("Expand", pid.ExpandCollapsePattern, lambda p: p.Expand())],
        "collapse": [("Collapse", pid.ExpandCollapsePattern, lambda p: p.Collapse())],
        "default": [("DoDefaultAction", pid.LegacyIAccessiblePattern,
                     lambda p: p.DoDefaultAction())],
    }
    if action == "focus":
        plan = [("SetFocus", None, lambda _: ctl.SetFocus())]
    elif action == "auto":
        plan = (attempts["invoke"] + attempts["toggle"] + attempts["select"]
                + [("Expand/Collapse", pid.ExpandCollapsePattern, expand_toggle)]
                + attempts["default"])
    elif action in attempts:
        plan = attempts[action]
    else:
        return f"[Error] Unknown action: {action}"

    prev = ctypes.windll.user32.GetForegroundWindow()
    for label, pattern_id, run in plan:
        pattern = ctl if pattern_id is None else ctl.GetPattern(pattern_id)
        if not pattern:
            continue
        run(pattern)
        note = "" if keep_focus else _focus_guard(prev)
        return f"Window: {win_name}\n{label}: {desc}{note}"
    return f"[Error] Element supports none of: {', '.join(p[0] for p in plan)}\n{desc}"


def uia_set_value(target: str = "", name: str = "", control_type: str = "Edit",
                  value: str = "", index: int = 0, scope: str = "page", wait: float = 2.0,
                  keep_focus: bool = False, ref: str = "") -> str:
    ctl, desc, win_name = _target_element(target, ref, name, control_type, index, scope, wait)
    if ctl is None:
        return desc
    import uiautomation as auto

    prev = ctypes.windll.user32.GetForegroundWindow()
    method = "ValuePattern"
    vp = ctl.GetPattern(auto.PatternId.ValuePattern)
    if vp:
        vp.SetValue(value)
    else:
        legacy = ctl.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
        if not legacy:
            return f"[Error] Element has no settable value\n{desc}"
        legacy.SetValue(value)
        method = "LegacyIAccessible"
    note = "" if keep_focus else _focus_guard(prev)
    try:
        now = ctl.GetPattern(auto.PatternId.ValuePattern).Value
    except Exception:
        now = None
    check = "" if now is None else (" (verified)" if now == value else f"\nRead back: {now!r}")
    return f"Window: {win_name}\nSet via {method}: {desc}{check}{note}"


# --- Browser (Chromium via DevTools Protocol) ---

DEFAULT_BROWSER = os.environ.get("AHK_BROWSER", "edge")
BROWSER_FLAGS = [
    # Expose page content to UIA as well (ahk_uia_* on browser windows).
    "--force-renderer-accessibility",
    # Keep rendering and the accessibility tree alive while the window is
    # covered; Chromium otherwise treats fully occluded windows as hidden.
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-backgrounding-occluded-windows",
]


def _browser_ctx(args: dict):
    b = cdp.get_browser(args.get("browser") or DEFAULT_BROWSER)
    tab = cdp.resolve_tab(b, args.get("tab"))
    return b, tab, b.session(tab["targetId"])


def _browser_node(b, sid, args: dict) -> tuple[int, str]:
    """Element from ref='#123' or name/role/index. Returns (backendNodeId, description)."""
    ref = str(args.get("ref", "")).lstrip("#")
    if ref:
        return int(ref), f"#{ref}"
    name, role = args.get("name", ""), args.get("role", "")
    if not name and not role:
        raise ValueError("Give ref, or name and/or role")
    index = args.get("index", 0)
    found = cdp.find(b, sid, name, role, index + 1, args.get("wait", 2.0))
    if len(found) <= index:
        raise LookupError(f"No element [{index}] matching name='{name}' role='{role}' "
                          f"({len(found)} found)")
    return found[index]["backendDOMNodeId"], cdp.describe(found[index])


def browser_launch(browser: str = "", url: str = "", restart: bool = False) -> str:
    browser = browser or DEFAULT_BROWSER
    exe = cdp.BROWSER_EXES[browser]
    running = bool(cdp._pids(exe))
    lines = []
    if running and restart:
        # WM_CLOSE each window so the session is saved and restored on relaunch.
        run_ahk(f'#Requires AutoHotkey v2.0\nfor h in WinGetList("ahk_exe {exe}")\n'
                f'    if WinGetTitle(h) != ""\n        PostMessage 0x10, 0, 0, , h\nExitApp')
        deadline = time.time() + 15
        while cdp._pids(exe) and time.time() < deadline:
            time.sleep(0.5)
        if cdp._pids(exe):
            return f"[Error] {browser} didn't close within 15s (an unsaved-form prompt?)"
        running = False
        lines.append(f"Closed {browser}; relaunching with automation flags.")
    if running:
        if url:
            subprocess.Popen(["cmd", "/c", "start", "", exe.removesuffix(".exe"), url],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            lines.append(f"Opened {url} in the running {browser}.")
        lines.append(f"{browser} was already running, so launch flags weren't applied; "
                     "pass restart=true to relaunch with them (the session is restored).")
    else:
        argv = [exe.removesuffix(".exe"), *BROWSER_FLAGS]
        argv += ["--restore-last-session"] if restart else []
        argv += [url] if url else []
        subprocess.Popen(["cmd", "/c", "start", "", *argv],
                         creationflags=subprocess.CREATE_NO_WINDOW)
        lines.append(f"Launched {browser} with: {' '.join(BROWSER_FLAGS)}")
        deadline = time.time() + 10
        while time.time() < deadline and not cdp._browser_window(browser):
            time.sleep(0.3)
    port_file = cdp.USER_DATA_DIRS[browser] / "DevToolsActivePort"
    deadline = time.time() + 5
    while not port_file.exists() and time.time() < deadline:
        time.sleep(0.3)
    if port_file.exists():
        lines.append("DevTools remote debugging is enabled. The first ahk_browser_* call "
                     f"brings {browser} forward once to approve the connection.")
    else:
        lines.append(f"DevTools remote debugging is OFF. To enable (one-time, persists): open "
                     f"{cdp.ENABLE_URLS[browser]} and tick 'Allow remote debugging for this "
                     "browser instance'. Until then only ahk_uia_* tools work on it.")
    return "\n".join(lines)


def browser_find(args: dict) -> str:
    b, tab, sid = _browser_ctx(args)
    name, role = args.get("name", ""), args.get("role", "")
    if not name and not role:
        return "[Error] Give name and/or role"
    found = cdp.find(b, sid, name, role, args.get("max_results", 30), args.get("wait", 2.0))
    header = f"Tab: {tab['title'][:80]}  | {tab['url'][:80]}"
    if not found:
        return f"{header}\nNo elements found matching name='{name}' role='{role}'"
    return "\n".join([f"{header}\nFound {len(found)} element(s) (#N = ref):"]
                     + [f"[{i}] {cdp.describe(n)}" for i, n in enumerate(found)])


def browser_click(args: dict) -> str:
    b, tab, sid = _browser_ctx(args)
    button, count = args.get("button", "left"), args.get("count", 1)
    if "x" in args and "y" in args:
        cdp.click_at(b, sid, args["x"], args["y"], button, count)
        what = f"viewport {args['x']},{args['y']}"
    else:
        node, what = _browser_node(b, sid, args)
        x, y = cdp.click_node(b, sid, node, button, count)
        what += f" @{x:.0f},{y:.0f}"
    time.sleep(0.3)
    return f"Clicked: {what} ({button} x{count})\nNow: {cdp.page_state(b, sid)}"


def browser_type(args: dict) -> str:
    b, tab, sid = _browser_ctx(args)
    done = []
    if args.get("ref") or args.get("name") or args.get("role"):
        node, what = _browser_node(b, sid, args)
        cdp.focus_node(b, sid, node)
        done.append(f"Focused: {what}")
    if args.get("clear"):
        cdp.press_keys(b, sid, "Ctrl+A Backspace")
        done.append("Cleared")
    if args.get("text"):
        cdp.insert_text(b, sid, args["text"])
        done.append(f"Typed: {len(args['text'])} chars")
    if args.get("keys"):
        cdp.press_keys(b, sid, args["keys"])
        done.append(f"Keys: {args['keys']}")
    if not done:
        return "[Error] Give text and/or keys"
    time.sleep(0.2)
    return "\n".join(done + [f"Now: {cdp.page_state(b, sid)}"])


def browser_navigate(args: dict) -> str:
    b = cdp.get_browser(args.get("browser") or DEFAULT_BROWSER)
    if args.get("new_tab"):
        # background=True: the new tab doesn't become the selected tab
        target = b.send("Target.createTarget", {"url": "about:blank", "background": True})
        sid = b.session(target["targetId"])
    else:
        sid = b.session(cdp.resolve_tab(b, args.get("tab"))["targetId"])
    return f"Loaded: {cdp.navigate(b, sid, args['url'])}"


def browser_dispatch(name: str, args: dict) -> str:
    if name == "ahk_browser_launch":
        return browser_launch(args.get("browser", ""), args.get("url", ""),
                              args.get("restart", False))
    if name == "ahk_browser_tabs":
        b = cdp.get_browser(args.get("browser") or DEFAULT_BROWSER)
        return cdp.list_tabs(b)
    if name == "ahk_browser_find":
        return browser_find(args)
    if name == "ahk_browser_tree":
        b, tab, sid = _browser_ctx(args)
        return cdp.tree(b, sid, args.get("max_depth", 8), args.get("max_nodes", 200))
    if name == "ahk_browser_click":
        return browser_click(args)
    if name == "ahk_browser_type":
        return browser_type(args)
    if name == "ahk_browser_navigate":
        return browser_navigate(args)
    if name == "ahk_browser_eval":
        b, tab, sid = _browser_ctx(args)
        value = cdp.evaluate(b, sid, args["script"], args.get("timeout", 30))
        return json.dumps(value, ensure_ascii=False, indent=1) if not isinstance(value, str) else value
    return f"Unknown tool: {name}"


# --- What changed ---
#
# Action results end with a one-line report of what the action did to the
# desktop (windows opened, closed or retitled, what came to the front and
# what has focus there), so the agent rarely needs a separate look. Windows
# of the agent's own host (the terminal running it, found by walking up the
# process tree) are left out: Claude Code animates the terminal title.

_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
_host_pids: set[int] | None = None


def _ancestor_pids() -> set[int]:
    import ctypes.wintypes as wt

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
    procs = {}
    ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
    while ok:
        procs[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
        ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    k32.CloseHandle(ctypes.c_void_p(snap))
    pids, pid = set(), os.getpid()
    # explorer.exe owns File Explorer windows too, so the walk stops there.
    while pid in procs and pid not in pids and procs[pid][1] != "explorer.exe":
        pids.add(pid)
        pid = procs[pid][0]
    return pids


def _is_host(hwnd: int) -> bool:
    global _host_pids
    if _host_pids is None:
        _host_pids = _ancestor_pids()
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value in _host_pids


def _windows() -> dict[int, str]:
    """Visible titled top-level windows, minus the agent's own host."""
    user32 = ctypes.windll.user32
    out = {}

    cls = ctypes.create_unicode_buffer(16)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or _is_host(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd):
            out[hwnd] = _window_title(hwnd)
        else:
            # Popup menus are untitled; an unexpected one (a stray Alt) eats input.
            user32.GetClassNameW(ctypes.c_void_p(hwnd), cls, 16)
            if cls.value == "#32768":
                out[hwnd] = "(menu)"
        return True

    user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return out


def _snapshot() -> tuple[int, dict]:
    return ctypes.windll.user32.GetForegroundWindow(), _windows()


def _focus_desc() -> str:
    import uiautomation as auto
    try:
        el = auto.GetFocusedControl()
        name = _clip(el.Name or "", 50)
        return f"{el.ControlTypeName.removesuffix('Control')}" + (f' "{name}"' if name else "")
    except Exception:
        return ""


def _changes(before: tuple[int, dict], settle: float, quiet: bool = False) -> str:
    """Report what changed since `before`, polling up to `settle` seconds for
    the first change (windows appear a beat after the action that opens them)."""
    fg0, wins0 = before
    deadline = time.time() + settle
    while True:
        fg, wins = _snapshot()
        if fg != fg0 or wins != wins0:
            time.sleep(0.15)  # changes come in stages: let the rest land
            fg, wins = _snapshot()
            break
        if time.time() >= deadline:
            break
        time.sleep(0.05)
    t = lambda s: f'"{_clip(s, 60)}"'
    parts = [f"opened {t(wins[h])}" for h in wins if h not in wins0]
    parts += [f"closed {t(wins0[h])}" for h in wins0 if h not in wins]
    parts += [f"title {t(wins0[h])} -> {t(wins[h])}" for h in wins
              if h in wins0 and wins0[h] != wins[h]]
    if fg != fg0:
        if _is_host(fg):
            parts.append("front: the agent's terminal")
        else:
            focus = _focus_desc()
            parts.append(f"front {t(_window_title(fg))}" + (f", focus {focus}" if focus else ""))
    if not parts:
        return "" if quiet else "\nChanged: no window changes"
    return "\nChanged: " + "; ".join(parts)


# Seconds to watch for a change after each action tool. Browser tools already
# report the page; their window line is shown only when something changed.
ACTION_SETTLE = {
    "ahk_focus": 0.3, "ahk_send": 0.3, "ahk_type": 0.3, "ahk_click": 0.3,
    "ahk_run": 3.0, "ahk_uia_invoke": 0.3, "ahk_uia_set_value": 0.3,
    "ahk_control_send": 0.3, "ahk_control_click": 0.3,
    "ahk_browser_click": 0.0, "ahk_browser_type": 0.0, "ahk_browser_navigate": 0.0,
}


# --- Waiting and batches ---

def wait_for(args: dict) -> str:
    """Poll until a window, element or page condition holds, or time out."""
    until = args.get("until", "")
    gone = args.get("gone", False)
    timeout = float(args.get("timeout", 10))
    start = time.time()

    if until == "window":
        target = args["target"]

        def check():
            win = _find_window(target)
            return (win is None) == gone, (f'window "{win.Name}"' if win else f"no window {target!r}")
    elif until == "element":
        name, ctype = args.get("name", ""), args.get("control_type", "")
        if not name and not ctype:
            return "[Error] Give name and/or control_type"

        def check():
            win = _find_window(args.get("target", ""))
            found = _find_elements(win, name, ctype, args.get("scope", "page"), 1, 0)[0] if win else []
            if found:
                return not gone, f"{_remember(found[0], win.NativeWindowHandle)} {_describe(found[0])}"
            return gone, f"no element name={name!r} type={ctype!r}"
    elif until in ("browser_element", "browser_page"):
        b, tab, sid = _browser_ctx(args)
        name, role = args.get("name", ""), args.get("role", "")
        url, title = args.get("url", "").lower(), args.get("title", "").lower()
        if until == "browser_element" and not name and not role:
            return "[Error] Give name and/or role"

        def check():
            if until == "browser_element":
                found = cdp.find(b, sid, name, role, 1, 0)
                if found:
                    return not gone, cdp.describe(found[0])
                return gone, f"no element name={name!r} role={role!r}"
            try:
                v = cdp.evaluate(b, sid, "[document.title, location.href, document.readyState]", 5)
            except cdp.CDPError:
                return False, "page loading"  # context destroyed mid-navigation
            ok = v[2] == "complete" and url in v[1].lower() and title in v[0].lower()
            return ok, f"{v[0]}  | {v[1]}" + ("" if v[2] == "complete" else f"  ({v[2]})")
    else:
        return "[Error] until must be window, element, browser_element or browser_page"

    while True:
        ok, what = check()
        if ok:
            return f"Waited {time.time() - start:.1f}s: {what}"
        if time.time() - start >= timeout:
            return f"[Error] Timed out after {timeout:g}s waiting for {until}{' gone' if gone else ''} (last: {what})"
        time.sleep(0.25)


def batch(args: dict) -> str:
    """Run steps in order in one call; stop at the first error unless told not to."""
    steps = args["steps"]
    keep_going = args.get("continue_on_error", False)
    tool_names = {t.name for t in TOOLS}
    before = _snapshot()
    settle, lines, failed = 0.0, [], None
    for i, step in enumerate(steps, 1):
        step = dict(step)
        tool = str(step.pop("tool", ""))
        if tool == "sleep":
            time.sleep(min(float(step.get("seconds", 0.5)), 30))
            lines.append(f"{i}. sleep {step.get('seconds', 0.5)}s")
            continue
        name = tool if tool.startswith("ahk_") else "ahk_" + tool
        if name not in tool_names or name == "ahk_batch":
            out = f"[Error] Unknown step tool: {tool}"
        else:
            try:
                out = dispatch(name, step, report=False)
            except KeyError as e:
                out = f"[Error] Missing required argument: {e.args[0]}"
            except Exception as e:
                out = f"[Error] {type(e).__name__}: {e}"
        settle = ACTION_SETTLE.get(name, settle)
        lines.append(f"{i}. {name.removeprefix('ahk_')}: " + out.replace("\n", "\n   "))
        if out.startswith(ERROR_PREFIXES) and failed is None:
            failed = i
            if not keep_going:
                break
    head = (f"[Error] Step {failed} of {len(steps)} failed" + ("" if keep_going else "; stopped")
            if failed else f"Ran {len(steps)} step(s)")
    return head + "\n" + "\n".join(lines) + _changes(before, settle)


# --- MCP Server ---

TOOLS = [
    types.Tool(
        name="ahk_windows",
        description="List all visible windows with title, class, PID, and position. "
        "Use this instead of screenshots to understand what's on screen.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="ahk_focus",
        description="Activate/focus a window by title (supports partial match). "
        "Example: 'Notepad', 'Chrome', 'Visual Studio'.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Window title (partial match OK)",
                },
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="ahk_send",
        description="Send keystrokes to a window (takes focus). Uses AHK Send syntax: "
        "{Enter}, {Tab}, ^c (Ctrl+C), !{F4} (Alt+F4), +{Home} (Shift+Home), etc. "
        "Pass target= so focus and keys happen atomically. For plain text use ahk_type; "
        "for background input see ahk_control_send / ahk_browser_type.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window to activate first, verified, in the same call (title words, any order). Recommended: focus set by an earlier call is usually lost by the time this runs."},
                "keys": {
                    "type": "string",
                    "description": "AHK v2 Send key sequence",
                },
            },
            "required": ["keys"],
        },
    ),
    types.Tool(
        name="ahk_type",
        description="Type plain text into a window (no special key interpretation; takes focus). "
        f"Text over {PASTE_OVER} chars is pasted (clipboard saved and restored); shorter text is "
        "typed key by key. Pass target= so focus and typing happen atomically.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window to activate first, verified, in the same call (title words, any order). Recommended: focus set by an earlier call is usually lost by the time this runs."},
                "text": {
                    "type": "string",
                    "description": "Text to type",
                },
                "mode": {"type": "string", "enum": ["auto", "keys", "paste"], "description": "auto (default): paste long text, type short. keys: always type (for apps where Ctrl+V differs, like terminals). paste: always paste"},
            },
            "required": ["text"],
        },
    ),
    types.Tool(
        name="ahk_click",
        description="Click with the real cursor (takes focus): at x,y, or on the centre of a ref "
        "from ahk_uia_find/tree (for elements Invoke doesn't work on). Pass target= with x,y so the "
        "window is in front when the click lands. Background alternatives: "
        "ahk_uia_invoke, ahk_control_click, ahk_browser_click.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window to activate first, verified, in the same call (title words, any order). Recommended: focus set by an earlier call is usually lost by the time this runs."},
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
                "ref": {"type": "string", "description": "Element ref like e12 (instead of x,y; its window is activated)"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Mouse button (default: left)",
                },
            },
        },
    ),
    types.Tool(
        name="ahk_read",
        description="Read all text content from a window. "
        "Much cheaper than a screenshot — returns actual text, not pixels.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Window title (omit for active window)",
                },
            },
        },
    ),
    types.Tool(
        name="ahk_window_info",
        description="Get detailed info about a window: title, class, PID, process name, "
        "position, size, and its Win32 controls (ClassNN names, for ahk_control_send/click) with their text.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window title words (omit for active window)"},
            },
        },
    ),
    types.Tool(
        name="ahk_clipboard",
        description="Get or set the system clipboard.",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set"],
                    "description": "Get or set clipboard",
                },
                "content": {
                    "type": "string",
                    "description": "Content to set (only for action=set)",
                },
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="ahk_run",
        description="Launch a program or open a file/URL.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Program path, file, or URL to open",
                },
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="ahk_msgbox",
        description="Show a message box to the user. Useful for notifications or confirmations.",
        inputSchema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message body"},
                "title": {"type": "string", "description": "Window title (default: AHK-MCP)"},
            },
            "required": ["message"],
        },
    ),
    types.Tool(
        name="ahk_screenshot",
        description="Capture a screenshot of a window, region, or full screen. Returns a PNG file path "
        "— use the Read tool to view it. Per-window captures use PrintWindow (works even when "
        "occluded). Specify x/y/w/h for a region crop relative to the window or screen.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Window title (partial match), 'screen' for full desktop, or omit for active window",
                },
                "x": {"type": "integer", "description": "Region X offset (relative to window/screen)"},
                "y": {"type": "integer", "description": "Region Y offset"},
                "w": {"type": "integer", "description": "Region width"},
                "h": {"type": "integer", "description": "Region height"},
            },
        },
    ),
    types.Tool(
        name="ahk_uia_tree",
        description="Dump the UI Automation accessibility tree of a window. Returns element types, "
        "names, and values in a structured text format. This is how you read browser content, "
        "form fields, and UI state that WinGetText can't see. In browsers it shows the active "
        "tab's page by default; unnamed layout wrappers are collapsed and don't count toward depth. "
        "Interactive elements carry refs (e12) for ahk_uia_invoke/set_value/click.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window title (omit for active window)"},
                "max_depth": {"type": "integer", "description": "Max depth of shown nodes (default: 8)"},
                "max_nodes": {"type": "integer", "description": "Max nodes to return (default: 200)"},
                "scope": {
                    "type": "string",
                    "enum": ["page", "window"],
                    "description": "'page' (default): the active browser tab's document, or the whole "
                    "window if it has none. 'window': everything, including browser chrome and "
                    "background tabs.",
                },
            },
        },
    ),
    types.Tool(
        name="ahk_uia_find",
        description="Search for UI elements by name and/or control type within a window. "
        "Returns matches with position, value and a ref (e12) that ahk_uia_invoke/set_value/click "
        "take for the rest of the session. Searches the full depth of the tree "
        "(browser page content sits 10-35 levels deep). Name matching is token-based: all words, "
        "any order. Polls briefly (wait=, default 2s) so pages still rendering after a click "
        "are caught. Useful for finding buttons, links, inputs, or any named element.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window title (omit for active window)"},
                "name": {"type": "string", "description": "Text to match in element name/value (all words, any order)"},
                "control_type": {"type": "string", "description": "Control type: Button, Edit, Hyperlink, Text, etc. (partial match: 'link' = Hyperlink)"},
                "max_results": {"type": "integer", "description": "Max results (default: 30)"},
                "scope": {
                    "type": "string",
                    "enum": ["page", "window"],
                    "description": "'page' (default): the active browser tab's document, or the whole "
                    "window if it has none. 'window': everything, including browser chrome and "
                    "background tabs.",
                },
                "wait": {"type": "number", "description": "Seconds to poll for a match before returning empty (default 2; 0 = check once)"},
            },
        },
    ),
    types.Tool(
        name="ahk_uia_url",
        description="Get the URL from the active browser's address bar via UI Automation. "
        "Works with Firefox, Chrome, and Edge.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="ahk_eval",
        description="Execute arbitrary AHK v2 code. Output via: FileAppend \"text\", \"*\" "
        "and end with ExitApp. This is the escape hatch for anything the other tools can't do. "
        "Full AHK v2 language available: COM objects, UI Automation, regex, file I/O, etc.",
        inputSchema={
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "AHK v2 script code",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 10)",
                },
            },
            "required": ["script"],
        },
    ),
    # --- Background actions: UIA (any app; focus guarded) ---
    types.Tool(
        name="ahk_uia_invoke",
        description="Find a UI element (same matching as ahk_uia_find) and act on it through UI "
        "Automation: no coordinates, no cursor, works on occluded windows and elements scrolled "
        "out of view. action=auto tries Invoke, Toggle, Select, Expand/Collapse, then the default "
        "action. Many apps (all browsers) raise their window when acted on; a focus guard puts "
        "the previous foreground window back. For Chromium prefer ahk_browser_click (no flash).",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window title words (omit for active window)"},
                "name": {"type": "string", "description": "Text in element name/value (all words, any order)"},
                "control_type": {"type": "string", "description": "Button, Hyperlink, CheckBox, ListItem, MenuItem, TabItem, ..."},
                "ref": {"type": "string", "description": "Element ref like e12 from ahk_uia_find/tree/wait (instead of searching)"},
                "index": {"type": "integer", "description": "Which match (0-based, default 0)"},
                "action": {"type": "string", "enum": ["auto", "invoke", "toggle", "select", "expand", "collapse", "focus", "default"]},
                "scope": {"type": "string", "enum": ["page", "window"], "description": "As in ahk_uia_find (default page)"},
                "wait": {"type": "number", "description": "Seconds to poll for the element (default 2)"},
                "keep_focus": {"type": "boolean", "description": "Let the target keep focus if it takes it (default false)"},
            },
        },
    ),
    types.Tool(
        name="ahk_uia_set_value",
        description="Set the text of an input through UI Automation (ValuePattern) without typing: "
        "no focus needed, fires real input/change events (React-style apps see it). Works for "
        "native edit boxes and browser inputs; contenteditable editors need ahk_browser_type. "
        "Focus guarded like ahk_uia_invoke.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window title words (omit for active window)"},
                "name": {"type": "string", "description": "Field name/label text (all words, any order)"},
                "control_type": {"type": "string", "description": "Default Edit; ComboBox for editable combos"},
                "value": {"type": "string", "description": "The full new value (replaces existing text)"},
                "ref": {"type": "string", "description": "Element ref like e12 (instead of searching)"},
                "index": {"type": "integer", "description": "Which match (0-based, default 0)"},
                "scope": {"type": "string", "enum": ["page", "window"]},
                "wait": {"type": "number", "description": "Seconds to poll for the element (default 2)"},
                "keep_focus": {"type": "boolean", "description": "Let the target keep focus if it takes it (default false)"},
            },
            "required": ["value"],
        },
    ),
    # --- Background actions: posted window messages (classic Win32 only) ---
    types.Tool(
        name="ahk_control_send",
        description="Send text or keys to a window/control in the background via posted messages "
        "(AHK ControlSend). True background: no focus change, no flash. Only classic Win32 "
        "controls respond (dialogs, older apps, AHK GUIs); browsers, WinUI/UWP apps (Win11 "
        "Notepad, Settings) and most Electron apps ignore it. Get control names from ahk_window_info.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window title words"},
                "control": {"type": "string", "description": "ClassNN like Edit1 (omit: the window's focused control)"},
                "text": {"type": "string", "description": "Literal text to send"},
                "keys": {"type": "string", "description": "Key sequence in AHK Send syntax ({Enter}, ^a, ...); set raw=true"},
                "raw": {"type": "boolean", "description": "Interpret keys= as AHK key syntax (default false: literal text)"},
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="ahk_control_click",
        description="Click a control (or client-area x,y) in a background window via posted "
        "messages (AHK ControlClick NA). No cursor movement, no focus change. Classic Win32 "
        "only, like ahk_control_send.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Window title words"},
                "control": {"type": "string", "description": "ClassNN like Button1, or the control's text"},
                "x": {"type": "integer", "description": "Client-area X (when no control given)"},
                "y": {"type": "integer", "description": "Client-area Y (when no control given)"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "count": {"type": "integer", "description": "Click count (default 1)"},
            },
            "required": ["target"],
        },
    ),
    # --- Flow: waiting and batches ---
    types.Tool(
        name="ahk_wait",
        description="Wait until something appears (or, with gone=true, disappears): a window, a UI "
        "element, a Chromium page element, or a Chromium page finished loading with a URL/title. "
        "Returns as soon as it holds, with the element's ref. Use instead of guessing sleeps.",
        inputSchema={
            "type": "object",
            "properties": {
                "until": {"type": "string", "enum": ["window", "element", "browser_element", "browser_page"]},
                "target": {"type": "string", "description": "window: title words to wait for. element: window to search (omit for active)"},
                "name": {"type": "string", "description": "element/browser_element: name text (all words, any order)"},
                "control_type": {"type": "string", "description": "element: Button, Edit, ..."},
                "role": {"type": "string", "description": "browser_element: button, link, textbox, ..."},
                "url": {"type": "string", "description": "browser_page: text the URL must contain"},
                "title": {"type": "string", "description": "browser_page: text the title must contain"},
                "gone": {"type": "boolean", "description": "Wait for it to disappear instead"},
                "scope": {"type": "string", "enum": ["page", "window"]},
                "tab": {"type": "string", "description": "Browser tab index or title/URL text"},
                "browser": {"type": "string", "enum": ["edge", "chrome"]},
                "timeout": {"type": "number", "description": "Seconds (default 10)"},
            },
            "required": ["until"],
        },
    ),
    types.Tool(
        name="ahk_batch",
        description="Run several tool calls in order in one call: each step is an object with "
        "\"tool\" (any ahk_* tool name, the ahk_ prefix optional, or \"sleep\" with \"seconds\") plus "
        "that tool's arguments. Stops at the first failing step. One what-changed report at the "
        "end. Example: [{\"tool\":\"run\",\"target\":\"notepad\"}, {\"tool\":\"wait\",\"until\":"
        "\"window\",\"target\":\"Notepad\"}, {\"tool\":\"type\",\"target\":\"Notepad\",\"text\":\"hi\"}]",
        inputSchema={
            "type": "object",
            "properties": {
                "steps": {"type": "array", "items": {"type": "object"}, "description": "Steps to run in order"},
                "continue_on_error": {"type": "boolean", "description": "Run the remaining steps after a failure (default false)"},
            },
            "required": ["steps"],
        },
    ),
    # --- Browser: Chromium (Edge/Chrome) via DevTools Protocol, true background ---
    types.Tool(
        name="ahk_browser_launch",
        description="Start Edge/Chrome (your normal profile) with flags that keep pages readable "
        "and controllable while the window is covered, and report whether DevTools remote "
        "debugging (needed by ahk_browser_*) is enabled. restart=true closes and relaunches a "
        "running browser with the flags, restoring its tabs.",
        inputSchema={
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["edge", "chrome"], "description": "Default edge (env AHK_BROWSER)"},
                "url": {"type": "string", "description": "Optional URL to open"},
                "restart": {"type": "boolean", "description": "Relaunch a running browser to apply the flags"},
            },
        },
    ),
    types.Tool(
        name="ahk_browser_tabs",
        description="List open tabs in Edge/Chrome as [index] title | url. Index or title/URL "
        "text can be passed as tab= to the other ahk_browser_* tools.",
        inputSchema={
            "type": "object",
            "properties": {"browser": {"type": "string", "enum": ["edge", "chrome"]}},
        },
    ),
    types.Tool(
        name="ahk_browser_find",
        description="Search a Chromium tab's accessibility tree by name and/or role. Returns "
        "numbered matches with a #ref for ahk_browser_click/type. Name matching: all words, any "
        "order. Polls briefly (wait=) for pages still rendering. Works in the background.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Text in accessible name/value (all words, any order)"},
                "role": {"type": "string", "description": "button, link, textbox, checkbox, combobox, heading, ... (partial match)"},
                "tab": {"type": "string", "description": "Tab index or title/URL text (default: the visible tab)"},
                "max_results": {"type": "integer", "description": "Default 30"},
                "wait": {"type": "number", "description": "Seconds to poll for a match (default 2)"},
                "browser": {"type": "string", "enum": ["edge", "chrome"]},
            },
        },
    ),
    types.Tool(
        name="ahk_browser_tree",
        description="Dump a Chromium tab's accessibility tree (roles, names, values, states, "
        "#refs) with layout wrappers collapsed. The structured way to read a page.",
        inputSchema={
            "type": "object",
            "properties": {
                "tab": {"type": "string", "description": "Tab index or title/URL text (default: the visible tab)"},
                "max_depth": {"type": "integer", "description": "Max depth of shown nodes (default 8)"},
                "max_nodes": {"type": "integer", "description": "Max nodes (default 200)"},
                "browser": {"type": "string", "enum": ["edge", "chrome"]},
            },
        },
    ),
    types.Tool(
        name="ahk_browser_click",
        description="Click an element in a Chromium tab in the background: scrolls it into view "
        "and sends renderer-level mouse events. No cursor, no focus change, works with the "
        "window covered. Target by ref (from find/tree), name/role/index, or viewport x,y. "
        "Returns the page title/URL afterward.",
        inputSchema={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref like #123 from ahk_browser_find/tree"},
                "name": {"type": "string", "description": "Accessible name text (all words, any order)"},
                "role": {"type": "string", "description": "Role filter (partial match)"},
                "index": {"type": "integer", "description": "Which match (0-based, default 0)"},
                "x": {"type": "number", "description": "Viewport X in CSS pixels (instead of an element)"},
                "y": {"type": "number", "description": "Viewport Y in CSS pixels"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "count": {"type": "integer", "description": "2 for double-click"},
                "tab": {"type": "string", "description": "Tab index or title/URL text (default: the visible tab)"},
                "browser": {"type": "string", "enum": ["edge", "chrome"]},
            },
        },
    ),
    types.Tool(
        name="ahk_browser_type",
        description="Type into a Chromium tab in the background: optionally focus an element "
        "(ref or name/role), optionally clear it, insert text, then press keys. Works with "
        "inputs, React-controlled fields and contenteditable editors. No focus change.",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to insert"},
                "keys": {"type": "string", "description": "Keys after the text, e.g. 'Enter', 'Tab', 'Ctrl+A', 'Shift+Tab'; space-separated sequence"},
                "ref": {"type": "string", "description": "Element to focus first, like #123"},
                "name": {"type": "string", "description": "Element to focus first, by accessible name"},
                "role": {"type": "string", "description": "Role filter for name (e.g. textbox)"},
                "index": {"type": "integer", "description": "Which match (0-based, default 0)"},
                "clear": {"type": "boolean", "description": "Select all and delete before typing"},
                "tab": {"type": "string", "description": "Tab index or title/URL text (default: the visible tab)"},
                "browser": {"type": "string", "enum": ["edge", "chrome"]},
            },
        },
    ),
    types.Tool(
        name="ahk_browser_navigate",
        description="Load a URL in a Chromium tab (or a new background tab) and wait for it to "
        "finish loading. Returns the resulting title and URL.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "new_tab": {"type": "boolean", "description": "Open in a new background tab"},
                "tab": {"type": "string", "description": "Tab index or title/URL text (default: the visible tab)"},
                "browser": {"type": "string", "enum": ["edge", "chrome"]},
            },
            "required": ["url"],
        },
    ),
    types.Tool(
        name="ahk_browser_eval",
        description="Evaluate JavaScript in a Chromium tab and return the result (JSON). Promises "
        "are awaited. The escape hatch for page state the accessibility tree doesn't show.",
        inputSchema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "JS expression"},
                "tab": {"type": "string", "description": "Tab index or title/URL text (default: the visible tab)"},
                "timeout": {"type": "integer", "description": "Seconds (default 30)"},
                "browser": {"type": "string", "enum": ["edge", "chrome"]},
            },
            "required": ["script"],
        },
    ),
]


def dispatch(name: str, arguments: dict, report: bool = True) -> str:
    """Run one tool synchronously (called from a worker thread). Action tools
    get a what-changed line unless `report` is off (batches report once)."""
    if not report or name not in ACTION_SETTLE:
        return _run_tool(name, arguments)
    before = _snapshot()
    result = _run_tool(name, arguments)
    if result.startswith(ERROR_PREFIXES):
        return result
    return result + _changes(before, ACTION_SETTLE[name], quiet=name.startswith("ahk_browser_"))


def _click_ref(arguments: dict) -> str:
    """Real-cursor click on the centre of a ref'd element, its window in front."""
    try:
        ctl, desc, hwnd = _resolve_ref(arguments["ref"])
    except LookupError as e:
        return f"[Error] {e}"
    r = ctl.BoundingRectangle
    if r.right <= r.left:
        return f"[Error] {arguments['ref']} has no on-screen position (offscreen?)\n{desc}"
    # UIA rects are screen coordinates; ahk_click's x,y are window-relative.
    script = 'CoordMode "Mouse", "Screen"\n' + SCRIPT_CLICK.format(
        x=(r.left + r.right) // 2, y=(r.top + r.bottom) // 2,
        button=arguments.get("button", "left"), win=f"ahk_id {hwnd}")
    return run_ahk(script) + f"\nElement: {desc}"


def _run_tool(name: str, arguments: dict) -> str:
    result = ""

    if name == "ahk_windows":
        raw = run_ahk(SCRIPT_WINDOWS)
        if not raw:
            result = "No visible windows found."
        else:
            lines = []
            for line in raw.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 5:
                    lines.append(f"{parts[1]}  [{parts[2]}]  PID:{parts[3]}  @{parts[4]}")
                elif len(parts) >= 2:
                    lines.append(parts[1])
            result = "\n".join(lines)

    elif name == "ahk_focus":
        target = escape_ahk(ahk_win(arguments["target"]))
        result = run_ahk(SCRIPT_FOCUS.format(target=target))

    elif name == "ahk_send":
        keys = escape_ahk(arguments["keys"])
        result = run_ahk(SCRIPT_SEND.format(keys=keys, win=ahk_win(arguments.get("target", ""))))

    elif name == "ahk_type":
        text = arguments["text"]
        mode = arguments.get("mode", "auto")
        paste = mode == "paste" or (mode == "auto" and len(text) > PASTE_OVER)
        template = SCRIPT_PASTE_TEXT if paste else SCRIPT_SEND_TEXT
        result = run_ahk(template.format(text=escape_ahk(text), win=ahk_win(arguments.get("target", ""))),
                         timeout=AHK_TIMEOUT + (0 if paste else len(text) // 50))

    elif name == "ahk_click" and arguments.get("ref"):
        result = _click_ref(arguments)

    elif name == "ahk_click":
        x = arguments["x"]
        y = arguments["y"]
        button = arguments.get("button", "left")
        result = run_ahk(SCRIPT_CLICK.format(x=x, y=y, button=button,
                                             win=ahk_win(arguments.get("target", ""))))

    elif name == "ahk_read":
        target = arguments.get("target", "")
        if target:
            result = run_ahk(SCRIPT_READ_TARGET.format(target=escape_ahk(target)))
        else:
            result = run_ahk(SCRIPT_READ)

    elif name == "ahk_window_info":
        result = run_ahk(SCRIPT_WINDOW_INFO.format(win=ahk_win(arguments.get("target", ""))))

    elif name == "ahk_clipboard":
        action = arguments["action"]
        if action == "get":
            result = run_ahk(SCRIPT_CLIPBOARD_GET)
            if not result:
                result = "(clipboard is empty)"
        elif action == "set":
            content = escape_ahk(arguments.get("content", ""))
            result = run_ahk(SCRIPT_CLIPBOARD_SET.format(content=content))

    elif name == "ahk_run":
        target = escape_ahk(arguments["target"])
        result = run_ahk(SCRIPT_RUN.format(target=target))

    elif name == "ahk_msgbox":
        message = escape_ahk(arguments["message"])
        title = escape_ahk(arguments.get("title", "AHK-MCP"))
        result = run_ahk(SCRIPT_MSGBOX.format(message=message, title=title), timeout=30)

    elif name == "ahk_screenshot":
        target = arguments.get("target", "")
        rx = arguments.get("x", 0)
        ry = arguments.get("y", 0)
        rw = arguments.get("w", 0)
        rh = arguments.get("h", 0)
        ts = int(time.time() * 1000)
        filepath = str(TEMP_DIR / f"screenshot_{ts}.png").replace("/", "\\")
        result = run_ahk(SCRIPT_SCREENSHOT.format(
            target=escape_ahk(target), filepath=filepath,
            rx=rx, ry=ry, rw=rw, rh=rh,
        ))

    elif name == "ahk_uia_tree":
        target = arguments.get("target", "")
        max_depth = arguments.get("max_depth", 8)
        max_nodes = arguments.get("max_nodes", 200)
        scope = arguments.get("scope", "page")
        try:
            result = uia_tree(target, max_depth, max_nodes, scope)
        except Exception as e:
            result = f"[UIA Error] {e}"

    elif name == "ahk_uia_find":
        target = arguments.get("target", "")
        uia_name = arguments.get("name", "")
        control_type = arguments.get("control_type", "")
        max_results = arguments.get("max_results", 30)
        scope = arguments.get("scope", "page")
        wait = arguments.get("wait", 2.0)
        try:
            result = uia_find(target, uia_name, control_type, max_results, scope, wait)
        except Exception as e:
            result = f"[UIA Error] {e}"

    elif name == "ahk_uia_url":
        try:
            result = uia_url()
        except Exception as e:
            result = f"[UIA Error] {e}"

    elif name == "ahk_uia_invoke":
        result = uia_invoke(
            arguments.get("target", ""), arguments.get("name", ""),
            arguments.get("control_type", ""), arguments.get("index", 0),
            arguments.get("action", "auto"), arguments.get("scope", "page"),
            arguments.get("wait", 2.0), arguments.get("keep_focus", False),
            arguments.get("ref", ""),
        )

    elif name == "ahk_uia_set_value":
        result = uia_set_value(
            arguments.get("target", ""), arguments.get("name", ""),
            arguments.get("control_type", "Edit"), arguments["value"],
            arguments.get("index", 0), arguments.get("scope", "page"),
            arguments.get("wait", 2.0), arguments.get("keep_focus", False),
            arguments.get("ref", ""),
        )

    elif name == "ahk_control_send":
        raw = arguments.get("raw", False)
        result = run_ahk(SCRIPT_CONTROL_SEND.format(
            win=ahk_win(arguments["target"]), control=escape_ahk(arguments.get("control", "")),
            keys=escape_ahk(arguments.get("keys") or arguments.get("text", "")),
            send_cmd="ControlSend" if raw else "ControlSendText",
            mode="keys" if raw else "text",
        ))

    elif name == "ahk_control_click":
        result = run_ahk(SCRIPT_CONTROL_CLICK.format(
            win=ahk_win(arguments["target"]), control=escape_ahk(arguments.get("control", "")),
            x=int(arguments.get("x", 0)), y=int(arguments.get("y", 0)),
            button=arguments.get("button", "left"), count=int(arguments.get("count", 1)),
        ))

    elif name == "ahk_wait":
        result = wait_for(arguments)

    elif name == "ahk_batch":
        result = batch(arguments)

    elif name.startswith("ahk_browser_"):
        result = browser_dispatch(name, arguments)

    elif name == "ahk_eval":
        script = arguments["script"]
        timeout = arguments.get("timeout", AHK_TIMEOUT)
        result = run_ahk(script, timeout=timeout)

    else:
        result = f"Unknown tool: {name}"

    return result or "(no output)"


def _dispatch_in_thread(name: str, arguments: dict) -> str:
    # uiautomation is COM-based; each worker thread needs its own COM init.
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        return dispatch(name, arguments)


ERROR_PREFIXES = ("[Error]", "[AHK Error]", "[UIA Error]", "Unknown tool:")


async def list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    # The 2.x lowlevel server doesn't validate arguments against inputSchema,
    # so a missing required argument surfaces here as a KeyError.
    try:
        result = await anyio.to_thread.run_sync(
            _dispatch_in_thread, params.name, params.arguments or {}
        )
    except KeyError as e:
        result = f"[Error] Missing required argument: {e.args[0]}"
    except Exception as e:
        result = f"[Error] {type(e).__name__}: {e}"
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result)],
        is_error=result.startswith(ERROR_PREFIXES),
    )


server = Server("ahk-mcp", on_list_tools=list_tools, on_call_tool=call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
