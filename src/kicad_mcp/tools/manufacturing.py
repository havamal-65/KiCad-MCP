"""Manufacturing-readiness audit — 1 tool (§6.7).

One orchestration tool that runs the board's pre-fab **checks** (DRC,
board-size, courtyard-overlap, 3D-model resolution) and produces its five
**artifacts** (Gerbers, drill, BOM, pick-and-place, STEP) in a single call,
returning a structured ``ready_to_ship`` verdict.

It writes no new fab logic: it consumes the importable §6.5/§6.6 impls
(``run_verify_board_size``, ``run_verify_3d_models``,
``run_check_courtyard_overlaps``) plus the same backend DRC/export ops the
standalone export tools call (``get_drc_ops``/``get_export_ops``) — no kicad-cli
re-implementation (REQ-ROLLUP-*).

Artifact generation is gated on DRC passing, reproducing §6.3's
``export_gerbers``←``run_drc`` refuse-gate intent by ordering (REQ-VERDICT-003).
A missing 3D model is an advisory, never blocking (Q7-d).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.manufacturing")

# Board-size warning types surfaced as advisories (never blocking, REQ-VERDICT-004).
_BOARD_SIZE_ADVISORY_TYPES = ("high_utilization", "no_courtyard")

# The five AC5 artifacts, in export order.
_ARTIFACT_NAMES = ("gerbers", "drill", "bom", "pos", "step")


def run_manufacturing_readiness_audit(
    backend: BackendProtocol,
    board_path: Path,
    output_dir: Path,
    *,
    skip_artifacts: bool = False,
    panel_keepout_mm: float = 3.0,
    mounting_hole_keepout_mm: float = 3.0,
    fiducial_keepout_mm: float = 1.0,
    routing_channel_pct: float = 20.0,
) -> dict[str, Any]:
    """Run the four pre-fab checks and (unless gated) produce the five fab
    artifacts, returning a single ``ready_to_ship`` verdict.

    Returns ``{ready_to_ship, checks, artifacts, blocking_issues, advisories}``.
    Each check/artifact/export is isolated: a raise is caught and recorded as a
    failure + blocking issue without aborting the rest (REQ-CHECK-005/REQ-ART-006).
    """
    from kicad_mcp.tools.board import run_verify_board_size
    from kicad_mcp.tools.drc import run_check_courtyard_overlaps
    from kicad_mcp.tools.export import run_verify_3d_models

    tol = {
        "panel_keepout_mm": panel_keepout_mm,
        "mounting_hole_keepout_mm": mounting_hole_keepout_mm,
        "fiducial_keepout_mm": fiducial_keepout_mm,
        "routing_channel_pct": routing_channel_pct,
    }

    checks: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []

    def _check(name: str, fn: Callable[[], Any]) -> Any:
        """Run a check in isolation (REQ-CHECK-005). On raise, record a failed
        check + blocking issue and return None so the caller skips its specifics."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — isolation is the requirement
            logger.warning("manufacturing audit: check %s raised: %s", name, exc)
            checks.append({"name": name, "passed": False, "detail": {"error": str(exc)}})
            blocking.append({"check": name, "reason": f"{name} raised",
                             "detail": {"error": str(exc)}})
            return None

    # ── 1. DRC (gates artifacts) ─────────────────────────────────────────────
    drc = _check("drc", lambda: backend.get_drc_ops().run_drc(board_path, None))
    drc_passed = bool(drc and drc.get("passed"))
    if drc is not None:
        checks.append({
            "name": "drc",
            "passed": drc_passed,
            "detail": {
                "errors": drc.get("error_count"),
                "warnings": drc.get("warning_count"),
                "violations": drc.get("violations", []),
            },
        })
        if not drc_passed:
            blocking.append({
                "check": "drc",
                "reason": "DRC errors — fix before exporting fab files",
                "detail": {"errors": drc.get("error_count")},
            })

    # ── 2. board-size (§6.5) ─────────────────────────────────────────────────
    bs = _check("board_size", lambda: run_verify_board_size(board_path, **tol))
    if bs is not None:
        checks.append({"name": "board_size", "passed": bs["passed"],
                       "detail": bs.get("shortfall_breakdown", {})})
        if not bs["passed"]:
            blocking.append({
                "check": "board_size",
                "reason": "Board too small for placed parts + tolerances",
                "detail": {
                    "shortfall_breakdown": bs.get("shortfall_breakdown"),
                    "suggested_min_dimensions": bs.get("suggested_min_dimensions"),
                },
            })
        advisories.extend(
            {"type": w.get("type"), "detail": w}
            for w in bs.get("warnings", [])
            if w.get("type") in _BOARD_SIZE_ADVISORY_TYPES
        )

    # ── 3. courtyard overlaps ────────────────────────────────────────────────
    co = _check("courtyard_overlaps", lambda: run_check_courtyard_overlaps(board_path))
    if co is not None:
        checks.append({"name": "courtyard_overlaps", "passed": co["passed"],
                       "detail": {"overlaps": co.get("overlaps", [])}})
        if not co["passed"]:
            blocking.append({
                "check": "courtyard_overlaps",
                "reason": "Footprint courtyards overlap",
                "detail": {"overlaps": co.get("overlaps")},
            })

    # ── 4. 3D models (ADVISORY, never blocking) — Q7-d ───────────────────────
    m3d = _check("verify_3d_models", lambda: run_verify_3d_models(board_path))
    if m3d is not None:
        missing = m3d.get("missing", [])
        checks.append({
            "name": "verify_3d_models",
            "passed": m3d.get("ready", True),
            "detail": {"missing_count": len(missing)},
        })
        for miss in missing:
            advisories.append({"type": "missing_3d_model", "detail": miss})

    # ── 5. artifacts — gated on DRC (REQ-VERDICT-003) ────────────────────────
    if skip_artifacts:
        blocking.append({
            "artifact": "*",
            "reason": "skip_artifacts=True — no fab files produced",
            "detail": {},
        })
    elif not drc_passed:
        for name in _ARTIFACT_NAMES:
            artifacts.append({"name": name, "generated": False,
                              "detail": {"skipped": "drc_failed"}})
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        export = backend.get_export_ops()

        def _artifact(name: str, fn: Callable[[], Any],
                      surface_key: str, result_field: str) -> None:
            """Run one export in isolation (REQ-ART-006), recording
            ``{name, generated, <surface_key>, detail?}`` and a blocking issue on
            failure/raise."""
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001 — isolation is the requirement
                logger.warning("manufacturing audit: export %s raised: %s", name, exc)
                artifacts.append({"name": name, "generated": False,
                                  "detail": {"error": str(exc)}})
                blocking.append({"artifact": name, "reason": f"{name} export raised",
                                 "detail": {"error": str(exc)}})
                return
            ok = bool(res.get("success"))
            if result_field == "_first_file":
                files = res.get("output_files") or []
                value: Any = files[0] if files else None
            else:
                value = res.get(result_field)
            entry: dict[str, Any] = {"name": name, "generated": ok, surface_key: value}
            if not ok:
                entry["detail"] = {"message": res.get("message")}
                blocking.append({"artifact": name, "reason": f"{name} export failed",
                                 "detail": {"message": res.get("message")}})
            artifacts.append(entry)

        _artifact("gerbers", lambda: export.export_gerbers(board_path, output_dir / "gerbers", None),
                  "files", "output_files")
        _artifact("drill", lambda: export.export_drill(board_path, output_dir / "drill"),
                  "output_path", "output_dir")
        # BOM is generated from the schematic (kicad-cli `sch export bom`); the
        # sibling .kicad_sch is the canonical source — the board path is not.
        sch_path = board_path.with_suffix(".kicad_sch")
        bom_source = sch_path if sch_path.exists() else board_path
        _artifact("bom", lambda: export.export_bom(bom_source, output_dir / "bom.csv", "csv"),
                  "output_path", "_first_file")
        _artifact("pos", lambda: export.export_pick_and_place(board_path, output_dir / "pos.csv"),
                  "output_path", "_first_file")
        _artifact("step", lambda: export.export_step(board_path, output_dir / "model.step"),
                  "output_path", "output_file")

    # ── verdict (REQ-VERDICT-001/004) ────────────────────────────────────────
    # 3D-models is advisory → excluded from the check-pass conjunction.
    ready = (
        not blocking
        and all(c["passed"] for c in checks if c["name"] != "verify_3d_models")
        and skip_artifacts is False
        and all(a["generated"] for a in artifacts)
    )

    return {
        "ready_to_ship": ready,
        "checks": checks,
        "artifacts": artifacts,
        "blocking_issues": blocking,
        "advisories": advisories,
    }


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register the manufacturing-readiness audit tool on the MCP server."""

    @mcp.tool()
    def manufacturing_readiness_audit(
        board_path: str,
        output_dir: str,
        skip_artifacts: bool = False,
        panel_keepout_mm: float = 3.0,
        mounting_hole_keepout_mm: float = 3.0,
        fiducial_keepout_mm: float = 1.0,
        routing_channel_pct: float = 20.0,
    ) -> str:
        """Run a one-call manufacturing-readiness audit: all pre-fab checks plus
        the five fab artifacts, returning a structured ship/no-ship verdict.

        Runs DRC, board-size verification (§6.5), courtyard-overlap, and 3D-model
        resolution (§6.6), then — only if DRC passes — exports Gerbers, drill,
        BOM, pick-and-place, and STEP into ``output_dir``. A missing 3D model is
        an advisory, not a blocker. Reuses the same backend DRC/export ops as the
        standalone tools; adds no new write surface.

        Args:
            board_path: Path to the .kicad_pcb file.
            output_dir: Directory the fab artifacts are written under
                (gerbers/, drill/, bom.csv, pos.csv, model.step).
            skip_artifacts: Run checks only (fast pre-flight). Artifacts are not
                produced and ready_to_ship is false.
            panel_keepout_mm: Board-size tolerance — panel edge keepout.
            mounting_hole_keepout_mm: Board-size tolerance — mounting-hole keepout.
            fiducial_keepout_mm: Board-size tolerance — fiducial keepout.
            routing_channel_pct: Board-size tolerance — routing-channel headroom %.

        Returns:
            JSON: {ready_to_ship, checks, artifacts, blocking_issues, advisories}.
        """
        p = validate_kicad_path(board_path, ".kicad_pcb")
        result = run_manufacturing_readiness_audit(
            backend, p, Path(output_dir),
            skip_artifacts=skip_artifacts,
            panel_keepout_mm=panel_keepout_mm,
            mounting_hole_keepout_mm=mounting_hole_keepout_mm,
            fiducial_keepout_mm=fiducial_keepout_mm,
            routing_channel_pct=routing_channel_pct,
        )
        change_log.record("manufacturing_readiness_audit",
                          {"board_path": board_path, "output_dir": output_dir,
                           "ready_to_ship": result["ready_to_ship"]})
        return json.dumps(result, indent=2)
