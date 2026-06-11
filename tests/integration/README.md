# Integration tests

These tests run against a real KiCad pcbnew process with the `kicad_mcp_bridge`
plugin loaded. They catch the class of bugs unit tests can't see — the ones
that depend on pcbnew's C++ APIs, on real footprint loading, or on bridge
threading semantics (the silk-centroid mating-face bug is the canonical
example: 229 unit tests passed but the algorithm was wrong against real
KiCad geometry).

The default `pytest` run skips this directory. Opt in with the
`KICAD_INTEGRATION=1` environment variable.

---

## Running locally (Windows)

1. Confirm the bridge is installed (one-time):
   ```powershell
   kicad-mcp install-bridge --kicad-version 9.0
   ```
2. Open **pcbnew** with any `.kicad_pcb` file. Tail the bridge log to confirm
   `TCP server started`:
   ```
   %USERPROFILE%\OneDrive\Documents\KiCad\9.0\3rdparty\plugins\kicad_mcp_bridge\bridge_startup.log
   ```
3. Run the integration smoke test:
   ```powershell
   $env:KICAD_INTEGRATION = "1"
   .venv\Scripts\python.exe -m pytest tests/integration -v
   ```

If the smoke test passes, the harness is wired up correctly. Failures here
mean the harness itself is broken — fix that before reading bridge-handler
test results.

---

## Running locally (Linux / macOS)

Same flow as Windows, with platform-appropriate paths. The bridge installer
detects the platform automatically:

```bash
kicad-mcp install-bridge --kicad-version 9.0
# then start pcbnew with a board, and:
KICAD_INTEGRATION=1 pytest tests/integration -v
```

---

## State isolation strategy

pcbnew has one open board at a time, and `reload_board` is unreliable on
KiCad 9 (see ROADMAP.md §3.4). Tests therefore follow these rules:

1. **Shared session, developer-opened board.** You open any `.kicad_pcb`
   in pcbnew once at the start of the session. Tests use whatever board
   is open — they do not reopen, and they MUST NOT call `reload_board`.
2. **Append-only with ref namespacing.** A test that adds footprints uses
   refs prefixed with its test number (e.g. `T06_R1` for the test
   anchoring REQ-COV-006, `T07_R1` for REQ-COV-007) so concurrently
   failing tests don't collide.
3. **Each test does its own setup.** No implicit dependencies between
   tests. Test ordering is not relied upon — except for the two
   intentional alphabetical bookends described next.
4. **Filename ordering bookends.** Pytest collects files alphabetically,
   so:
   - `test_aa_smoke.py` runs **first** so harness failures surface
     before bridge-handler failures.
   - `test_zz_clear_routes.py` runs **last** because `clear_routes` is
     destructive to all tracks/vias on the shared board — running it
     earlier would poison any later test that relied on routing state.
5. **Teardown is best-effort.** The shared board is allowed to accumulate
   state across a session; restart pcbnew between runs for a clean slate.

If a test truly requires an empty board, it must restart pcbnew itself —
do not assume "clear" state.

---

## Adding a new integration test

1. Decide whether the test exercises a single bridge handler (call via
   `_tcp_call` directly) or a full MCP-tool round-trip (call the tool's
   underlying implementation function with the plugin backend).
2. Pick a unique ref prefix (`T17_*` and onward — the first 16 are
   reserved for the REQ-COV-001 … 016 starter set).
3. Mark the test with `pytestmark = pytest.mark.integration` at module
   scope and depend on the `bridge_session` fixture.
4. Document any fixture footprint the test loads from the KiCad standard
   library — Ubuntu's `kicad` apt package may not ship the same set as the
   Windows installer.
5. If the new test is destructive (e.g. clears or wipes shared state),
   place it in a file whose alphabetical name sorts after every
   non-destructive test (use a `test_zz_` prefix), and document the
   reason at the top of the file.
