"""
AHK-MCP: AutoHotkey v2 computer-use tools via MCP.

Cursed but token-efficient alternative to screenshot-based computer use.
Each tool generates a temporary AHK v2 script, executes it, and returns
structured text results instead of images.

Token cost per action: ~200-700 tokens (vs ~2000-3500 for screenshot-based).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import mcp.types as types
from mcp.server.lowlevel.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server

# --- Configuration ---

AHK_EXE = os.environ.get(
    "AHK_EXE",
    str(Path.home() / "AppData/Local/Programs/AutoHotkey/v2/AutoHotkey64.exe"),
)
AHK_TIMEOUT = int(os.environ.get("AHK_TIMEOUT", "10"))
TEMP_DIR = Path(tempfile.gettempdir()) / "ahk-mcp"
TEMP_DIR.mkdir(exist_ok=True)


def run_ahk(script: str, timeout: int | None = None) -> str:
    """Write and execute an AHK v2 script, return its stdout."""
    timeout = timeout or AHK_TIMEOUT
    script_path = TEMP_DIR / f"cmd_{int(time.time()*1000)}.ahk"
    script_path.write_text(script, encoding="utf-8")
    try:
        result = subprocess.run(
            [AHK_EXE, "/CP65001", str(script_path)],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            output += f"\n[AHK Error] {result.stderr.strip()}"
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

SCRIPT_SEND = r"""
#Requires AutoHotkey v2.0
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

SCRIPT_SEND_TEXT = r"""
#Requires AutoHotkey v2.0
text := "{text}"
try {{
    title := WinGetTitle("A")
    exe := WinGetProcessName("A")
    SendText text
    FileAppend "Target: " . title . " [" . exe . "]`nSent text: " . StrLen(text) . " chars", "*"
}} catch as e {{
    FileAppend "[Error] " . e.Message, "*"
}}
ExitApp
"""

SCRIPT_CLICK = r"""
#Requires AutoHotkey v2.0
x := {x}
y := {y}
button := "{button}"
try {{
    ; Report what's under the cursor before clicking
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
try {{
    title := WinGetTitle("A")
    cls := WinGetClass("A")
    pid := WinGetPID("A")
    exe := WinGetProcessName("A")
    WinGetPos(&x, &y, &w, &h, "A")
    out := "Title: " . title
    out .= "`nClass: " . cls
    out .= "`nPID: " . pid
    out .= "`nProcess: " . exe
    out .= "`nPosition: " . x . "," . y . " Size: " . w . "x" . h

    ; List controls
    try {{
        controls := WinGetControls("A")
        out .= "`n`nControls (" . controls.Length . "):"
        limit := Min(controls.Length, 50)
        loop limit {{
            ctl := controls[A_Index]
            try {{
                ctlText := ControlGetText(ctl, "A")
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

def _uia_control_type_name(type_id: int) -> str:
    """Map UIA ControlTypeId to readable name."""
    names = {
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
    }
    return names.get(type_id, f"Unknown({type_id})")


def uia_tree(target: str = "", max_depth: int = 4, max_nodes: int = 200) -> str:
    """Dump the UI Automation tree for a window."""
    import uiautomation as auto

    if target:
        win = auto.WindowControl(searchDepth=1, Name=target, searchInterval=0.5)
        if not win.Exists(1):
            # Try partial match
            win = auto.WindowControl(searchDepth=1, SubName=target, searchInterval=0.5)
        if not win.Exists(1):
            return f"[Error] Window not found: {target}"
    else:
        win = auto.GetForegroundControl()

    lines = [f"Window: {win.Name}"]
    count = [0]

    def walk(element, depth):
        if depth > max_depth or count[0] >= max_nodes:
            return
        try:
            children = element.GetChildren()
        except Exception:
            return
        for child in children:
            if count[0] >= max_nodes:
                lines.append(f"{'  ' * depth}... (truncated at {max_nodes} nodes)")
                return
            count[0] += 1
            indent = "  " * depth
            ctype = _uia_control_type_name(child.ControlTypeName and 0 or 0)
            name = child.Name or ""
            ctype_name = child.ControlTypeName or "?"
            value = ""
            try:
                vp = child.GetValuePattern()
                if vp:
                    value = f" = {vp.Value[:100]}" if vp.Value else ""
            except Exception:
                pass

            # Truncate long names
            display_name = name[:80] + "..." if len(name) > 80 else name
            line = f"{indent}{ctype_name}"
            if display_name:
                line += f': "{display_name}"'
            if value:
                line += value
            lines.append(line)

            walk(child, depth + 1)

    walk(win, 1)
    if count[0] >= max_nodes:
        lines.append(f"\n({count[0]} nodes shown, tree may be larger)")
    return "\n".join(lines)


def uia_find(target: str = "", name: str = "", control_type: str = "", max_results: int = 30) -> str:
    """Find UI elements by name and/or control type."""
    import uiautomation as auto

    if target:
        win = auto.WindowControl(searchDepth=1, SubName=target, searchInterval=0.5)
        if not win.Exists(1):
            return f"[Error] Window not found: {target}"
    else:
        win = auto.GetForegroundControl()

    results = []

    def walk(element, depth):
        if len(results) >= max_results:
            return
        if depth > 8:
            return
        try:
            children = element.GetChildren()
        except Exception:
            return
        for child in children:
            if len(results) >= max_results:
                return
            cname = child.Name or ""
            ctype = child.ControlTypeName or ""

            match = True
            if name and name.lower() not in cname.lower():
                match = False
            if control_type and control_type.lower() not in ctype.lower():
                match = False
            if not name and not control_type:
                match = False

            if match:
                rect = child.BoundingRectangle
                pos = f"@{rect.left},{rect.top},{rect.width()}x{rect.height()}" if rect.width() > 0 else ""
                value = ""
                try:
                    vp = child.GetValuePattern()
                    if vp and vp.Value:
                        value = f" = {vp.Value[:100]}"
                except Exception:
                    pass
                display_name = cname[:80] + "..." if len(cname) > 80 else cname
                results.append(f'{ctype}: "{display_name}" {pos}{value}')

            walk(child, depth + 1)

    walk(win, 0)
    if not results:
        return f"No elements found matching name='{name}' type='{control_type}'"
    header = f"Found {len(results)} element(s) in: {win.Name}\n"
    return header + "\n".join(results)


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


# --- MCP Server ---

server = Server("ahk-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
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
            description="Send keystrokes to the active window. Uses AHK Send syntax: "
            "{Enter}, {Tab}, ^c (Ctrl+C), !{F4} (Alt+F4), +{Home} (Shift+Home), etc. "
            "For plain text, use ahk_type instead.",
            inputSchema={
                "type": "object",
                "properties": {
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
            description="Type plain text into the active window (SendText — no special key interpretation).",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to type",
                    },
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="ahk_click",
            description="Click at screen coordinates. Use ahk_window_info to find control positions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate"},
                    "y": {"type": "integer", "description": "Y coordinate"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button (default: left)",
                    },
                },
                "required": ["x", "y"],
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
            description="Get detailed info about the active window: title, class, PID, "
            "process name, position, size, and list of UI controls with their text.",
            inputSchema={"type": "object", "properties": {}},
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
            "form fields, and UI state that WinGetText can't see.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Window title (omit for active window)"},
                    "max_depth": {"type": "integer", "description": "Max tree depth (default: 4)"},
                    "max_nodes": {"type": "integer", "description": "Max nodes to return (default: 200)"},
                },
            },
        ),
        types.Tool(
            name="ahk_uia_find",
            description="Search for UI elements by name and/or control type within a window. "
            "Returns matching elements with position and value. Useful for finding buttons, "
            "links, inputs, or any named element.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Window title (omit for active window)"},
                    "name": {"type": "string", "description": "Element name to search for (partial match)"},
                    "control_type": {"type": "string", "description": "Control type: Button, Edit, Hyperlink, Text, etc."},
                    "max_results": {"type": "integer", "description": "Max results (default: 30)"},
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
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
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
        target = escape_ahk(arguments["target"])
        result = run_ahk(SCRIPT_FOCUS.format(target=target))

    elif name == "ahk_send":
        keys = escape_ahk(arguments["keys"])
        result = run_ahk(SCRIPT_SEND.format(keys=keys))

    elif name == "ahk_type":
        text = escape_ahk(arguments["text"])
        result = run_ahk(SCRIPT_SEND_TEXT.format(text=text))

    elif name == "ahk_click":
        x = arguments["x"]
        y = arguments["y"]
        button = arguments.get("button", "left")
        result = run_ahk(SCRIPT_CLICK.format(x=x, y=y, button=button))

    elif name == "ahk_read":
        target = arguments.get("target", "")
        if target:
            result = run_ahk(SCRIPT_READ_TARGET.format(target=escape_ahk(target)))
        else:
            result = run_ahk(SCRIPT_READ)

    elif name == "ahk_window_info":
        result = run_ahk(SCRIPT_WINDOW_INFO)

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
        max_depth = arguments.get("max_depth", 4)
        max_nodes = arguments.get("max_nodes", 200)
        try:
            result = uia_tree(target, max_depth, max_nodes)
        except Exception as e:
            result = f"[UIA Error] {e}"

    elif name == "ahk_uia_find":
        target = arguments.get("target", "")
        uia_name = arguments.get("name", "")
        control_type = arguments.get("control_type", "")
        max_results = arguments.get("max_results", 30)
        try:
            result = uia_find(target, uia_name, control_type, max_results)
        except Exception as e:
            result = f"[UIA Error] {e}"

    elif name == "ahk_uia_url":
        try:
            result = uia_url()
        except Exception as e:
            result = f"[UIA Error] {e}"

    elif name == "ahk_eval":
        script = arguments["script"]
        timeout = arguments.get("timeout", AHK_TIMEOUT)
        result = run_ahk(script, timeout=timeout)

    else:
        result = f"Unknown tool: {name}"

    return [types.TextContent(type="text", text=result or "(no output)")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions()
            ),
        )


if __name__ == "__main__":
    import anyio
    anyio.run(main)
