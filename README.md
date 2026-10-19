# ahk-mcp

An MCP server that gives AI agents hands on Windows — via AutoHotkey v2 and the Windows UI Automation accessibility tree.

This is the "cursed but effective" alternative to screenshot-based computer use. Instead of sending 1280x720 PNGs back and forth (burning 2000-3500 tokens per action on image encoding alone), ahk-mcp returns **structured text**: window titles, control lists, accessibility tree nodes, and action confirmations. Typical cost: **200-700 tokens per action**.

The thesis is simple: the Windows accessibility tree already contains a machine-readable description of everything on screen. Screenshots throw that away and make the model re-derive it from pixels. Why?

## How it works

ahk-mcp exposes 29 tools over MCP's stdio transport:

- **Observation tools** read the accessibility tree, window text, and UI element properties
- **Action tools** send keystrokes, click coordinates, manage clipboard, launch programs
- **Background action tools** act on windows *without* taking focus or moving the cursor (see [Background input](#background-input))
- **Browser tools** drive Edge/Chrome through the DevTools Protocol, fully in the background
- **Flow tools** wait for things and run several steps in one call (see [Fewer round trips](#fewer-round-trips))
- **`ahk_eval`** is the escape hatch — execute arbitrary AHK v2 code for anything the built-in tools don't cover

Every action tool ends with a `Changed:` line saying what the action did to the desktop: windows opened, closed or retitled, menus that popped up, what came to the front and what has focus there. For example, `Changed: opened "Save As"; front "Save As", focus Edit "File name:"`. This "guardrails pattern" means the agent always knows what it just did and what state the system is in, without needing a follow-up look. Windows belonging to the agent's own terminal are left out (Claude Code animates its title).

## Token cost comparison

| Approach | Tokens per action | What you get |
|---|---|---|
| Screenshot-based (1280x720 PNG) | ~2000-3500 | Pixels. Model must OCR, locate elements, interpret layout. |
| ahk-mcp (structured text) | ~200-700 | Window title, control names, accessibility tree nodes, action confirmation. |

The savings compound fast. A 20-step workflow that would cost ~50k tokens in screenshots costs ~8k tokens with ahk-mcp. More importantly, the structured output is *more reliable* — the model doesn't have to guess where the "Save" button is from pixels when the accessibility tree says `Button: "Save" @1043,672,88x32`.

## Installation

### Prerequisites

1. **Windows 10/11** (this is an AutoHotkey project — Windows is the point)
2. **Python 3.10+**
3. **AutoHotkey v2** — install via winget (the official distribution, avoids dodgy third-party repackages):
   ```
   winget install AutoHotkey.AutoHotkey
   ```
   This puts it at `%LOCALAPPDATA%\Programs\AutoHotkey\v2\AutoHotkey64.exe`. If you prefer a manual install, download only from [autohotkey.com](https://www.autohotkey.com/) — avoid other sources.

### Setup

```bash
# Clone the repo
git clone https://github.com/anomalous3/ahk-mcp.git
cd ahk-mcp

# Create a virtual environment (uv or plain venv)
uv venv .venv
# or: python -m venv .venv

# Activate
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

Requires `mcp` 2.x (the server uses the 2.x constructor-based lowlevel API).

### Verify AHK is found

The server looks for AutoHotkey at the default install path. If yours is elsewhere, set the `AHK_EXE` environment variable:

```bash
set AHK_EXE=C:\Path\To\AutoHotkey64.exe
```

## Claude Code MCP configuration

Add this to your `~/.claude.json` (or the project-level `.claude/settings.json`):

```json
{
  "mcpServers": {
    "ahk": {
      "command": "C:\\Users\\YOUR_USER\\ahk-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\YOUR_USER\\ahk-mcp\\server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

Replace `YOUR_USER` with your Windows username. The `PYTHONIOENCODING` env var prevents Windows cp1252 encoding crashes on Unicode output.

After adding the config, restart Claude Code. The tools will appear with the `mcp__ahk__` prefix.

## Tool reference

### Observation

| Tool | Description |
|---|---|
| `ahk_windows` | List all visible windows with title, class, PID, and position. The "what's on screen" overview. |
| `ahk_window_info` | Detailed info on a window (`target=`, default active): title, class, PID, process, position, and its Win32 controls (ClassNN names) with their text. |
| `ahk_read` | Read all text content from a window (active or by title). Cheap and fast. |
| `ahk_uia_tree` | Dump the UI Automation accessibility tree. In browsers it shows the active tab's page; unnamed layout wrappers are collapsed so depth goes to real content. |
| `ahk_uia_find` | Search for UI elements by name and/or control type at any depth. Matches carry position, value and a session ref (`e12`). Token matching (all words, any order); polls briefly for pages still rendering. |
| `ahk_uia_url` | Get the current URL from the active browser's address bar (Firefox, Chrome, Edge). |
| `ahk_screenshot` | Capture a screenshot of a window, region, or full screen. Returns a PNG file path. Uses PrintWindow for per-window capture (works even when the window is occluded). The fallback when you genuinely need pixels. |

### Action (foreground)

These use the real keyboard/cursor and need the target in front. Pass `target=` so the window is activated and verified **inside the same call**: tool calls are seconds apart, and the terminal running the agent reclaims focus in between, so a separate `ahk_focus` followed by `ahk_type` types into the terminal.

| Tool | Description |
|---|---|
| `ahk_focus` | Activate/focus a window by title. |
| `ahk_send` | Send keystrokes using AHK syntax: `{Enter}`, `^c` (Ctrl+C), `!{F4}` (Alt+F4), `+{Home}` (Shift+Home), etc. |
| `ahk_type` | Type plain text (no special key interpretation). Text over 80 chars is pasted, with the clipboard saved and restored; shorter text is typed key by key (see [Typing](#typing)). `mode=keys\|paste` forces one. |
| `ahk_click` | Click at coordinates, or on the centre of an element by `ref=e12`. Reports which window was active before and after. |
| `ahk_clipboard` | Get or set the system clipboard. |
| `ahk_run` | Launch a program, open a file, or navigate to a URL. |
| `ahk_msgbox` | Show a message box to the user (notifications, confirmations). |

### Background action

| Tool | Description |
|---|---|
| `ahk_uia_invoke` | Act on an element (by `ref=` or by search) through UI Automation: Invoke / Toggle / Select / Expand / default action. No coordinates; works occluded and scrolled out of view. Focus guarded. |
| `ahk_uia_set_value` | Set an input's text (by `ref=` or by search) through UI Automation. Fires real input/change events. Focus guarded. |
| `ahk_control_send` | Text/keys to a background window via posted messages (AHK ControlSend). Classic Win32 only. |
| `ahk_control_click` | Click a control or client point via posted messages (AHK ControlClick NA). Classic Win32 only. |

### Browser (Edge/Chrome, via DevTools Protocol)

| Tool | Description |
|---|---|
| `ahk_browser_launch` | Start the browser (your normal profile) with background-friendly flags; reports whether remote debugging is enabled. `restart=true` relaunches a running browser, restoring tabs. |
| `ahk_browser_tabs` | List tabs as `[index] title \| url`; pass index or title/URL text as `tab=` elsewhere. |
| `ahk_browser_find` | Search a tab's accessibility tree by name/role; matches carry a `#ref`. |
| `ahk_browser_tree` | Dump a tab's accessibility tree with wrappers collapsed. |
| `ahk_browser_click` | Click by `#ref`, name/role, or viewport x,y. Renderer-level events: no cursor, no focus change. |
| `ahk_browser_type` | Focus (optional), clear (optional), insert text, press keys (`Enter`, `Ctrl+A`, `Shift+Tab`). Works on React-controlled inputs and contenteditable editors. |
| `ahk_browser_navigate` | Load a URL in a tab or a new background tab; waits for load. |
| `ahk_browser_eval` | Evaluate JavaScript in a tab and return the result. |

### Flow

| Tool | Description |
|---|---|
| `ahk_wait` | Wait until a window, UI element or Chromium page element appears (or with `gone=true`, disappears), or a Chromium page has loaded with a given URL/title. Returns as soon as it holds, with the element's ref. |
| `ahk_batch` | Run a list of steps (any tool, plus `sleep`) in one call. Stops at the first failure; one `Changed:` line at the end. |

### Escape hatch

| Tool | Description |
|---|---|
| `ahk_eval` | Execute arbitrary AHK v2 code. Output via `FileAppend "text", "*"`, end with `ExitApp`. Full AHK v2 language: COM automation, DllCall, regex, file I/O, window manipulation — anything. Script errors come back as text (`[AHK Error] ... (line N)`), never as a dialog on the user's screen. |

## Fewer round trips

Every tool call is a model turn, and for voice control each turn is seconds of silence. Three things cut the count:

- **`ahk_batch`** runs a whole sequence in one call. Opening Notepad and typing into it is one call, not three:
  ```json
  {"steps": [
    {"tool": "run", "target": "notepad.exe"},
    {"tool": "wait", "until": "window", "target": "Notepad"},
    {"tool": "type", "target": "Notepad", "text": "hello"}
  ]}
  ```
  Step tools take the same arguments as the tools themselves; the `ahk_` prefix is optional.
- **`ahk_wait`** replaces guessed sleeps and repeated looks. It returns the moment the condition holds.
- **Refs.** `ahk_uia_find`, `ahk_uia_tree` (interactive elements) and `ahk_wait` label elements `e1`, `e2`, ... for the whole session, and the same element keeps its ref across searches. `ahk_uia_invoke`, `ahk_uia_set_value` and `ahk_click` take `ref=`, so acting on something already found needs no second search. A ref works for as long as the app keeps the element; once the app discards it, using the ref says it's stale. Browser tools have their own refs (`#123`).

## Typing

At full speed, some apps garble synthetic input. Win11 Notepad, given AHK's default `SendText`, keeps the first word and then repeats a single character for the rest; it also loses Shift states (`100%` arrives as `1005`). So keys are sent one at a time (AHK Event mode, minimal delay). That's reliable, but limited by the system timer to about 60 characters per second, so `ahk_type` pastes anything over 80 characters instead, saving the clipboard first and restoring it afterwards. Pass `mode=keys` where Ctrl+V means something else (terminals), or `mode=paste` to always paste. With clipboard history (Win+V) turned on, pasted text appears in it.

## Background input

The goal: act on a window while the user (or the agent's own terminal) keeps focus. What Windows actually allows, measured on Windows 11 with a test page and a classic Win32 window, both covered by another window:

| | Read tree / find | UIA actions (`ahk_uia_invoke`, `ahk_uia_set_value`) | Posted messages (`ahk_control_send`, `ahk_control_click`) | DevTools (`ahk_browser_*`) |
|---|---|---|---|---|
| Classic Win32 (dialogs, older apps) | ✅ | ✅ works, **raises the window** | ✅ **true background** | — |
| Edge / Chrome | ✅ (launch flags) | ✅ works, **raises the window** | ❌ ignored | ✅ **true background** |
| Firefox | ✅ (occlusion pref) | ✅ works, **raises the window** | ❌ ignored | — |
| WinUI / UWP (Win11 Notepad, Settings) | ✅ | ? | ❌ ignored | — |

- **UIA actions raise the target.** Apps bring their window forward when an action focuses an element, and UIA grants them the foreground right to do it (so `LockSetForegroundWindow` doesn't help). `ahk_uia_invoke` / `ahk_uia_set_value` run a **focus guard** that puts the previous foreground window straight back: a brief flash, and a short window in which keystrokes could land in the target. Pass `keep_focus=true` to skip it.
- **Posted window messages** only reach classic Win32 controls. Modern UI frameworks read input from the raw input stack, not the message queue.
- **Chromium via DevTools** is the one path to true background input in a browser: mouse and key events go straight to the renderer. Since most browsing happens in Chromium, that covers the common case.

## Browser automation

### Edge / Chrome (recommended): DevTools Protocol

One-time setup:

1. Start the browser through `ahk_browser_launch` (or `ahk_browser_launch restart=true` if it's already running). This adds `--force-renderer-accessibility --disable-features=CalculateNativeWinOcclusion --disable-backgrounding-occluded-windows`, which keep page content alive and readable while the window is covered — Chromium otherwise treats a fully occluded window as hidden and empties its accessibility tree.
2. Open `edge://inspect/#remote-debugging` (`chrome://inspect/#remote-debugging`) and tick **Allow remote debugging for this browser instance**. It persists across restarts. This replaces `--remote-debugging-port`, which Chromium ignores for your normal profile since ~v136. **Don't pass `--remote-debugging-port` yourself**: it leaves the inspect-page server stuck on "starting…".
3. On the first `ahk_browser_*` call in a server session the browser asks you to approve the connection. The prompt is inside the browser window, so the server brings the browser forward for it and restores focus afterwards. The connection is kept open, so that's once per session.

The browser tools read Chromium's own accessibility tree (roles, names, values, states) and act on `#ref`s from it:

```
> ahk_browser_find name="focus races" role="link"

Tab: GitHub - anomalous3/macbot-mcp ...  | https://github.com/anomalous3/macbot-mcp
Found 4 element(s) (#N = ref):
[0] link: "Fix focus races, deepen ui_find, add mac_ui_press"  #2363
...

> ahk_browser_type name="Controlled input" role="textbox" clear=true text="hello" keys="Enter"
```

### Any browser: UI Automation

`ahk_uia_find` / `ahk_uia_tree` read any browser's accessibility tree through UIA, and `ahk_uia_invoke` / `ahk_uia_set_value` act on it (focus guarded). Two things that make this work on real pages:

- **Depth.** Page content sits 10–35 levels below the window root (the first link on a GitHub repo page is at depth 16). Searches use native `FindAll` with no depth cap, and tree dumps collapse unnamed wrappers.
- **Tabs.** Firefox exposes *every* open tab's document at once. Both tools default to `scope="page"`: the active tab's document. `scope="window"` searches everything, including browser chrome.

**Firefox** needs `widget.windows.window_occlusion_tracking.enabled = false` in `about:config` to stay readable while covered; with it on, a fully covered Firefox window reports everything offscreen with no positions. **Edge/Chrome** need the launch flags above.

Window titles are matched case-insensitively, word order free, and ignoring the zero-width space Edge puts in "Microsoft​ Edge".

## The guardrails pattern

Every action tool reports what happened:

```
Target: Untitled - Notepad [notepad.exe]
Sent: ^a
```

```
Before: Mozilla Firefox
Clicked: 500,300 left
After: Mozilla Firefox
```

This is deliberate. The agent always knows:
1. What window was active when the action fired
2. What the action was
3. What changed afterward

No guessing, no "did that click land?" ambiguity. If the active window changed unexpectedly, the agent sees it immediately in the response and can course-correct.

## The `ahk_eval` philosophy

The built-in tools cover ~80% of common tasks. For the other 20%, `ahk_eval` gives the agent full access to AutoHotkey v2 — which is a surprisingly capable automation language with COM object support, DllCall for the entire Win32 API, regex, file I/O, and window manipulation primitives.

In practice, agents learn to use `ahk_eval` for things like:
- Multi-step sequences (select all, copy, process clipboard, paste result)
- COM automation (Excel, Outlook, Word via their COM interfaces)
- Fine-grained window manipulation (resize, move, set transparency)
- Anything the built-in tools don't cover

The convention: output results with `FileAppend "text", "*"` (writes to stdout) and end scripts with `ExitApp`.

## Platform notes

ahk-mcp is Windows-only because AutoHotkey is Windows-only. That said, the *approach* is portable:

- **Linux**: AT-SPI2 provides an equivalent accessibility tree. The `python-atspi` package or `busctl` can read it. Keyboard/mouse synthesis via `xdotool` or `ydotool` (Wayland).
- **macOS**: The Accessibility API exposes the same tree. `pyobjc` can read it, and `cliclick` or AppleScript can drive input.

The core insight — that structured accessibility data is cheaper and more reliable than screenshots for most automation tasks — applies everywhere. The AHK-specific parts are just the Windows implementation.

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `AHK_EXE` | `%LOCALAPPDATA%\Programs\AutoHotkey\v2\AutoHotkey64.exe` | Path to AutoHotkey v2 executable |
| `AHK_TIMEOUT` | `10` | Default script execution timeout in seconds |
| `AHK_BROWSER` | `edge` | Default browser for `ahk_browser_*` (`edge` or `chrome`) |

## License

MIT
