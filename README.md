# ahk-mcp

An MCP server that gives AI agents hands on Windows — via AutoHotkey v2 and the Windows UI Automation accessibility tree.

This is the "cursed but effective" alternative to screenshot-based computer use. Instead of sending 1280x720 PNGs back and forth (burning 2000-3500 tokens per action on image encoding alone), ahk-mcp returns **structured text**: window titles, control lists, accessibility tree nodes, and action confirmations. Typical cost: **200-700 tokens per action**.

The thesis is simple: the Windows accessibility tree already contains a machine-readable description of everything on screen. Screenshots throw that away and make the model re-derive it from pixels. Why?

## How it works

ahk-mcp exposes 15 tools over MCP's stdio transport:

- **Observation tools** read the accessibility tree, window text, and UI element properties
- **Action tools** send keystrokes, click coordinates, manage clipboard, launch programs
- **`ahk_eval`** is the escape hatch — execute arbitrary AHK v2 code for anything the built-in tools don't cover

Every action tool reports context before and after execution (which window was focused, what changed). This "guardrails pattern" means the agent always knows what it just did and what state the system is in, without needing a follow-up screenshot.

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
| `ahk_window_info` | Detailed info on the active window: title, class, PID, process, position, and a list of all UI controls with their text. |
| `ahk_read` | Read all text content from a window (active or by title). Cheap and fast. |
| `ahk_uia_tree` | Dump the UI Automation accessibility tree. This is how you read browser content, form fields, and UI state that WinGetText can't see. Configurable depth and node limit. |
| `ahk_uia_find` | Search for UI elements by name and/or control type. Returns matches with position and value. Find that "Submit" button without scanning the whole tree. |
| `ahk_uia_url` | Get the current URL from the active browser's address bar (Firefox, Chrome, Edge). |
| `ahk_screenshot` | Capture a screenshot of a window, region, or full screen. Returns a PNG file path. Uses PrintWindow for per-window capture (works even when the window is occluded). The fallback when you genuinely need pixels. |

### Action

| Tool | Description |
|---|---|
| `ahk_focus` | Activate/focus a window by title (partial match). |
| `ahk_send` | Send keystrokes using AHK syntax: `{Enter}`, `^c` (Ctrl+C), `!{F4}` (Alt+F4), `+{Home}` (Shift+Home), etc. |
| `ahk_type` | Type plain text via SendText (no special key interpretation). Use this for typing into fields. |
| `ahk_click` | Click at screen coordinates. Reports which window was active before and after. |
| `ahk_clipboard` | Get or set the system clipboard. |
| `ahk_run` | Launch a program, open a file, or navigate to a URL. |
| `ahk_msgbox` | Show a message box to the user (notifications, confirmations). |

### Escape hatch

| Tool | Description |
|---|---|
| `ahk_eval` | Execute arbitrary AHK v2 code. Output via `FileAppend "text", "*"`, end with `ExitApp`. Full AHK v2 language: COM automation, DllCall, regex, file I/O, window manipulation — anything. |

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

## License

MIT
