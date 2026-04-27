"""Design Rule Check tools - 7 tools."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.response_limit import limit_response
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.drc")


def run_validate_schematic_for_pcb(sch_path: Path) -> dict[str, Any]:
    """File-based schematic completeness check before PCB sync.

    Extracted as module-level function so it can be imported by pcb_pipeline.
    Returns the same structure as the validate_schematic_for_pcb MCP tool.
    """
    from kicad_mcp.backends.file_backend import FileSchematicOps

    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    try:
        sch_ops = FileSchematicOps()
        sch_data = sch_ops.read_schematic(sch_path)
    except Exception as exc:
        return {
            "ready_for_pcb_sync": False,
            "blocking_issues": [{"type": "parse_error", "detail": str(exc)}],
            "warnings": [],
        }

    symbols = sch_data.get("symbols", [])
    nets = sch_data.get("nets", [])

    # Filter to real (non-power, non-#) components
    real_components = [
        s for s in symbols
        if not s.get("is_power") and not (s.get("reference", "").startswith("#"))
    ]

    # ── Check 1: All components have a non-empty Footprint ───────────────────
    for sym in real_components:
        fp = sym.get("footprint", "")
        if not fp:
            blocking.append({
                "type": "missing_footprint",
                "reference": sym.get("reference", "?"),
                "detail": f"Component {sym.get('reference', '?')} has no Footprint property set.",
            })

    # ── Check 2: Unique, non-empty references ────────────────────────────────
    ref_counts: dict[str, int] = {}
    for sym in real_components:
        ref = sym.get("reference", "")
        if not ref:
            blocking.append({
                "type": "empty_reference",
                "detail": f"A component with value '{sym.get('value', '?')}' has an empty reference.",
            })
        else:
            ref_counts[ref] = ref_counts.get(ref, 0) + 1
    for ref, count in ref_counts.items():
        if count > 1:
            blocking.append({
                "type": "duplicate_reference",
                "reference": ref,
                "detail": f"Reference '{ref}' appears {count} times in the schematic.",
            })

    # ── Check 3: PWR_FLAG coverage for power nets ────────────────────────────
    pwr_flag_refs: set[str] = set()
    pwr_flag_values: set[str] = set()
    power_net_names: set[str] = set()

    for sym in symbols:
        lib_id = sym.get("lib_id", "")
        value = sym.get("value", "")
        if sym.get("is_power"):
            power_net_names.add(value)
        if "PWR_FLAG" in lib_id or value == "PWR_FLAG":
            pwr_flag_refs.add(sym.get("reference", ""))

    # Check nets list for power-like names
    for net in nets:
        name = net.get("name", "") if isinstance(net, dict) else str(net)
        if name and any(name.startswith(p) for p in ("GND", "VCC", "VDD", "VBUS", "+", "PWR")):
            power_net_names.add(name)

    # PWR_FLAG check: if there are power nets and no PWR_FLAG instances, warn
    if power_net_names and not pwr_flag_refs:
        warnings.append({
            "type": "missing_pwr_flag",
            "detail": (
                f"Power nets detected ({', '.join(sorted(power_net_names)[:5])}) but no PWR_FLAG "
                "symbols found. Add PWR_FLAG to each power net to silence ERC power_pin_not_driven errors."
            ),
        })

    # ── Check 4: No component at position (0, 0) ────────────────────────────
    for sym in real_components:
        pos = sym.get("position", {})
        x = pos.get("x", None)
        y = pos.get("y", None)
        if x is not None and y is not None and abs(x) < 0.01 and abs(y) < 0.01:
            warnings.append({
                "type": "unplaced_component",
                "reference": sym.get("reference", "?"),
                "detail": f"Component {sym.get('reference', '?')} is at (0, 0) — it may not have been placed.",
            })

    # ── Check 5: Net count non-zero ──────────────────────────────────────────
    if not nets:
        blocking.append({
            "type": "no_nets",
            "detail": "Schematic has no nets. Add wires and labels to connect components.",
        })

    # ── Check 6: Run ERC via kicad-cli if available ──────────────────────────
    erc_results: dict[str, Any] | None = None
    try:
        from kicad_mcp.utils.platform_helper import find_kicad_cli
        cli = find_kicad_cli()
        if cli:
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                erc_out = Path(tf.name)
            try:
                proc = subprocess.run(
                    [str(cli), "sch", "erc", "--output", str(erc_out), str(sch_path)],
                    capture_output=True, text=True, timeout=30,
                )
                if erc_out.exists():
                    try:
                        erc_data = json.loads(erc_out.read_text(encoding="utf-8"))
                        erc_results = erc_data
                        # Surface ERC errors as blocking issues
                        for sheet in erc_data.get("sheets", []):
                            for violation in sheet.get("violations", []):
                                if violation.get("severity") == "error":
                                    blocking.append({
                                        "type": "erc_error",
                                        "detail": violation.get("description", str(violation)),
                                    })
                    except (json.JSONDecodeError, OSError):
                        pass
                    finally:
                        erc_out.unlink(missing_ok=True)
            except (subprocess.TimeoutExpired, OSError):
                pass
    except Exception:
        pass

    ready = len(blocking) == 0
    result: dict[str, Any] = {
        "ready_for_pcb_sync": ready,
        "blocking_issues": blocking,
        "warnings": warnings,
    }
    if erc_results is not None:
        result["erc_results"] = erc_results
    return result


def run_check_courtyard_overlaps(pcb_path: Path) -> dict[str, Any]:
    """File-based courtyard overlap check — extracted for use by pcb_pipeline.

    Returns the same structure as the check_courtyard_overlaps MCP tool
    (without the JSON serialisation step).
    """
    import math as _math

    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

    content = pcb_path.read_text(encoding="utf-8")
    courtyards: dict[str, dict[str, float]] = {}

    at_pat = re.compile(r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)')
    ref_pat = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')
    fp_ref_pat = re.compile(r'\(fp_text\s+reference\s+"([^"]+)"')
    coord_pat = re.compile(r'\((?:start|end)\s+([-\d.]+)\s+([-\d.]+)\)')

    i = 0
    n = len(content)
    while i < n:
        if content[i] != "(":
            i += 1
            continue
        j = i + 1
        while j < n and content[j] not in (" ", "\t", "\n", "(", ")"):
            j += 1
        token = content[i + 1 : j]

        if token == "footprint":
            end_idx = _walk_balanced_parens(content, i)
            if end_idx is None:
                i += 1
                continue
            block = content[i : end_idx + 1]

            ref_m = ref_pat.search(block)
            if not ref_m:
                ref_m = fp_ref_pat.search(block)
            ref = ref_m.group(1) if ref_m else None
            if not ref or ref.startswith("#"):
                i = end_idx + 1
                continue

            at_m = at_pat.search(block[:block.find("\n") + 200] if "\n" in block else block)
            origin_x = float(at_m.group(1)) if at_m else 0.0
            origin_y = float(at_m.group(2)) if at_m else 0.0
            rotation_deg = float(at_m.group(3)) if (at_m and at_m.group(3)) else 0.0

            cyd_xs: list[float] = []
            cyd_ys: list[float] = []

            bi = 0
            bn = len(block)
            while bi < bn:
                if block[bi] != "(":
                    bi += 1
                    continue
                bj = bi + 1
                while bj < bn and block[bj] not in (" ", "\t", "\n", "(", ")"):
                    bj += 1
                sub_token = block[bi + 1 : bj]
                if sub_token in ("fp_rect", "fp_line"):
                    sub_end = _walk_balanced_parens(block, bi)
                    if sub_end is not None:
                        sub_block = block[bi : sub_end + 1]
                        if '"F.CrtYd"' in sub_block or '"B.CrtYd"' in sub_block:
                            for m in coord_pat.finditer(sub_block):
                                cyd_xs.append(float(m.group(1)))
                                cyd_ys.append(float(m.group(2)))
                        bi = sub_end + 1
                        continue
                bi += 1

            if cyd_xs and cyd_ys:
                if abs(rotation_deg % 90) < 0.01 and abs(rotation_deg) > 0.01:
                    rad = _math.radians(rotation_deg)
                    cos_r, sin_r = _math.cos(rad), _math.sin(rad)
                    cyd_xs, cyd_ys = (
                        [x * cos_r - y * sin_r for x, y in zip(cyd_xs, cyd_ys)],
                        [x * sin_r + y * cos_r for x, y in zip(cyd_xs, cyd_ys)],
                    )
                courtyards[ref] = {
                    "xmin": origin_x + min(cyd_xs),
                    "ymin": origin_y + min(cyd_ys),
                    "xmax": origin_x + max(cyd_xs),
                    "ymax": origin_y + max(cyd_ys),
                }

            i = end_idx + 1
            continue
        i += 1

    overlaps: list[dict[str, Any]] = []
    refs = sorted(courtyards.keys())
    for ai in range(len(refs)):
        for bi in range(ai + 1, len(refs)):
            ra, rb = refs[ai], refs[bi]
            a, b = courtyards[ra], courtyards[rb]
            overlap_x = min(a["xmax"], b["xmax"]) - max(a["xmin"], b["xmin"])
            overlap_y = min(a["ymax"], b["ymax"]) - max(a["ymin"], b["ymin"])
            if overlap_x > 0 and overlap_y > 0:
                suggested_move = _math.sqrt(overlap_x ** 2 + overlap_y ** 2)
                overlaps.append({
                    "ref_a": ra,
                    "ref_b": rb,
                    "overlap_x_mm": round(overlap_x, 4),
                    "overlap_y_mm": round(overlap_y, 4),
                    "suggested_move_mm": round(suggested_move + 0.5, 2),
                })

    return {
        "passed": len(overlaps) == 0,
        "overlap_count": len(overlaps),
        "footprints_checked": len(courtyards),
        "overlaps": overlaps,
    }


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register DRC/ERC tools on the MCP server."""

    @mcp.tool()
    def run_drc(path: str, output: str | None = None) -> str:
        """Run Design Rule Check on a PCB board.

        Checks for clearance violations, unconnected nets, track width violations,
        and other manufacturing constraint issues.

        Args:
            path: Path to .kicad_pcb file.
            output: Optional path for the DRC report file (JSON format).

        Returns:
            JSON with DRC results: passed/failed, error/warning counts, violations list.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out = Path(output) if output else None

        try:
            backend.save_board(p)
            drc_ops = backend.get_drc_ops()
            result = drc_ops.run_drc(p, out)
            change_log.record("run_drc", {"path": path, "output": output})
            return json.dumps({"status": "success", **limit_response(result)}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"DRC failed: {e}. Requires kicad-cli backend.",
            })

    @mcp.tool()
    def run_erc(path: str, output: str | None = None) -> str:
        """Run Electrical Rules Check on a schematic.

        Checks for unconnected pins, conflicting pin types, missing power flags,
        and other electrical connectivity issues.

        When kicad-cli is available, runs the full KiCad ERC. Otherwise falls
        back to file-based ERC lite which checks for duplicate references,
        floating pins, and missing power connections.

        Args:
            path: Path to .kicad_sch file.
            output: Optional path for the ERC report file (JSON format).

        Returns:
            JSON with ERC results: passed/failed, error/warning counts, violations list.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        out = Path(output) if output else None

        try:
            drc_ops = backend.get_drc_ops()
            result = drc_ops.run_erc(p, out)
            change_log.record("run_erc", {"path": path, "output": output})
            return json.dumps({"status": "success", **limit_response(result)}, indent=2)
        except NotImplementedError:
            # Fall back to file-based validation
            try:
                sch_ops = backend.get_schematic_ops()
                result = sch_ops.validate_schematic(p)
                result["backend"] = "file"
                result["note"] = "File-based ERC lite. For full ERC, install kicad-cli."
                change_log.record("run_erc", {"path": path, "output": output, "backend": "file"})
                return json.dumps({"status": "success", **limit_response(result)}, indent=2)
            except Exception as fallback_err:
                return json.dumps({
                    "status": "error",
                    "message": f"ERC failed: {fallback_err}. Neither kicad-cli nor file-based ERC available.",
                })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"ERC failed: {e}",
            })

    @mcp.tool()
    def validate_schematic(
        path: str,
        check_floating_pins: bool = True,
        check_duplicate_references: bool = True,
    ) -> str:
        """File-based electrical rules check (no kicad-cli needed).

        Performs basic ERC validation using file parsing and connectivity
        analysis. Checks for:
        - Duplicate reference designators (error)
        - Floating pins without no-connect markers (warning)
        - Unconnected power symbols (warning)

        This is a lightweight alternative to run_erc that works without
        kicad-cli installed.

        Args:
            path: Path to .kicad_sch file.
            check_floating_pins: Check for unconnected pins (default true).
            check_duplicate_references: Check for duplicate references (default true).

        Returns:
            JSON with {passed, violations, error_count, warning_count}.
        """
        p = validate_kicad_path(path, ".kicad_sch")

        ops = backend.get_schematic_ops()
        try:
            result = ops.validate_schematic(p)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": "Schematic validation not supported by current backend.",
            })

        # Filter violations based on check flags
        if not check_floating_pins:
            result["violations"] = [
                v for v in result["violations"]
                if v["type"] != "floating_pin"
            ]
        if not check_duplicate_references:
            result["violations"] = [
                v for v in result["violations"]
                if v["type"] != "duplicate_reference"
            ]

        # Recount after filtering
        result["error_count"] = sum(
            1 for v in result["violations"] if v["severity"] == "error"
        )
        result["warning_count"] = sum(
            1 for v in result["violations"] if v["severity"] == "warning"
        )
        result["passed"] = result["error_count"] == 0

        change_log.record("validate_schematic", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def validate_schematic_cli(path: str) -> str:
        """Validate schematic loadability using kicad-cli's strict export validator.

        Runs ``kicad-cli sch export --format svg`` on the schematic. This exercises
        kicad-cli's C++ symbol loader, which is stricter than the plugin-backend
        Python API. Use this in Phase 3 alongside ``run_erc`` to catch lib_symbol
        defects (malformed geometry, unsupported extends chains, etc.) that the
        plugin-backend ERC accepts but kicad-cli export rejects.

        Args:
            path: Path to .kicad_sch file.

        Returns:
            JSON with {passed, backend, message} or {status: "unavailable"} if
            kicad-cli is not installed.
        """
        import subprocess
        import tempfile

        p = validate_kicad_path(path, ".kicad_sch")

        try:
            from kicad_mcp.utils.platform_helper import find_kicad_cli
            cli = find_kicad_cli()
        except Exception:
            cli = None

        if not cli:
            return json.dumps({
                "status": "unavailable",
                "message": "kicad-cli not found. Install KiCad and ensure kicad-cli is on PATH.",
            }, indent=2)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_svg = Path(tmp_dir) / "validate_check.svg"
            try:
                proc = subprocess.run(
                    [str(cli), "sch", "export", "--format", "svg",
                     "--output", str(tmp_svg), str(p)],
                    capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                return json.dumps({
                    "status": "error",
                    "passed": False,
                    "backend": "kicad-cli",
                    "message": "kicad-cli timed out after 30 s",
                }, indent=2)
            except OSError as e:
                return json.dumps({
                    "status": "error",
                    "passed": False,
                    "backend": "kicad-cli",
                    "message": f"kicad-cli launch failed: {e}",
                }, indent=2)

        if proc.returncode == 0:
            change_log.record("validate_schematic_cli", {"path": path, "passed": True})
            return json.dumps({
                "status": "success",
                "passed": True,
                "backend": "kicad-cli",
                "message": "Schematic loaded and exported successfully — no validator errors.",
            }, indent=2)

        # Non-zero exit: surface the error text
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        change_log.record("validate_schematic_cli", {"path": path, "passed": False})
        return json.dumps({
            "status": "success",
            "passed": False,
            "backend": "kicad-cli",
            "exit_code": proc.returncode,
            "message": detail,
        }, indent=2)

    @mcp.tool()
    def validate_board(path: str) -> str:
        """File-based pre-flight checks for a PCB board (no kicad-cli needed).

        Checks performed:
        - Edge.Cuts outline present (error if missing — board cannot be manufactured)
        - Duplicate reference designators (error)
        - Footprints at position (0, 0) (warning — likely unplaced)
        - Design rules block in .kicad_pro file (warning if absent)

        This is a lightweight alternative to run_drc that works without
        kicad-cli installed.

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with {passed, violations, error_count, warning_count, checks_performed}.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        from kicad_mcp.backends.file_backend import FileBoardOps

        try:
            result = FileBoardOps().validate_board(p)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "message": f"Board validation failed: {exc}",
            })

        change_log.record("validate_board", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def validate_schematic_for_pcb(path: str) -> str:
        """Pre-PCB-sync schematic completeness check (no kicad-cli required).

        A richer gate than run_erc that catches issues which become expensive
        to fix after sync_schematic_to_pcb. All checks are file-based.

        Checks performed:
          1. All non-power components have a non-empty Footprint property
          2. All references are unique and non-empty
          3. PWR_FLAG coverage for power nets (warning if missing)
          4. No component placed at position (0, 0)
          5. Schematic has at least one net
          6. Full ERC via kicad-cli (if available — errors become blocking issues)

        The model MUST NOT call sync_schematic_to_pcb if ready_for_pcb_sync is false.

        Args:
            path: Path to .kicad_sch file.

        Returns:
            JSON with ready_for_pcb_sync bool, blocking_issues list, warnings list,
            and optional erc_results (only when kicad-cli is available).
        """
        p = validate_kicad_path(path, ".kicad_sch")
        result = run_validate_schematic_for_pcb(p)
        change_log.record("validate_schematic_for_pcb", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def check_courtyard_overlaps(path: str) -> str:
        """Fast file-based courtyard overlap check before routing.

        Parses all footprint courtyard bounding boxes from the .kicad_pcb file
        and runs an O(n²) axis-aligned bounding box (AABB) intersection check.
        Milliseconds to run — no kicad-cli required.

        Call this after auto_place and after any move_component batch, before
        starting autoroute. The model MUST NOT call autoroute if passed is false.

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with passed bool and list of overlapping component pairs, each
            with ref_a, ref_b, overlap_x_mm, overlap_y_mm, and suggested_move_mm.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        result = run_check_courtyard_overlaps(p)
        change_log.record("check_courtyard_overlaps", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def get_board_design_rules(path: str) -> str:
        """Get the design rules configured for a PCB board.

        Returns clearance constraints, track width limits, via size requirements,
        and other manufacturing rules.

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with design rule parameters.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        ops = backend.get_board_ops()
        rules = ops.get_design_rules(p)
        change_log.record("get_board_design_rules", {"path": path})
        return json.dumps({"status": "success", "rules": rules}, indent=2)
