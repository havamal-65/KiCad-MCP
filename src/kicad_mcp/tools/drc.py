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


# ---------------------------------------------------------------------------
# Symbol/footprint pair validator helpers (§6.2)
# ---------------------------------------------------------------------------

def _collect_all_real_symbols(root_sch_path: Path) -> list[dict[str, Any]]:
    """Walk every sub-sheet from *root_sch_path* and return a flat list of real symbols.

    "Real" means not power and reference does not start with "#" — matching the
    filter used by ``run_validate_schematic_for_pcb``. Each returned dict carries
    the schematic's ``reference``, ``lib_id``, and ``footprint`` fields plus a
    ``_sheet_path`` (str) so error messages can name the originating file.
    Circular sheet references are skipped (first occurrence wins).
    """
    from kicad_mcp.backends.file_backend import FileSchematicOps

    sch_ops = FileSchematicOps()
    visited: set[str] = set()
    out: list[dict[str, Any]] = []

    def _walk(p: Path) -> None:
        resolved = str(p.resolve())
        if resolved in visited:
            return
        visited.add(resolved)
        try:
            data = sch_ops.read_schematic(p)
        except Exception:
            return  # malformed sub-sheet — let the existing parse_error path handle it
        for sym in data.get("symbols", []):
            ref = sym.get("reference", "")
            if sym.get("is_power"):
                continue
            if ref.startswith("#"):
                continue
            sym["_sheet_path"] = str(p)
            out.append(sym)
        for sh in data.get("sheets", []):
            sheetfile = sh.get("sheetfile", "")
            if not sheetfile:
                continue
            child_path = p.parent / sheetfile
            if child_path.exists():
                _walk(child_path)

    _walk(root_sch_path)
    return out


def _classify_fp_resolution_failure(lib_name: str, project_dir: str | Path | None = None) -> str:
    """Distinguish a missing footprint library from a missing footprint file inside one."""
    from kicad_mcp.utils.kicad_paths import find_footprint_libraries
    for lib_dir in find_footprint_libraries(project_dir):
        if lib_dir.stem == lib_name or lib_dir.stem.replace(".pretty", "") == lib_name:
            return "footprint file not found"
    return "library not found"


def run_validate_symbol_footprint_pairs(sch_path: Path) -> dict[str, Any]:
    """Verify every symbol's Footprint field resolves and its pin set is a
    subset of the footprint's pad set.

    Walks all sub-sheets, de-duplicates by reference (multi-unit instances),
    skips symbols with empty Footprint (those are caught by check 1 of
    ``run_validate_schematic_for_pcb``), and returns the shape documented in
    docs/specs/symbol-footprint-validator/spec.md §4.2. Read-only.
    """
    from kicad_mcp.backends.file_backend import (
        FileLibraryOps,
        _load_kicad_mod,
        _parse_footprint_detail,
    )
    from kicad_mcp.utils.sexp_parser import parse_sexp_content

    mismatches: list[dict[str, Any]] = []
    unresolvable: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    LIMIT = 20

    symbols = _collect_all_real_symbols(sch_path)
    lib_ops = FileLibraryOps()
    # Resolve footprints with the project dir so project-local .pretty libraries
    # (registered in the project fp-lib-table, S1) resolve — otherwise they'd be
    # mis-reported as unresolvable and falsely block sync.
    project_dir = sch_path.parent

    seen_refs: set[str] = set()
    checked = 0
    for sym in symbols:
        ref = sym.get("reference", "")
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        checked += 1

        fp_lib_id = sym.get("footprint", "") or ""
        if not fp_lib_id:
            continue

        fp_text = _load_kicad_mod(fp_lib_id, project_dir)
        if fp_text is None:
            lib_name = fp_lib_id.split(":", 1)[0] if ":" in fp_lib_id else fp_lib_id
            reason = _classify_fp_resolution_failure(lib_name, project_dir)
            unresolvable.append({
                "ref": ref, "footprint": fp_lib_id, "reason": reason,
            })
            continue

        sym_lib_id = sym.get("lib_id", "") or ""
        sym_info = lib_ops.get_symbol_info(sym_lib_id)
        if "error" in sym_info:
            unresolvable.append({
                "ref": ref, "footprint": fp_lib_id,
                "reason": "symbol library not found",
            })
            continue

        S = {
            str(p.get("number", ""))
            for p in sym_info.get("pins", [])
            if p.get("number")
        }

        try:
            fp_tree = parse_sexp_content(fp_text, source=fp_lib_id)
            fp_detail = _parse_footprint_detail(fp_tree, *fp_lib_id.split(":", 1))
        except Exception:
            unresolvable.append({
                "ref": ref, "footprint": fp_lib_id,
                "reason": "footprint file not parseable",
            })
            continue

        F = {
            str(p.get("number", ""))
            for p in fp_detail.get("pads", [])
            if p.get("number")
        }

        missing = S - F
        extra = F - S
        if missing:
            mismatches.append({
                "ref": ref,
                "footprint": fp_lib_id,
                "symbol_pins": sorted(S),
                "footprint_pads": sorted(F),
                "missing": sorted(missing),
            })
        if extra:
            warnings.append({
                "ref": ref,
                "footprint": fp_lib_id,
                "extra_pads": sorted(extra),
                "reason": (
                    "footprint has pads not referenced by symbol "
                    "(likely mechanical / thermal / NC)"
                ),
            })

    over_limit = False
    for arr in (mismatches, unresolvable, warnings):
        if len(arr) > LIMIT:
            over_limit = True
            del arr[LIMIT:]

    return {
        "passed": not (mismatches or unresolvable),
        "checked": checked,
        "mismatches": mismatches,
        "unresolvable": unresolvable,
        "warnings": warnings,
        "over_limit": over_limit,
    }


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

    # The file-based reader doesn't build a nets list, so fall back to
    # checking wires, labels, and power symbols as evidence of connectivity.
    _has_connectivity = (
        nets
        or sch_data.get("wires", [])
        or sch_data.get("labels", [])
        or any(s.get("is_power") for s in symbols)
    )

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
    if not _has_connectivity:
        blocking.append({
            "type": "no_nets",
            "detail": "Schematic has no nets. Add wires and labels to connect components.",
        })

    # ── Check 7: Symbol pins ⊆ footprint pads (§6.2 sub-gate) ────────────────
    # A symbol whose pin numbers aren't all present on its Footprint produces a
    # board with unassigned pads — caught here before sync rather than at DRC.
    sf_result = run_validate_symbol_footprint_pairs(sch_path)
    for mm in sf_result.get("mismatches", []):
        blocking.append({
            "type": "footprint_pad_mismatch",
            "reference": mm["ref"],
            "detail": (
                f"{mm['ref']} ({mm['footprint']}) symbol expects pad(s) "
                f"{mm['missing']} which don't exist on the footprint. "
                f"Symbol pins: {mm['symbol_pins']}; footprint pads: {mm['footprint_pads']}."
            ),
        })
    for un in sf_result.get("unresolvable", []):
        blocking.append({
            "type": "unresolvable_footprint",
            "reference": un["ref"],
            "detail": f"{un['ref']} footprint {un['footprint']!r}: {un['reason']}.",
        })
    for w in sf_result.get("warnings", []):
        warnings.append({
            "type": "extra_footprint_pads",
            "reference": w["ref"],
            "detail": (
                f"{w['ref']} ({w['footprint']}) has extra pads {w['extra_pads']} "
                f"not referenced by the symbol — usually mechanical / thermal / NC. "
                f"Not blocking."
            ),
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
                if abs(rotation_deg) > 0.01:
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


# ---------------------------------------------------------------------------
# Edge-facing connector detection (Phase 6.1.1)
# ---------------------------------------------------------------------------

# Substrings that indicate a connector with an external mating face. Matched
# case-sensitively against the footprint's full lib_id (Library:Name).
_EDGE_NAME_TOKENS: tuple[str, ...] = (
    "Horizontal",
    "_Receptacle_",
    "Connector_USB",
    "Connector_Audio",
    "Connector_JST",
    "Connector_BarrelJack",
    "Connector_RJ",
    "Connector_HDMI",
    "Connector_Coaxial",
)


def _classify_footprint_local_face(local_x: float, local_y: float) -> str:
    """Return the dominant compass direction of (local_x, local_y) in local frame.

    Returns one of "+x", "-x", "+y", "-y". Used to derive mating face from a
    "PCB edge" marker's position relative to the footprint origin.
    """
    if abs(local_x) >= abs(local_y):
        return "+x" if local_x >= 0 else "-x"
    return "+y" if local_y >= 0 else "-y"


def _scan_footprint_for_edge_marker(block: str) -> tuple[float, float] | None:
    """Find a (fp_text user "PCB edge" ... (layer "Dwgs.User")) inside a footprint block.

    Returns the (local_x, local_y) of the marker's (at ...) clause, or None if
    no marker is present. The marker is the high-confidence signal placed by
    KiCad library maintainers on horizontal/receptacle connector footprints.
    """
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

    at_pat = re.compile(r'\(at\s+([-\d.]+)\s+([-\d.]+)')

    i = 0
    n = len(block)
    while i < n:
        if block[i] != "(":
            i += 1
            continue
        j = i + 1
        while j < n and block[j] not in (" ", "\t", "\n", "(", ")"):
            j += 1
        token = block[i + 1 : j]
        if token != "fp_text":
            i += 1
            continue
        end_idx = _walk_balanced_parens(block, i)
        if end_idx is None:
            i += 1
            continue
        sub_block = block[i : end_idx + 1]
        if '"PCB edge"' in sub_block and '"Dwgs.User"' in sub_block:
            at_m = at_pat.search(sub_block)
            if at_m:
                return (float(at_m.group(1)), float(at_m.group(2)))
        i = end_idx + 1
    return None


def _scan_footprint_courtyard_center(block: str) -> tuple[float, float] | None:
    """Centroid of the F.CrtYd rectangle (or fp_line extents) in local frame."""
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

    coord_pat = re.compile(r'\((?:start|end)\s+([-\d.]+)\s+([-\d.]+)\)')

    xs: list[float] = []
    ys: list[float] = []
    i = 0
    n = len(block)
    while i < n:
        if block[i] != "(":
            i += 1
            continue
        j = i + 1
        while j < n and block[j] not in (" ", "\t", "\n", "(", ")"):
            j += 1
        token = block[i + 1 : j]
        if token not in ("fp_rect", "fp_line"):
            i += 1
            continue
        end_idx = _walk_balanced_parens(block, i)
        if end_idx is None:
            i += 1
            continue
        sub = block[i : end_idx + 1]
        if '"F.CrtYd"' in sub or '"B.CrtYd"' in sub:
            for m in coord_pat.finditer(sub):
                xs.append(float(m.group(1)))
                ys.append(float(m.group(2)))
        i = end_idx + 1
    if not xs:
        return None
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def _scan_footprint_pad_centroid(block: str) -> tuple[float, float] | None:
    """Compute the centroid of pads inside a footprint block (footprint-local frame).

    Returns None if no pads were found. Used by the name-heuristic fallback
    to infer the mating-face direction: see `_scan_footprint_courtyard_center`
    above — the mating face is the direction from pad centroid toward the
    courtyard center (which sits in the body of the connector, where the
    cable inserts).
    """
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

    pad_at_pat = re.compile(r'\(at\s+([-\d.]+)\s+([-\d.]+)')

    xs: list[float] = []
    ys: list[float] = []
    i = 0
    n = len(block)
    while i < n:
        if block[i] != "(":
            i += 1
            continue
        j = i + 1
        while j < n and block[j] not in (" ", "\t", "\n", "(", ")"):
            j += 1
        token = block[i + 1 : j]
        if token != "pad":
            i += 1
            continue
        end_idx = _walk_balanced_parens(block, i)
        if end_idx is None:
            i += 1
            continue
        sub_block = block[i : end_idx + 1]
        # Skip non-plated through-holes (mounting holes) — they don't anchor the cable
        if "np_thru_hole" in sub_block:
            i = end_idx + 1
            continue
        at_m = pad_at_pat.search(sub_block)
        if at_m:
            xs.append(float(at_m.group(1)))
            ys.append(float(at_m.group(2)))
        i = end_idx + 1

    if not xs:
        return None
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _name_matches_edge_connector(lib_id: str) -> str | None:
    """Return the first _EDGE_NAME_TOKENS substring matched in lib_id, or None."""
    for token in _EDGE_NAME_TOKENS:
        if token in lib_id:
            return token
    return None


def run_identify_edge_facing_connectors(pcb_path: Path) -> dict[str, Any]:
    """Detect footprints that need outward-facing edge placement.

    Scans every (footprint ...) block in pcb_path and classifies each
    based on multiple signals (highest-confidence first):

      1. (fp_text user "PCB edge" ... (layer "Dwgs.User")) — definitive
         marker placed by KiCad library maintainers on horizontal /
         receptacle footprints. The marker's (at lx ly) gives the
         mating-face direction in footprint-local frame.

      2. Footprint name heuristic — lib_id substring match against
         "Horizontal", "_Receptacle_", "Connector_USB",
         "Connector_Audio", "Connector_JST", "Connector_BarrelJack",
         "Connector_RJ", "Connector_HDMI", "Connector_Coaxial". When
         matched, mating face is inferred from the pad-cluster centroid:
         the cable enters opposite the pad cluster.

      3. (attr through_hole exclude_from_pos_files) combined with a
         J/P reference is a confidence-bump for hand-solder cable
         connectors that don't have the marker.

    Returns ``mating_face`` in the footprint's LOCAL coordinate frame. The
    placed footprint's outer ``(at x y rotation)`` must be applied by callers
    (e.g. validate_connector_orientations) to get the board-frame direction.

    Returns:
        ``{"connectors": [{ref, footprint, mating_face, confidence, evidence}],
           "checked_count": <int>}``
    """
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

    content = pcb_path.read_text(encoding="utf-8")

    fp_header_pat = re.compile(r'\(footprint\s+"([^"]+)"')
    ref_pat = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')
    fp_ref_pat = re.compile(r'\(fp_text\s+reference\s+"([^"]+)"')
    attr_pat = re.compile(r"\(attr\s+([^\)]+)\)")

    connectors: list[dict[str, Any]] = []
    checked = 0

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

        if token != "footprint":
            i += 1
            continue

        end_idx = _walk_balanced_parens(content, i)
        if end_idx is None:
            i += 1
            continue

        block = content[i : end_idx + 1]
        i = end_idx + 1

        header_m = fp_header_pat.match(block)
        lib_id = header_m.group(1) if header_m else ""

        ref_m = ref_pat.search(block) or fp_ref_pat.search(block)
        ref = ref_m.group(1) if ref_m else None
        if not ref or ref.startswith("#"):
            continue

        checked += 1

        # ---- Signal 1: "PCB edge" marker (high confidence) -----------------
        marker_pos = _scan_footprint_for_edge_marker(block)
        if marker_pos is not None:
            face = _classify_footprint_local_face(*marker_pos)
            connectors.append({
                "ref": ref,
                "footprint": lib_id,
                "mating_face": face,
                "confidence": "high",
                "evidence": (
                    f"Dwgs.User 'PCB edge' marker at "
                    f"({marker_pos[0]:.2f}, {marker_pos[1]:.2f})"
                ),
            })
            continue

        # ---- Signal 2: pad-vs-courtyard geometry (medium confidence) -------
        # If the lib_id matches a known edge-connector pattern, infer the
        # mating face from the connector's pad-to-body geometry. The body
        # extends from the pad cluster toward the cable opening — so the
        # vector from the pad centroid toward the courtyard center points
        # at the mating face. This is reliable for both USB-C horizontal
        # SMD (pads at back, body extends to mating face) and JST horizontal
        # (pins near one end, body extends to cable opening at the other).
        #
        # An earlier revision used the absolute F.SilkS centroid, but that
        # signal is computed in the footprint's origin frame — and KiCad's
        # USB-C SMD footprints have the origin near the body center while
        # the silk wraps the back shell only. The silk centroid then pointed
        # opposite the mating face, flipping the rotation gate 180° and
        # silently passing inward-facing receptacles. Pad-vs-courtyard is
        # invariant to where the origin sits and is the load-bearing signal.
        matched_token = _name_matches_edge_connector(lib_id)
        if matched_token is None:
            continue
        pad_centroid = _scan_footprint_pad_centroid(block)
        cy_center = _scan_footprint_courtyard_center(block)
        if pad_centroid is not None and cy_center is not None:
            dx = cy_center[0] - pad_centroid[0]
            dy = cy_center[1] - pad_centroid[1]
            if abs(dx) > 0.05 or abs(dy) > 0.05:
                mating_face = _classify_footprint_local_face(dx, dy)
                evidence = (
                    f"name match '{matched_token}'; pad centroid "
                    f"({pad_centroid[0]:.2f}, {pad_centroid[1]:.2f}) → "
                    f"courtyard center ({cy_center[0]:.2f}, {cy_center[1]:.2f})"
                )
            else:
                mating_face = None
                evidence = (
                    f"name match '{matched_token}'; pad centroid coincident "
                    "with courtyard center (symmetric body) — manual review needed"
                )
        elif pad_centroid is not None and (
            abs(pad_centroid[0]) > 0.01 or abs(pad_centroid[1]) > 0.01
        ):
            # Fallback when no courtyard available: use opposite-of-pads
            pad_face = _classify_footprint_local_face(*pad_centroid)
            opposite = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}
            mating_face = opposite[pad_face]
            evidence = (
                f"name match '{matched_token}'; pad centroid at "
                f"({pad_centroid[0]:.2f}, {pad_centroid[1]:.2f}) "
                "(no courtyard — using opposite-of-pads fallback)"
            )
        else:
            mating_face = None
            evidence = f"name match '{matched_token}'; pad centroid unavailable"

        # ---- Signal 3: attr flag bump --------------------------------------
        confidence = "medium"
        attr_m = attr_pat.search(block)
        if attr_m and "exclude_from_pos_files" in attr_m.group(1):
            if ref[:1] in ("J", "P"):
                evidence += "; attr 'exclude_from_pos_files' confirms hand-solder cable"

        connectors.append({
            "ref": ref,
            "footprint": lib_id,
            "mating_face": mating_face,
            "confidence": confidence,
            "evidence": evidence,
        })

    return {
        "connectors": connectors,
        "checked_count": checked,
    }


# ---------------------------------------------------------------------------
# Connector-orientation validation (Phase 6.1.2)
# ---------------------------------------------------------------------------

_FACE_VECTORS: dict[str, tuple[float, float]] = {
    "+x": (1.0, 0.0),
    "-x": (-1.0, 0.0),
    "+y": (0.0, 1.0),
    "-y": (0.0, -1.0),
}

# Outward normal of each board edge in board-frame coordinates.
# Note: KiCad's Y axis points DOWNWARD on screen — north (top) is ymin.
_EDGE_OUTWARD_FACE: dict[str, str] = {
    "north": "-y",
    "south": "+y",
    "east": "+x",
    "west": "-x",
}

# Angular tolerance (degrees) between the board-frame mating face and the
# outward normal of the closest edge for the orientation to count as correct.
_ORIENTATION_TOLERANCE_DEG: float = 30.0


def _rotate_vec(vx: float, vy: float, deg: float) -> tuple[float, float]:
    import math as _math

    rad = _math.radians(deg)
    cos_r = _math.cos(rad)
    sin_r = _math.sin(rad)
    return (vx * cos_r - vy * sin_r, vx * sin_r + vy * cos_r)


def _vec_to_face(vx: float, vy: float) -> str:
    if abs(vx) >= abs(vy):
        return "+x" if vx >= 0 else "-x"
    return "+y" if vy >= 0 else "-y"


def _angle_deg(vx: float, vy: float) -> float:
    import math as _math
    return _math.degrees(_math.atan2(vy, vx))


def _angle_diff(a: float, b: float) -> float:
    """Smallest signed difference between angles *a* and *b*, in degrees."""
    return ((a - b + 180.0) % 360.0) - 180.0


def _parse_board_bbox(content: str) -> tuple[float, float, float, float] | None:
    """Compute the Edge.Cuts bounding box from gr_rect / gr_line graphics.

    Returns (xmin, ymin, xmax, ymax) or None if no Edge.Cuts geometry was found.
    Handles both rectangular outlines (gr_rect) and polygonal outlines made of
    gr_line segments.
    """
    from kicad_mcp.utils.sexp_parser import _walk_balanced_parens

    coord_pat = re.compile(r'\((?:start|end)\s+([-\d.]+)\s+([-\d.]+)\)')

    xs: list[float] = []
    ys: list[float] = []

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
        if token in ("gr_rect", "gr_line", "gr_arc"):
            end_idx = _walk_balanced_parens(content, i)
            if end_idx is None:
                i += 1
                continue
            block = content[i : end_idx + 1]
            if '"Edge.Cuts"' in block:
                for m in coord_pat.finditer(block):
                    xs.append(float(m.group(1)))
                    ys.append(float(m.group(2)))
            i = end_idx + 1
            continue
        # Skip footprint blocks — their inner fp_line/fp_rect are not the outline.
        if token == "footprint":
            end_idx = _walk_balanced_parens(content, i)
            if end_idx is None:
                i += 1
                continue
            i = end_idx + 1
            continue
        i += 1

    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _edge_distances(
    origin_x: float, origin_y: float,
    xmin: float, ymin: float, xmax: float, ymax: float,
) -> dict[str, float]:
    return {
        "north": origin_y - ymin,
        "south": ymax - origin_y,
        "east": xmax - origin_x,
        "west": origin_x - xmin,
    }


def _closest_edge(
    origin_x: float, origin_y: float,
    xmin: float, ymin: float, xmax: float, ymax: float,
) -> str:
    """Return one of "north"/"south"/"east"/"west" — the edge closest to origin."""
    distances = _edge_distances(origin_x, origin_y, xmin, ymin, xmax, ymax)
    return min(distances.items(), key=lambda kv: (kv[1], ["north", "south", "east", "west"].index(kv[0])))[0]


def _acceptable_edges(
    origin_x: float, origin_y: float,
    xmin: float, ymin: float, xmax: float, ymax: float,
    tolerance_mm: float = 2.0,
) -> list[str]:
    """Return all edges within ``tolerance_mm`` of the closest one.

    Corner-placed connectors are nearly equidistant from two edges — a user
    might intend either. Accept both so the validator doesn't punish a 0.5 mm
    difference in distance with a 90° rotation suggestion.
    """
    distances = _edge_distances(origin_x, origin_y, xmin, ymin, xmax, ymax)
    closest = min(distances.values())
    return [e for e, d in distances.items() if d <= closest + tolerance_mm]


def _suggested_rotation_for_edge(local_face: str, edge: str) -> float:
    """Rotation (deg, 0-360) that would place local_face pointing outward at edge."""
    lx, ly = _FACE_VECTORS[local_face]
    local_angle = _angle_deg(lx, ly)
    outward = _EDGE_OUTWARD_FACE[edge]
    ox, oy = _FACE_VECTORS[outward]
    outward_angle = _angle_deg(ox, oy)
    required = (outward_angle - local_angle) % 360.0
    # Normalize to the nearest 90° step — KiCad footprint rotations are typically cardinal.
    snapped = round(required / 90.0) * 90.0 % 360.0
    return float(snapped)


def _get_footprint_placement(content: str, ref: str) -> tuple[float, float, float] | None:
    """Find a placed footprint's outer (at x y rotation). Returns (x, y, rot)."""
    from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference

    located = find_footprint_block_by_reference(content, ref)
    if located is None:
        return None
    start, end = located
    block = content[start : end + 1]
    # The outer (at ...) is the first one after the (footprint "name" (layer ...))
    # — earlier than any (property ... (at ...)) blocks. Look for it before the
    # first property block.
    first_prop = block.find("(property")
    head = block[: first_prop] if first_prop != -1 else block
    at_m = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)', head)
    if not at_m:
        return None
    x = float(at_m.group(1))
    y = float(at_m.group(2))
    rot = float(at_m.group(3)) if at_m.group(3) else 0.0
    return (x, y, rot)


def compute_edge_placement(
    pcb_path: Path, reference: str, edge: str, offset_mm: float = 2.0,
) -> dict[str, Any]:
    """Compute (target_x, target_y, target_rotation) for anchoring a connector at an edge.

    Pure geometry — does NOT mutate the board file. Used by both the
    ``place_at_edge`` tool and ``sync_schematic_to_pcb`` (which reads
    ``PlacementIntent`` properties from schematic symbols).

    Returns either::

        {"status": "success", "target_x": float, "target_y": float,
         "target_rotation": float, "local_mating_face": str, "evidence": str}

    or::

        {"status": "error", "message": "...detailed reason..."}
    """
    from kicad_mcp.backends.file_backend import _parse_footprint_bounds
    from kicad_mcp.utils.sexp_parser import find_footprint_block_by_reference

    if edge not in _EDGE_OUTWARD_FACE:
        return {
            "status": "error",
            "message": f"edge must be one of {list(_EDGE_OUTWARD_FACE)}; got {edge!r}",
        }

    content = pcb_path.read_text(encoding="utf-8")

    bbox = _parse_board_bbox(content)
    if bbox is None:
        return {
            "status": "error",
            "message": "No Edge.Cuts geometry — add a board outline before placing at an edge.",
        }
    board_xmin, board_ymin, board_xmax, board_ymax = bbox

    identify = run_identify_edge_facing_connectors(pcb_path)
    connector = next(
        (c for c in identify["connectors"] if c["ref"] == reference),
        None,
    )
    if connector is None:
        return {
            "status": "error",
            "message": (
                f"{reference} is not detected as an edge-facing connector. "
                "Either it isn't a connector, or its footprint lacks "
                "the 'PCB edge' marker and doesn't match the name heuristic."
            ),
        }
    local_face = connector["mating_face"]
    if local_face is None:
        return {
            "status": "error",
            "message": (
                f"{reference} mating face is indeterminate ({connector['evidence']})."
            ),
        }

    located = find_footprint_block_by_reference(content, reference)
    if located is None:
        return {
            "status": "error",
            "message": f"footprint {reference} not found in board file",
        }
    start, end = located
    block = content[start : end + 1]
    bounds = _parse_footprint_bounds(block)
    courtyard = bounds.get("courtyard")
    if courtyard is None:
        return {
            "status": "error",
            "message": (
                f"{reference} has no courtyard fp_rect on F.CrtYd — "
                "cannot compute clearance offset."
            ),
        }

    target_rotation = _suggested_rotation_for_edge(local_face, edge)

    corners = [
        (courtyard["xmin"], courtyard["ymin"]),
        (courtyard["xmin"], courtyard["ymax"]),
        (courtyard["xmax"], courtyard["ymin"]),
        (courtyard["xmax"], courtyard["ymax"]),
    ]
    rotated = [_rotate_vec(cx, cy, target_rotation) for cx, cy in corners]
    r_xmin = min(rx for rx, _ in rotated)
    r_ymin = min(ry for _, ry in rotated)
    r_xmax = max(rx for rx, _ in rotated)
    r_ymax = max(ry for _, ry in rotated)

    if edge == "north":
        target_x = (board_xmin + board_xmax) / 2.0 - (r_xmin + r_xmax) / 2.0
        target_y = board_ymin + offset_mm - r_ymin
    elif edge == "south":
        target_x = (board_xmin + board_xmax) / 2.0 - (r_xmin + r_xmax) / 2.0
        target_y = board_ymax - offset_mm - r_ymax
    elif edge == "east":
        target_x = board_xmax - offset_mm - r_xmax
        target_y = (board_ymin + board_ymax) / 2.0 - (r_ymin + r_ymax) / 2.0
    else:  # west
        target_x = board_xmin + offset_mm - r_xmin
        target_y = (board_ymin + board_ymax) / 2.0 - (r_ymin + r_ymax) / 2.0

    return {
        "status": "success",
        "target_x": target_x,
        "target_y": target_y,
        "target_rotation": target_rotation,
        "local_mating_face": local_face,
        "evidence": connector["evidence"],
    }


def run_validate_connector_orientations(pcb_path: Path) -> dict[str, Any]:
    """Verify every edge-facing connector points outward at its closest board edge.

    For each connector returned by :func:`run_identify_edge_facing_connectors`:

      1. Look up the placed footprint's ``(at x y rotation)`` in the board file.
      2. Rotate the local-frame ``mating_face`` vector by that rotation to get
         the board-frame face direction.
      3. Find the closest edge (north/south/east/west) by distance from the
         footprint origin to each edge of the Edge.Cuts bounding box.
      4. Check whether the board-frame face direction is within
         ±30° of the outward normal of that edge.

    Connectors with ``mating_face = None`` (medium-confidence detections that
    couldn't resolve a face direction) are reported as ``indeterminate`` and
    do not count as either pass or fail — the model is expected to inspect
    them manually.

    Side effect: writes the result to the board's sidecar validation cache so
    that downstream gates (e.g. ``autoroute``) can confirm a prior pass
    applies to the current board state.

    Returns:
        ``{"passed": bool, "checked": int, "violations": [...], "indeterminate": [...]}``
        ``passed=True`` when no edge-facing connectors were detected
        (empty-board case) — a board with no connectors must not be blocked
        by this gate.
    """
    from kicad_mcp.utils.validation_cache import record_validation

    content = pcb_path.read_text(encoding="utf-8")
    bbox = _parse_board_bbox(content)
    identify = run_identify_edge_facing_connectors(pcb_path)
    connectors = identify["connectors"]

    if not connectors:
        result = {
            "passed": True,
            "checked": 0,
            "violations": [],
            "indeterminate": [],
        }
        record_validation(pcb_path, "validate_connector_orientations", result)
        return result

    if bbox is None:
        # Can't determine edges without an outline. Surface as a blocking
        # violation rather than a silent pass — the user needs to add an
        # Edge.Cuts outline before this gate has meaning.
        result = {
            "passed": False,
            "checked": len(connectors),
            "violations": [{
                "type": "no_board_outline",
                "detail": "No Edge.Cuts geometry found — add a board outline before validating connector orientations.",
            }],
            "indeterminate": [],
        }
        record_validation(pcb_path, "validate_connector_orientations", result)
        return result

    xmin, ymin, xmax, ymax = bbox
    violations: list[dict[str, Any]] = []
    indeterminate: list[dict[str, Any]] = []

    for c in connectors:
        ref = c["ref"]
        local_face = c.get("mating_face")
        if local_face is None:
            indeterminate.append({
                "ref": ref,
                "footprint": c["footprint"],
                "reason": "mating_face could not be determined — manual review required",
                "evidence": c.get("evidence", ""),
            })
            continue

        placement = _get_footprint_placement(content, ref)
        if placement is None:
            indeterminate.append({
                "ref": ref,
                "footprint": c["footprint"],
                "reason": "footprint placement (at ...) could not be parsed",
            })
            continue

        ox, oy, rot = placement
        lvx, lvy = _FACE_VECTORS[local_face]
        bvx, bvy = _rotate_vec(lvx, lvy, rot)
        board_face = _vec_to_face(bvx, bvy)

        # Corner placements are ambiguous — accept any edge within 2 mm of the
        # closest. If the connector faces ANY acceptable edge, pass.
        acceptable = _acceptable_edges(ox, oy, xmin, ymin, xmax, ymax, tolerance_mm=2.0)
        best_diff: float | None = None
        best_edge: str = acceptable[0]
        for edge_candidate in acceptable:
            outward = _EDGE_OUTWARD_FACE[edge_candidate]
            ex, ey = _FACE_VECTORS[outward]
            diff = _angle_diff(_angle_deg(bvx, bvy), _angle_deg(ex, ey))
            if best_diff is None or abs(diff) < abs(best_diff):
                best_diff = diff
                best_edge = edge_candidate

        if best_diff is not None and abs(best_diff) <= _ORIENTATION_TOLERANCE_DEG:
            continue

        violations.append({
            "ref": ref,
            "footprint": c["footprint"],
            "current_face": local_face,
            "current_face_in_board_frame": board_face,
            "closest_edge": best_edge,
            "acceptable_edges": acceptable,
            "suggested_edge": best_edge,
            "suggested_rotation": _suggested_rotation_for_edge(local_face, best_edge),
            "angle_off_deg": round(best_diff if best_diff is not None else 0.0, 1),
        })

    result = {
        "passed": len(violations) == 0,
        "checked": len(connectors),
        "violations": violations,
        "indeterminate": indeterminate,
    }
    record_validation(pcb_path, "validate_connector_orientations", result)
    return result


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
            # Stamp the result so export_gerbers can gate on a clean DRC against
            # the current board content (§6.3).
            try:
                from kicad_mcp.utils.validation_cache import record_validation
                record_validation(p, "run_drc", {"passed": bool(result.get("passed"))})
            except Exception as cache_exc:
                logger.warning("run_drc: validation-cache stamp failed (non-fatal): %s", cache_exc)
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
            try:
                proc = subprocess.run(
                    [str(cli), "sch", "export", "svg",
                     "--output", str(tmp_dir), str(p)],
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
        # Stamp the result so sync_schematic_to_pcb can note when the schematic
        # hasn't been validated against its current content (§6.3).
        try:
            from kicad_mcp.utils.validation_cache import record_validation
            record_validation(
                p, "validate_schematic_for_pcb",
                {"passed": bool(result.get("ready_for_pcb_sync"))},
            )
        except Exception as cache_exc:
            logger.warning(
                "validate_schematic_for_pcb: validation-cache stamp failed (non-fatal): %s",
                cache_exc,
            )
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def validate_symbol_footprint_pairs(path: str) -> str:
        """Verify every symbol's Footprint field resolves and matches pad numbering.

        For each non-power component (walking sub-sheets recursively):
          - Loads the .kicad_mod that the symbol's Footprint property points at.
          - Compares the symbol's pin numbers to the footprint's pad numbers.
          - Reports mismatches (symbol pin missing from footprint = blocking),
            unresolvable footprint or symbol-library references (= blocking), and
            extras (pads with no symbol pin = warning, usually mechanical /
            thermal / NC).

        Read-only; no writes anywhere. Lists are capped at 20 entries each;
        ``over_limit`` becomes true when any list was truncated.

        Args:
            path: Path to .kicad_sch file (root sheet).

        Returns:
            JSON with passed (bool), checked (int), mismatches, unresolvable,
            warnings, over_limit.
        """
        p = validate_kicad_path(path, ".kicad_sch")
        result = run_validate_symbol_footprint_pairs(p)
        change_log.record("validate_symbol_footprint_pairs", {"path": path})
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
    def identify_edge_facing_connectors(path: str) -> str:
        """Detect connectors that need outward-facing placement at a board edge.

        Scans every footprint in the .kicad_pcb file and classifies each as
        edge-facing or not, based on:
          1. Definitive 'PCB edge' marker on the Dwgs.User layer (high confidence)
          2. Footprint name heuristic — Horizontal, _Receptacle_, Connector_USB,
             Connector_Audio, Connector_JST, Connector_BarrelJack, etc. (medium)

        Returns the mating-face direction in the footprint's local frame.
        The placed footprint's outer (at x y rotation) must be applied by
        the caller to convert to board frame — that's what
        validate_connector_orientations does.

        Use this as the survey step in /build-pcb Phase 4a — every detected
        ref needs a planned edge assignment before Phase 4b anchoring.

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with list of edge-facing connectors and their local-frame
            mating face direction (+x, -x, +y, -y), with confidence and
            evidence describing which detection signal fired.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        result = run_identify_edge_facing_connectors(p)
        change_log.record("identify_edge_facing_connectors", {"path": path})
        return json.dumps({"status": "success", **result}, indent=2)

    @mcp.tool()
    def validate_connector_orientations(path: str) -> str:
        """Placement-quality gate: every edge-facing connector points outward.

        For each connector returned by identify_edge_facing_connectors, applies
        the placed footprint's rotation to its local-frame mating face and
        checks whether the resulting board-frame direction is within ±30° of
        pointing outward at the nearest board edge.

        Connectors whose mating face could not be determined (medium-confidence
        name-matches without a usable pad centroid) are reported under
        'indeterminate' rather than failing the gate — the model should
        review them manually.

        Side effect: the result is written to <board>.validation_cache.json so
        that autoroute can refuse to start when the most recent orientation
        validation failed for the current board state.

        The model MUST NOT call autoroute if passed is false. With Phase 6.1.4
        in place, autoroute itself will refuse — but reading this first lets
        the model surface specific remediation (which refs to rotate).

        Args:
            path: Path to .kicad_pcb file.

        Returns:
            JSON with passed bool, checked count, violations list (each with
            ref, current_face_in_board_frame, closest_edge, suggested_rotation,
            angle_off_deg), and indeterminate list (refs needing manual review).
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        result = run_validate_connector_orientations(p)
        change_log.record("validate_connector_orientations", {"path": path})
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
