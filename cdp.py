"""
Background browser control for Chromium (Edge/Chrome) over the DevTools Protocol.

UIA actions make browsers raise their window, and posted window messages
don't reach them at all, so this is the one route to true background input:
CDP mouse/keyboard events go straight to the renderer, with no cursor
movement, no focus change, and they work on occluded windows.

Connection: since Chromium ~136, --remote-debugging-port is ignored for the
default profile. Instead the user ticks "Allow remote debugging for this
browser instance" at edge://inspect/#remote-debugging (chrome://inspect/...
for Chrome), which persists across restarts. The browser then writes
<User Data>/DevToolsActivePort with the port and the browser websocket path.
In this mode the HTTP /json endpoints return 404, so everything goes through
the browser websocket and the Target domain with flattened sessions.
"""

import ctypes
import itertools
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

from websockets.sync.client import connect

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
USER_DATA_DIRS = {
    "edge": LOCALAPPDATA / "Microsoft/Edge/User Data",
    "chrome": LOCALAPPDATA / "Google/Chrome/User Data",
}
ENABLE_URLS = {
    "edge": "edge://inspect/#remote-debugging",
    "chrome": "chrome://inspect/#remote-debugging",
}


class CDPError(Exception):
    pass


def _endpoint(browser: str) -> str:
    """Browser websocket URL from DevToolsActivePort, or raise with setup steps."""
    port_file = USER_DATA_DIRS[browser] / "DevToolsActivePort"
    try:
        port, path = port_file.read_text(encoding="utf-8").split()[:2]
    except (OSError, ValueError):
        raise CDPError(
            f"{browser} isn't accepting DevTools connections (no {port_file}). "
            f"Start {browser}, open {ENABLE_URLS[browser]} and tick 'Allow remote "
            "debugging for this browser instance' (a one-time setting), or use "
            "ahk_browser_launch."
        ) from None
    return f"ws://127.0.0.1:{port}{path}"


BROWSER_EXES = {"edge": "msedge.exe", "chrome": "chrome.exe"}
APPROVAL_TIMEOUT = 90


def _browser_window(browser: str, prefer: str = "") -> int:
    """Top-level window handle of a running browser, or 0. With `prefer`, a
    window whose title contains it wins (the approval prompt is its own window)."""
    user32 = ctypes.windll.user32
    pids = _pids(BROWSER_EXES[browser])
    found = []
    pid = wintypes.DWORD()
    buf = ctypes.create_unicode_buffer(256)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            user32.GetWindowTextW(hwnd, buf, 256)
            found.append((prefer.lower() not in buf.value.lower() if prefer else False, hwnd))
        return True

    user32.EnumWindows(cb, 0)
    # Stable sort: otherwise the topmost window, as before.
    return sorted(found, key=lambda f: f[0])[0][1] if found else 0


def _pids(exe: str) -> set[int]:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return {int(row.split('","')[1]) for row in out.splitlines() if row.count('","') >= 2}


def _set_foreground(hwnd: int):
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # A synthetic Alt press lifts the foreground lock for this call.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)


def _connect_with_approval(browser: str, url: str):
    """Open the websocket; if the browser holds it for user approval, surface
    the browser window so the prompt is visible, then put focus back."""
    result: dict = {}
    if not _browser_window(browser):
        # Running windowless (Edge's startup boost, or every window closed):
        # with nowhere to show the approval prompt, the browser refuses the
        # connection outright (HTTP 403). Open a window for it first.
        subprocess.Popen(["cmd", "/c", "start", "", BROWSER_EXES[browser].removesuffix(".exe")],
                         creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.time() + 10
        while not _browser_window(browser) and time.time() < deadline:
            time.sleep(0.3)

    def run():
        try:
            result["ws"] = connect(url, max_size=None, open_timeout=APPROVAL_TIMEOUT)
        except Exception as e:
            result["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(1.5)
    if t.is_alive():
        # The approval prompt lives inside the browser window and doesn't
        # raise itself, so a backgrounded browser just hangs the handshake.
        user32 = ctypes.windll.user32
        prev = user32.GetForegroundWindow()
        time.sleep(0.5)  # give the prompt window a moment to exist
        hwnd = _browser_window(browser, prefer="remote debugging")
        if hwnd:
            _set_foreground(hwnd)
        t.join(APPROVAL_TIMEOUT)
        if prev and user32.GetForegroundWindow() != prev:
            _set_foreground(prev)
    if "ws" not in result:
        err = result.get("err")
        if t.is_alive() or isinstance(err, TimeoutError):
            raise CDPError(f"{browser} didn't approve the DevTools connection within "
                           f"{APPROVAL_TIMEOUT}s. Accept the prompt in the {browser} window "
                           "and retry.")
        raise CDPError(f"Couldn't connect to {browser}: {err}")
    return result["ws"]


class Browser:
    """One persistent browser-level connection, shared by all tool calls.

    Kept open because the browser asks the user to approve each new client.
    """

    def __init__(self, name: str):
        self.name = name
        self.url = _endpoint(name)
        self.ws = _connect_with_approval(name, self.url)
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.sessions: dict[str, str] = {}  # targetId -> sessionId

    def send(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 30) -> dict:
        msg_id = next(self.ids)
        msg = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        with self.lock:
            self.ws.send(json.dumps(msg))
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise CDPError(f"{method} timed out")
                reply = json.loads(self.ws.recv(timeout=left))
                if reply.get("id") == msg_id:
                    break
                if reply.get("method") == "Target.detachedFromTarget":
                    self.sessions = {t: s for t, s in self.sessions.items()
                                     if s != reply["params"]["sessionId"]}
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def pages(self) -> list[dict]:
        infos = self.send("Target.getTargets")["targetInfos"]
        return [t for t in infos if t["type"] == "page"]

    def session(self, target_id: str) -> str:
        if target_id not in self.sessions:
            sid = self.send("Target.attachToTarget",
                            {"targetId": target_id, "flatten": True})["sessionId"]
            self.sessions[target_id] = sid
            for domain in ("Page", "DOM", "Accessibility"):
                self.send(f"{domain}.enable", session=sid)
            # The page keeps behaving as focused while its window is in the
            # background (focus/blur handlers, :focus styles, caret).
            self.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, session=sid)
        return self.sessions[target_id]

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


_browsers: dict[str, Browser] = {}
_browsers_lock = threading.Lock()


def get_browser(name: str = "edge") -> Browser:
    """Shared connection, reconnecting if the browser restarted."""
    with _browsers_lock:
        b = _browsers.get(name)
        if b is not None:
            try:
                b.send("Browser.getVersion", timeout=5)
                return b
            except Exception:
                b.close()
        b = Browser(name)
        _browsers[name] = b
        return b


# --- Tabs ---

def _visible(b: Browser, target_id: str) -> bool:
    r = b.send("Runtime.evaluate", {"expression": "document.visibilityState",
                                    "returnByValue": True}, session=b.session(target_id))
    return r.get("result", {}).get("value") == "visible"


def resolve_tab(b: Browser, tab: str | int | None = None) -> dict:
    """A page target by list index, title/URL text, or default: a visible tab.

    getTargets lists the most recently active tab first, but a visible tab is
    the one actually on screen in its window, which is what 'current' means.
    """
    pages = b.pages()
    if not pages:
        raise CDPError("No open tabs")
    if tab is None or tab == "":
        for p in pages[:5]:
            if _visible(b, p["targetId"]):
                return p
        return pages[0]
    if isinstance(tab, int) or str(tab).isdigit():
        i = int(tab)
        if 0 <= i < len(pages):
            return pages[i]
        raise CDPError(f"No tab [{i}] ({len(pages)} open)")
    want = str(tab).casefold()
    for p in pages:
        if want in p["title"].casefold() or want in p["url"].casefold():
            return p
    raise CDPError(f"No tab matching {tab!r}")


def list_tabs(b: Browser) -> str:
    lines = [f"[{i}] {p['title'][:70]}  | {p['url'][:90]}" for i, p in enumerate(b.pages())]
    return "\n".join(lines) or "No open tabs"


# --- Accessibility tree ---

SKIP_ROLES = {"InlineTextBox", "LineBreak"}
WRAPPER_ROLES = {"generic", "none", "StaticText", "paragraph", "group", "section", "LabelText",
                 "Section", "div", "LayoutTable", "LayoutTableRow", "LayoutTableCell"}
INTERACTIVE_ROLES = {"button", "link", "textbox", "searchbox", "checkbox", "radio",
                     "combobox", "listbox", "option", "menuitem", "tab", "switch",
                     "slider", "spinbutton", "menuitemcheckbox", "menuitemradio",
                     "treeitem", "textField"}


def _prop(node: dict, key: str) -> str:
    v = (node.get(key) or {}).get("value", "")
    return v if isinstance(v, str) else str(v)


def _clip(s: str, n: int = 80) -> str:
    s = " ".join(s.split())
    return s[:n] + "..." if len(s) > n else s


def ax_nodes(b: Browser, sid: str) -> list[dict]:
    return b.send("Accessibility.getFullAXTree", session=sid, timeout=60)["nodes"]


def describe(node: dict) -> str:
    """One-line node summary: role: "name" = value [state]  #backendNodeId."""
    line = _prop(node, "role")
    name = _clip(_prop(node, "name"))
    if name:
        line += f': "{name}"'
    value = _prop(node, "value")
    if value and value != _prop(node, "name"):
        line += f" = {_clip(value, 100)}"
    for p in node.get("properties", []):
        if p["name"] in ("checked", "expanded", "selected", "disabled") and \
                p["value"].get("value") not in (False, "false", None):
            line += f" [{p['name']}={p['value'].get('value')}]"
    if "backendDOMNodeId" in node:
        line += f"  #{node['backendDOMNodeId']}"
    return line


def find(b: Browser, sid: str, name: str = "", role: str = "", max_results: int = 30,
         wait: float = 2.0) -> list[dict]:
    """Token match on name/value (all words, any order), substring match on role."""
    deadline = time.time() + wait
    while True:
        found = []
        for n in ax_nodes(b, sid):
            r = _prop(n, "role")
            if n.get("ignored") or r in SKIP_ROLES or "backendDOMNodeId" not in n:
                continue
            if role and role.lower() not in r.lower():
                continue
            if name:
                hay = f"{_prop(n, 'name')} {_prop(n, 'value')}".lower()
                if name.lower() not in hay and not all(t in hay for t in name.lower().split()):
                    continue
            found.append(n)
            if len(found) >= max_results:
                return found
        if found or time.time() >= deadline:
            return found
        time.sleep(0.3)


def tree(b: Browser, sid: str, max_depth: int = 8, max_nodes: int = 200) -> str:
    """AX tree dump with unnamed wrappers collapsed (they don't count toward depth)."""
    nodes = ax_nodes(b, sid)
    by_id = {n["nodeId"]: n for n in nodes}
    lines, count = [], 0

    def shown(n: dict, parent_label: set) -> bool:
        r, name = _prop(n, "role"), _prop(n, "name").strip()
        if n.get("ignored") or r in SKIP_ROLES:
            return False
        if r in INTERACTIVE_ROLES or _prop(n, "value"):
            return True
        if not name:
            return r not in WRAPPER_ROLES
        # Links, buttons and textboxes repeat their label/value as a text child.
        return not (r == "StaticText" and name in parent_label)

    def walk(n: dict, depth: int, parent_label: set):
        nonlocal count
        for cid in n.get("childIds", []):
            c = by_id.get(cid)
            if c is None or count >= max_nodes:
                continue
            if shown(c, parent_label):
                count += 1
                lines.append("  " * depth + describe(c))
                if depth < max_depth:
                    walk(c, depth + 1, {_prop(c, "name").strip(), _prop(c, "value").strip()})
            else:
                walk(c, depth, parent_label)

    if nodes:
        lines.append(describe(nodes[0]))
        walk(nodes[0], 1, set())
    if count >= max_nodes:
        lines.append(f"... (truncated at {max_nodes} nodes; use ahk_browser_find to search)")
    return "\n".join(lines)


# --- Input (renderer-level: no cursor, no focus change) ---

def _center(b: Browser, sid: str, backend_id: int) -> tuple[float, float]:
    try:
        b.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id}, session=sid)
    except CDPError:
        pass  # not every node supports it; the quads below still work if on screen
    quads = b.send("DOM.getContentQuads", {"backendNodeId": backend_id}, session=sid)["quads"]
    if not quads:
        raise CDPError("Element has no layout box (hidden or zero-size)")
    q = quads[0]
    return sum(q[0::2]) / 4, sum(q[1::2]) / 4


def click_at(b: Browser, sid: str, x: float, y: float, button: str = "left", count: int = 1):
    """Click at viewport CSS-pixel coordinates."""
    base = {"x": x, "y": y, "modifiers": 0}
    b.send("Input.dispatchMouseEvent", {**base, "type": "mouseMoved", "button": "none"}, session=sid)
    for i in range(1, count + 1):
        for kind in ("mousePressed", "mouseReleased"):
            b.send("Input.dispatchMouseEvent",
                   {**base, "type": kind, "button": button, "clickCount": i}, session=sid)


def click_node(b: Browser, sid: str, backend_id: int, button: str = "left",
               count: int = 1) -> tuple[float, float]:
    x, y = _center(b, sid, backend_id)
    click_at(b, sid, x, y, button, count)
    return x, y


def focus_node(b: Browser, sid: str, backend_id: int):
    b.send("DOM.focus", {"backendNodeId": backend_id}, session=sid)


def insert_text(b: Browser, sid: str, text: str):
    b.send("Input.insertText", {"text": text}, session=sid)


# key name -> (key, code, windowsVirtualKeyCode, text)
KEYS = {
    "enter": ("Enter", "Enter", 13, "\r"), "tab": ("Tab", "Tab", 9, ""),
    "escape": ("Escape", "Escape", 27, ""), "esc": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""), "delete": ("Delete", "Delete", 46, ""),
    "space": (" ", "Space", 32, " "),
    "up": ("ArrowUp", "ArrowUp", 38, ""), "down": ("ArrowDown", "ArrowDown", 40, ""),
    "left": ("ArrowLeft", "ArrowLeft", 37, ""), "right": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""), "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""), "pagedown": ("PageDown", "PageDown", 34, ""),
    **{f"f{i}": (f"F{i}", f"F{i}", 111 + i, "") for i in range(1, 13)},
}
MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "win": 4, "shift": 8}


def press_keys(b: Browser, sid: str, combo: str):
    """Press keys like 'Enter', 'Ctrl+A', 'Shift+Tab'; space-separate for a sequence."""
    for chord in combo.split():
        parts = chord.split("+")
        mods = 0
        for m in parts[:-1]:
            if m.lower() not in MODIFIERS:
                raise CDPError(f"Unknown modifier {m!r}")
            mods |= MODIFIERS[m.lower()]
        k = parts[-1]
        if k.lower() in KEYS:
            key, code, vk, text = KEYS[k.lower()]
        elif len(k) == 1:
            key, vk, text = k, ord(k.upper()), k
            code = f"Key{k.upper()}" if k.isalpha() else (f"Digit{k}" if k.isdigit() else "")
        else:
            raise CDPError(f"Unknown key {k!r}")
        if mods & ~8:
            text = ""  # Ctrl/Alt/Meta chords are commands, not text
        down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "modifiers": mods}
        if text:
            down["text"] = text
        if mods & 2 and k.isalpha() and len(k) == 1:
            # Editing shortcuts only fire through the browser's command layer.
            down["commands"] = {"a": ["selectAll"], "c": ["copy"], "x": ["cut"],
                                "v": ["paste"], "z": ["undo"], "y": ["redo"]}.get(k.lower(), [])
        b.send("Input.dispatchKeyEvent", down, session=sid)
        b.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code,
                                          "windowsVirtualKeyCode": vk, "modifiers": mods},
               session=sid)


# --- Page ---

def evaluate(b: Browser, sid: str, expression: str, timeout: float = 30):
    r = b.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                    "awaitPromise": True}, session=sid, timeout=timeout)
    if "exceptionDetails" in r:
        d = r["exceptionDetails"]
        raise CDPError(d.get("exception", {}).get("description") or d.get("text", "JS error"))
    return r.get("result", {}).get("value")


def page_state(b: Browser, sid: str) -> str:
    v = evaluate(b, sid, "[document.title, location.href, document.readyState]")
    return f"{v[0]}  | {v[1]}" + ("" if v[2] == "complete" else f"  ({v[2]})")


def navigate(b: Browser, sid: str, url: str, timeout: float = 20) -> str:
    r = b.send("Page.navigate", {"url": url}, session=sid)
    if r.get("errorText"):
        raise CDPError(f"Navigation failed: {r['errorText']}")
    deadline = time.time() + timeout
    time.sleep(0.2)
    while time.time() < deadline:
        try:
            if evaluate(b, sid, "document.readyState", timeout=5) == "complete":
                break
        except CDPError:
            pass  # execution context destroyed mid-navigation
        time.sleep(0.25)
    return page_state(b, sid)
