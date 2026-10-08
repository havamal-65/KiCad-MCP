"""Export tools - 8 tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from kicad_mcp.backends.base import BackendProtocol
from kicad_mcp.logging_config import get_logger
from kicad_mcp.utils.change_log import ChangeLog
from kicad_mcp.utils.validation import validate_kicad_path

logger = get_logger("tools.export")


# ── K10-S3 variant gate (REQ-S3F-002, REQ-S3A-001/003/005) ───────────────────

def _serving_cli_path(ops: Any) -> Path | None:
    """The kicad-cli binary this export will actually run with.

    REQ-S3F-002: key the capability gate on the ops' own resolved path, never
    a recomputed preference. ``find_kicad_cli()`` is only the fallback for an
    ops object that doesn't carry one (same resolver, same env → same answer).
    """
    cli = getattr(ops, "_cli", None)
    if isinstance(cli, Path):
        return cli
    from kicad_mcp.utils.platform_helper import find_kicad_cli
    return find_kicad_cli()


def _variant_gate(ops: Any, variant: str | list[str] | None, output: str) -> str | None:
    """Validate + capability-gate a variant-bearing export call.

    Returns a JSON error string the tool returns verbatim, or None to
    proceed. ``variant=None`` bypasses everything — those calls must keep
    working on KiCad 9 exactly as before (REQ-S3A-005). Parameter validation
    (the ``${VARIANT}`` placeholder rule) runs before any subprocess.
    """
    from kicad_mcp.models.errors import KicadCapabilityError
    from kicad_mcp.utils.kicad_capabilities import requires_kicad

    if variant is None:
        return None
    if isinstance(variant, list):
        if not variant:
            return json.dumps({
                "status": "error",
                "error": "parameter_error",
                "message": "variant list must not be empty; pass None for the default variant",
            }, indent=2)
        if "${VARIANT}" not in output:
            return json.dumps({
                "status": "error",
                "error": "parameter_error",
                "message": (
                    "Multi-variant export needs the literal ${VARIANT} placeholder in "
                    "the output path (kicad-cli expands it per variant), e.g. "
                    "'out/bom_${VARIANT}.csv'."
                ),
            }, indent=2)

    cli = _serving_cli_path(ops)
    if cli is None:
        return json.dumps({
            "status": "error",
            "error": "capability_unavailable",
            "capability": "variant-exports",
            "required": "kicad-cli 10.0+ (--variant)",
            "detected": "kicad-cli not found",
            "hint": "Install KiCad 10 and ensure kicad-cli is discoverable.",
        }, indent=2)
    try:
        requires_kicad("variant-exports", cli)
    except KicadCapabilityError as exc:
        return json.dumps({"status": "error", **exc.payload}, indent=2)
    return None


# ── §6.6 verify_3d_models — 3D model file resolution ─────────────────────────
# Pure file-read: walks every footprint's (model "…") clause, expands KiCad path
# variables, and checks the file exists on disk. Records the verdict to the
# validation cache so the §6.7 manufacturing audit can consume it. No bridge.

# .wrl <-> .step/.stp: KiCad footprints often reference one format while only the
# other is on disk (export_step vs the 3D viewer prefer different ones). Treat a
# present sibling as the model being present, not missing.
_MODEL_EXT_SIBLINGS: dict[str, list[str]] = {
    ".wrl": [".step", ".stp"],
    ".step": [".wrl"],
    ".stp": [".wrl"],
}


def _resolve_model_path(model_path: str, project_dir: Path) -> tuple[Path | None, str | None]:
    """Resolve a footprint model path to an absolute filesystem path.

    Returns ``(resolved_path, None)`` on success, or ``(None, variable_name)``
    when a ``${VAR}`` in the path cannot be expanded. ``${KIPRJMOD}``/``${PROJ_DIR}``
    resolve against *project_dir*; other variables try the environment then the
    KiCad-conventional defaults (``_default_var_value``). Relative paths with no
    variable are taken relative to *project_dir* (KiCad's ${KIPRJMOD} default).
    """
    from kicad_mcp.utils.fp_lib_table import (
        _VAR_PATTERN,
        _default_var_value,
        resolve_lib_uri,
    )

    # Pinpoint any unresolvable variable so the caller can report which to fix.
    for m in _VAR_PATTERN.finditer(model_path):
        var = m.group(1)
        if var in ("KIPRJMOD", "PROJ_DIR"):
            continue  # backed by project_dir
        if os.environ.get(var) or _default_var_value(var):
            continue
        return None, var

    resolved = resolve_lib_uri(model_path, project_dir)
    if resolved is None:
        return None, None
    if not resolved.is_absolute():
        resolved = project_dir / resolved
    return resolved, None


def _model_exists(resolved: Path) -> tuple[bool, str | None]:
    """Return ``(exists, note)``. *note* is set when the model was found only via
    a ``.wrl``/``.step`` sibling (REQ-RESOLVE-003)."""
    if resolved.exists():
        return True, None
    for alt in _MODEL_EXT_SIBLINGS.get(resolved.suffix.lower(), []):
        if resolved.with_suffix(alt).exists():
            return True, f"found via {alt} sibling"
    return False, None


def run_verify_3d_models(board_path: Path) -> dict[str, Any]:
    """Verify every placed footprint's 3D model file resolves on disk.

    Read-only. Returns ``{ready, checked, missing, warnings}`` where
    ``ready == (missing == [])``. Records ``{ready, missing_count}`` to the
    validation cache for the §6.7 manufacturing-readiness audit.
    """
    from kicad_mcp.backends.file_backend import FileBoardOps
    from kicad_mcp.utils.validation_cache import record_validation

    project_dir = board_path.parent
    components = FileBoardOps(project_dir=str(project_dir)).get_components(board_path)

    checked = 0
    missing: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for comp in components:
        ref = comp.get("reference", "?")
        fp = comp.get("footprint", "?")
        for model_path in comp.get("models", []):  # no models -> skipped (REQ-CHECK-004)
            checked += 1
            resolved, unresolved_var = _resolve_model_path(model_path, project_dir)
            if resolved is None:
                missing.append({
                    "footprint": fp, "ref": ref, "model_path": model_path,
                    "resolved_path": model_path, "reason": "unresolved_variable",
                    "variable": unresolved_var,
                })
                continue
            exists, note = _model_exists(resolved)
            if exists:
                if note:
                    warnings.append({
                        "ref": ref, "type": "extension_sibling",
                        "message": f"{ref}: {model_path} {note}",
                    })
            else:
                missing.append({
                    "footprint": fp, "ref": ref, "model_path": model_path,
                    "resolved_path": str(resolved), "reason": "file_not_found",
                })

    over_limit = len(missing) > 25
    result: dict[str, Any] = {
        "ready": not missing,
        "checked": checked,
        "missing": missing[:25],
        "warnings": warnings,
    }
    if over_limit:
        result["over_limit"] = True

    record_validation(
        board_path, "verify_3d_models",
        {"ready": result["ready"], "missing_count": len(missing)},
    )
    return result


# F-DV13 partial-STEP remedy — the exact advisory text, shared by the
# export_step tool and the manufacturing audit's partial_step advisory.
STEP_PARTIAL_REMEDY = (
    "The STEP file on disk is valid and usable; it omits the models listed in "
    "excluded_models. To get a complete export, provide a STEP/STP version of "
    "each omitted model next to its .wrl (kicad-cli --subst-models picks up "
    "same-name siblings), or accept the partial file."
)


def step_excluded_models(board_path: Path) -> list[dict[str, Any]]:
    """Models a kicad-cli STEP export drops, as best-effort advisory entries.

    A heuristic mirror of kicad-cli's own resolver (F-DV13): it informs the
    partial-export report, it never participates in the partial determination.
    Reuses the §6.6 walk (``FileBoardOps.get_components`` →
    ``_resolve_model_path`` → sibling-aware existence checks). Per excluded
    model it emits ``{ref, footprint, model_path, reason}`` where reason is:

    - ``"vrml_only"`` — the path resolves but only a ``.wrl`` exists, no
      ``.step``/``.stp`` sibling (mesh-only, unconvertible to STEP);
    - ``"model_missing"`` — unresolved variable or file not found with no
      sibling.

    Models that resolve to an existing ``.step``/``.stp`` (directly or via
    sibling) are not listed. Capped at 25 entries (advisory list).
    """
    from kicad_mcp.backends.file_backend import FileBoardOps

    project_dir = board_path.parent
    components = FileBoardOps(project_dir=str(project_dir)).get_components(board_path)

    excluded: list[dict[str, Any]] = []
    for comp in components:
        ref = comp.get("reference", "?")
        fp = comp.get("footprint", "?")
        for model_path in comp.get("models", []):
            resolved, _var = _resolve_model_path(model_path, project_dir)
            if resolved is None:
                excluded.append({"ref": ref, "footprint": fp,
                                 "model_path": model_path,
                                 "reason": "model_missing"})
                continue
            if any(resolved.with_suffix(ext).exists() for ext in (".step", ".stp")):
                continue  # a STEP-format model is present — included in the export
            if resolved.with_suffix(".wrl").exists():
                excluded.append({"ref": ref, "footprint": fp,
                                 "model_path": model_path,
                                 "reason": "vrml_only"})
            elif not resolved.exists():
                excluded.append({"ref": ref, "footprint": fp,
                                 "model_path": model_path,
                                 "reason": "model_missing"})
    return excluded[:25]


def register_tools(mcp: FastMCP, backend: BackendProtocol, change_log: ChangeLog) -> None:
    """Register export tools on the MCP server."""

    @mcp.tool()
    def export_gerbers(
        path: str,
        output_dir: str,
        layers: list[str] | None = None,
        variant: str | list[str] | None = None,
    ) -> str:
        """Export Gerber manufacturing files from a PCB board.

        Generates Gerber files for each copper and mask layer, plus drill files.
        These are the standard files needed for PCB fabrication.

        Args:
            path: Path to .kicad_pcb file.
            output_dir: Directory to write Gerber files to. For a list variant,
                must contain the literal ${VARIANT} placeholder.
            layers: Optional list of specific layers to export. Exports all enabled layers if not specified.
            variant: Design variant(s) to export (KiCad 10+). None = the default
                variant, identical to pre-variant behavior on any KiCad version.

        Returns:
            JSON with list of generated files and output directory.
        """
        p = validate_kicad_path(path, ".kicad_pcb")

        # §6.3 gate: refuse to ship fab files until DRC has passed against the
        # current board content. Prevents exporting gerbers from a board with
        # known clearance/short violations.
        from kicad_mcp.utils.gates import refuse_if_ungated
        refusal = refuse_if_ungated(
            p, "run_drc", "export_gerbers",
            fix_hint="Run run_drc(path), fix the violations, then re-export.",
        )
        if refusal is not None:
            return refusal

        out_dir = Path(output_dir)
        # A ${VARIANT}-templated dir is expanded by the backend; creating the
        # literal template dir here would leave junk on disk.
        if "${VARIANT}" not in output_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            gate_error = _variant_gate(ops, variant, output_dir)
            if gate_error is not None:
                return gate_error
            result = ops.export_gerbers(p, out_dir, layers, variant=variant)
            change_log.record("export_gerbers", {"path": path, "output_dir": output_dir,
                                                 "variant": variant})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Gerber export failed: {e}. Requires kicad-cli or SWIG backend.",
            })

    @mcp.tool()
    def export_drill(path: str, output_dir: str) -> str:
        """Export drill files (Excellon format) from a PCB board.

        Args:
            path: Path to .kicad_pcb file.
            output_dir: Directory to write drill files to.

        Returns:
            JSON with list of generated drill files.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_drill(p, out_dir)
            change_log.record("export_drill", {"path": path, "output_dir": output_dir})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Drill export failed: {e}. Requires kicad-cli or SWIG backend.",
            })

    @mcp.tool()
    def export_bom(
        path: str,
        output: str,
        format: str = "csv",
        variant: str | list[str] | None = None,
        exclude_dnp: bool = False,
    ) -> str:
        """Export Bill of Materials from a board or schematic.

        Args:
            path: Path to .kicad_pcb or .kicad_sch file.
            output: Output file path. For a list variant, must contain the
                literal ${VARIANT} placeholder (expanded per variant).
            format: Output format - 'csv', 'json', 'xml', or 'html'.
            variant: Design variant(s) to export (KiCad 10+). None = the default
                variant, identical to pre-variant behavior on any KiCad version.
            exclude_dnp: Exclude do-not-populate components from the BOM.

        Returns:
            JSON with export result and output file path(s).
        """
        p = validate_kicad_path(path)
        out = Path(output)

        valid_formats = {"csv", "json", "xml", "html"}
        if format not in valid_formats:
            return json.dumps({
                "status": "error",
                "message": f"Invalid format: {format}. Must be one of: {valid_formats}",
            })

        try:
            if p.suffix == ".kicad_pcb":
                backend.save_board(p)
            ops = backend.get_export_ops()
            gate_error = _variant_gate(ops, variant, output)
            if gate_error is not None:
                return gate_error
            result = ops.export_bom(p, out, format, variant=variant, exclude_dnp=exclude_dnp)
            change_log.record("export_bom", {"path": path, "output": output, "format": format,
                                             "variant": variant, "exclude_dnp": exclude_dnp})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"BOM export failed: {e}. Requires kicad-cli or SWIG backend.",
            })

    @mcp.tool()
    def export_pick_and_place(
        path: str,
        output: str,
        variant: str | list[str] | None = None,
        exclude_dnp: bool = False,
    ) -> str:
        """Export pick-and-place (component placement) file for assembly.

        Args:
            path: Path to .kicad_pcb file.
            output: Output CSV file path. For a list variant, must contain the
                literal ${VARIANT} placeholder (expanded per variant).
            variant: Design variant(s) to export (KiCad 10+). None = the default
                variant, identical to pre-variant behavior on any KiCad version.
            exclude_dnp: Exclude do-not-populate components from the placement file.

        Returns:
            JSON with export result.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out = Path(output)

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            gate_error = _variant_gate(ops, variant, output)
            if gate_error is not None:
                return gate_error
            result = ops.export_pick_and_place(p, out, variant=variant, exclude_dnp=exclude_dnp)
            change_log.record("export_pick_and_place", {"path": path, "output": output,
                                                        "variant": variant,
                                                        "exclude_dnp": exclude_dnp})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Pick-and-place export failed: {e}. Requires kicad-cli backend.",
            })

    @mcp.tool()
    def export_pdf(
        path: str,
        output: str,
        layers: list[str] | None = None,
        variant: str | list[str] | None = None,
    ) -> str:
        """Export a board or schematic to PDF.

        Requires kicad-cli. If kicad-cli is not on PATH the error message will
        include the exact path that was searched so you can diagnose the problem.
        After export, verifies the output file exists — a missing file is an error
        even when kicad-cli returns exit code 0.

        Args:
            path: Path to .kicad_pcb or .kicad_sch file.
            output: Output PDF file path. For a list variant, must contain the
                literal ${VARIANT} placeholder (expanded per variant).
            layers: For board files, optional list of layers to include.
            variant: Design variant(s) to export — schematic inputs only
                (KiCad 10+; kicad-cli pcb export pdf has no variant flag).
                None = the default variant, identical to pre-variant behavior.

        Returns:
            JSON with export result and output file path.
        """
        from kicad_mcp.utils.platform_helper import find_kicad_cli
        import shutil as _shutil

        p = validate_kicad_path(path)
        out = Path(output)

        if variant is not None and p.suffix == ".kicad_pcb":
            return json.dumps({
                "status": "error",
                "error": "parameter_error",
                "message": (
                    "variant is only supported for schematic PDF export "
                    "(kicad-cli pcb export pdf has no --variant flag). "
                    "Pass the .kicad_sch path, or drop the variant parameter."
                ),
            }, indent=2)

        # Verify kicad-cli is available before attempting export
        cli_path = _shutil.which("kicad-cli")
        if not cli_path:
            cli = find_kicad_cli()
            cli_path = str(cli) if cli else None
        if not cli_path:
            return json.dumps({
                "status": "error",
                "message": (
                    "export_pdf requires kicad-cli, which was not found on PATH or in "
                    "standard KiCad installation directories. "
                    "Install KiCad and ensure its bin/ directory is on PATH, or set "
                    "KICAD_MCP_CLI_PATH environment variable to the kicad-cli executable."
                ),
            }, indent=2)

        try:
            if p.suffix == ".kicad_pcb":
                backend.save_board(p)
            ops = backend.get_export_ops()
            gate_error = _variant_gate(ops, variant, output)
            if gate_error is not None:
                return gate_error
            result = ops.export_pdf(p, out, layers, variant=variant)

            # Surface failures: check exit code result and file existence
            if not result.get("success", True):
                cmd_hint = (
                    f"kicad-cli {'sch' if p.suffix == '.kicad_sch' else 'pcb'} export pdf "
                    f"--output {out} {p}"
                )
                return json.dumps({
                    "status": "error",
                    "message": f"PDF export failed. stderr: {result.get('message', 'no output')}",
                    "command_attempted": cmd_hint,
                    "kicad_cli_path": cli_path,
                }, indent=2)

            # Even a zero-exit kicad-cli sometimes produces no file (wrong path,
            # permission issue). Multi-variant calls check every expanded path.
            expected_files = result.get("output_files") or [str(out)]
            missing_files = [f for f in expected_files if not Path(f).exists()]
            if missing_files:
                return json.dumps({
                    "status": "error",
                    "message": (
                        "kicad-cli reported success but output file(s) were not created: "
                        f"{', '.join(missing_files)}. "
                        "Check that the output directory is writable and the path is correct."
                    ),
                    "output_path": str(out),
                    "missing_files": missing_files,
                }, indent=2)

            change_log.record("export_pdf", {"path": path, "output": output,
                                             "variant": variant})
            return json.dumps({"status": "success", **result}, indent=2)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": (
                    "PDF export is not implemented by the current backend. "
                    "Ensure kicad-cli is installed and on PATH."
                ),
            }, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"PDF export failed: {e}",
                "kicad_cli_path": cli_path,
            }, indent=2)

    @mcp.tool()
    def render_view(
        path: str,
        output: str | None = None,
        side: str = "top",
        width: int = 1600,
        height: int = 900,
        zoom: float | None = None,
        rotate: str | None = None,
    ) -> str:
        """Render a board or schematic to an image for visual inspection.

        Boards (.kicad_pcb) render to a PNG via kicad-cli pcb render (3D view,
        top side by default); schematics (.kicad_sch) export to a PDF (kicad-cli
        has no schematic PNG). Both outputs can be read back by an AI agent or
        opened by the user — use this to actually look at what you built.
        Board renders save the live board state to disk first, so the image
        always shows the current design, not a stale file.

        Args:
            path: Path to .kicad_pcb or .kicad_sch file.
            output: Output image path. Default: <input stem>_render.png (board)
                or _render.pdf (schematic) beside the input file.
            side: Board camera side: top, bottom, left, right, front, back.
            width: Board image width in pixels (64-8192).
            height: Board image height in pixels (64-8192).
            zoom: Optional board camera zoom factor.
            rotate: Optional board rotation 'X,Y,Z' in degrees,
                e.g. '-45,0,45' for an isometric view.

        Returns:
            JSON with the rendered file path and format.
        """
        from kicad_mcp.utils.platform_helper import find_kicad_cli
        import shutil as _shutil

        p = validate_kicad_path(path)

        if p.suffix not in (".kicad_pcb", ".kicad_sch"):
            return json.dumps({
                "status": "error",
                "error": "parameter_error",
                "message": "render_view accepts .kicad_pcb or .kicad_sch files",
            }, indent=2)

        is_board = p.suffix == ".kicad_pcb"
        board_params_customized = (
            side != "top" or width != 1600 or height != 900
            or zoom is not None or rotate is not None
        )
        if not is_board and board_params_customized:
            return json.dumps({
                "status": "error",
                "error": "parameter_error",
                "message": (
                    "side/width/height/zoom/rotate apply to board renders only; "
                    "schematic renders are PDF pages"
                ),
            }, indent=2)
        if side not in ("top", "bottom", "left", "right", "front", "back"):
            return json.dumps({
                "status": "error",
                "error": "parameter_error",
                "message": "side must be one of: top, bottom, left, right, front, back",
            }, indent=2)
        if not (64 <= width <= 8192) or not (64 <= height <= 8192):
            return json.dumps({
                "status": "error",
                "error": "parameter_error",
                "message": "width and height must be between 64 and 8192 pixels",
            }, indent=2)

        if output is not None:
            out = Path(output)
        else:
            out = p.parent / f"{p.stem}_render.{'png' if is_board else 'pdf'}"

        cli_path = _shutil.which("kicad-cli")
        if not cli_path:
            cli = find_kicad_cli()
            cli_path = str(cli) if cli else None
        if not cli_path:
            return json.dumps({
                "status": "error",
                "message": (
                    "render_view requires kicad-cli, which was not found on PATH or in "
                    "standard KiCad installation directories. "
                    "Install KiCad and ensure its bin/ directory is on PATH, or set "
                    "KICAD_MCP_CLI_PATH environment variable to the kicad-cli executable."
                ),
            }, indent=2)

        try:
            if is_board:
                backend.save_board(p)
            ops = backend.get_export_ops()
            if is_board:
                result = ops.render_board(
                    p, out, side=side, width=width, height=height,
                    zoom=zoom, rotate=rotate,
                )
            else:
                result = ops.export_pdf(p, out, None, variant=None)

            if not result.get("success", True):
                cmd_hint = (
                    f"kicad-cli pcb render --output {out} {p}"
                    if is_board else
                    f"kicad-cli sch export pdf --output {out} {p}"
                )
                return json.dumps({
                    "status": "error",
                    "message": f"Render failed. stderr: {result.get('message', 'no output')}",
                    "command_attempted": cmd_hint,
                    "kicad_cli_path": cli_path,
                }, indent=2)

            if not out.exists():
                return json.dumps({
                    "status": "error",
                    "message": (
                        "kicad-cli reported success but output file was not created: "
                        f"{out}. Check that the output directory is writable and the "
                        "path is correct."
                    ),
                    "output_path": str(out),
                }, indent=2)

            change_log.record("render_view", {"path": path, "output": str(out),
                                              "side": side})
            return json.dumps({
                "status": "success",
                "kind": "board" if is_board else "schematic",
                "format": "png" if is_board else "pdf",
                "output_file": str(out.resolve()),
                "message": "Rendered. Read the output file to inspect it visually.",
            }, indent=2)
        except NotImplementedError:
            return json.dumps({
                "status": "error",
                "message": (
                    "Board rendering is not implemented by the current backend. "
                    "Ensure kicad-cli is installed and on PATH."
                ),
            }, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Render failed: {e}",
                "kicad_cli_path": cli_path,
            }, indent=2)

    @mcp.tool()
    def export_step(path: str, output: str | None = None) -> str:
        """Export a 3D STEP model from a PCB board.

        Generates a STEP file suitable for mechanical integration and 3D viewer import.
        Requires kicad-cli (installed with KiCad).

        Status is three-way: "success" (clean export), "partial" (kicad-cli
        exited nonzero — e.g. VRML-only 3D models it cannot convert — but
        wrote a valid STEP file; the payload adds excluded_models and a
        remedy), or "error" (no usable STEP file produced).

        Args:
            path: Path to .kicad_pcb file.
            output: Output .step file path. Defaults to same directory as board with .step extension.

        Returns:
            JSON with export result and output file path. A partial result
            carries partial: true, file_size_bytes, stderr, excluded_models
            [{ref, footprint, model_path, reason}], and remedy.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out = Path(output) if output else p.with_suffix(".step")

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_step(p, out)
            if result.get("success"):
                change_log.record("export_step", {"path": path, "output": str(out)})
                return json.dumps({"status": "success", **result}, indent=2)
            if result.get("partial"):
                payload: dict[str, Any] = {"status": "partial", **result}
                # Best-effort advisory — a failing walk never fails the report.
                try:
                    payload["excluded_models"] = step_excluded_models(p)
                except Exception as exc:  # noqa: BLE001 — advisory must not raise
                    payload["excluded_models"] = []
                    payload["excluded_models_error"] = str(exc)
                payload["remedy"] = STEP_PARTIAL_REMEDY
                # An artifact was produced — record partial like success.
                change_log.record("export_step", {"path": path, "output": str(out)})
                return json.dumps(payload, indent=2)
            return json.dumps({"status": "error", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"STEP export failed: {e}. Requires kicad-cli backend.",
            })

    @mcp.tool()
    def verify_3d_models(board_path: str) -> str:
        """Verify every footprint's 3D model file resolves on disk.

        Walks each placed footprint's (model "…") clause, expands KiCad path
        variables (${KICAD9_3DMODEL_DIR}, ${KIPRJMOD}, …), and checks the file
        exists — with a .wrl<->.step sibling fallback. Read-only; a pre-flight
        for export_step and the manufacturing-readiness audit. Records its
        verdict to the validation cache.

        Args:
            board_path: Path to .kicad_pcb file.

        Returns:
            JSON: {ready, checked, missing:[{footprint,ref,model_path,resolved_path,reason}], warnings}.
            ready is true iff missing is empty. reason is "file_not_found" or
            "unresolved_variable".
        """
        p = validate_kicad_path(board_path, ".kicad_pcb")
        return json.dumps(run_verify_3d_models(p), indent=2)

    @mcp.tool()
    def export_vrml(path: str, output: str | None = None) -> str:
        """Export a 3D VRML model from a PCB board.

        Generates a VRML file for 3D rendering and simulation tools.
        Requires kicad-cli (installed with KiCad).

        Args:
            path: Path to .kicad_pcb file.
            output: Output .wrl file path. Defaults to same directory as board with .wrl extension.

        Returns:
            JSON with export result and output file path.
        """
        p = validate_kicad_path(path, ".kicad_pcb")
        out = Path(output) if output else p.with_suffix(".wrl")

        try:
            backend.save_board(p)
            ops = backend.get_export_ops()
            result = ops.export_vrml(p, out)
            change_log.record("export_vrml", {"path": path, "output": str(out)})
            return json.dumps({"status": "success", **result}, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"VRML export failed: {e}. Requires kicad-cli backend.",
            })
