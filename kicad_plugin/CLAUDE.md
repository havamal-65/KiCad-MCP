# KiCad MCP — Bridge Development Guidelines

> These rules apply when modifying `kicad_mcp_bridge.py` or `install_bridge.ps1`.
> The root `CLAUDE.md` (MCP usage rules + PCB protocol) is always in effect alongside these.

## After Any Bridge Change

The installed bridge is a snapshot — source changes do not take effect until reinstalled:

```powershell
pwsh -ExecutionPolicy Bypass -File kicad_plugin\install_bridge.ps1
```

Then close and reopen the PCB editor. Check the startup log to confirm the new version loaded:

```
C:\Users\<user>\OneDrive\Documents\KiCad\9.0\3rdparty\plugins\kicad_mcp_bridge\bridge_startup.log
```

## Threading Rules — Critical

The bridge TCP server runs in a background daemon thread. KiCad's pcbnew C++ is not thread-safe.

- **All handlers** (read and write) must use `_run_on_main_thread(fn)` — schedules on the wx main loop via `wx.CallAfter`, blocks with `threading.Event` (30 s timeout)
- **Never** call `wx` as a bare name inside a closure passed to `_run_on_main_thread` — `wx` is not in the closure's namespace. Use `_save_and_refresh(board)` for saves, which does a local `import wx` inside the helper
- `_mm(value)` — converts float mm → int pcbnew IU; required for `VECTOR2I` (C++ int template)

## Adding a Bridge Handler

1. Add the handler function: `def _handle_<command>(data): ...`
2. Register it in the dispatch table: `"<command>": _handle_<command>`
3. Add a corresponding `_tcp_call("<command>", ...)` call in `src/kicad_mcp/backends/plugin_backend.py` or `src/kicad_mcp_plugin/backends/plugin_direct.py`
4. Reinstall and test: `install_bridge.ps1` → restart pcbnew → call the tool

## Known Installation Hazard

The bridge must exist **only** in `3rdparty\plugins`, **not** in `scripting\plugins`.
If a copy exists in `scripting\plugins`, KiCad loads it in the project-manager Python context
where `import pcbnew` fails. That failed import is cached in `sys.modules` — when pcbnew opens,
Python finds the broken cached module and skips re-import, so the bridge never starts.

`install_bridge.ps1` Step 1a deletes any `scripting\plugins\kicad_mcp_bridge*` copy automatically.
