"""F-DV8 (DV build) — import_footprint must carry ${KIPRJMOD} 3D assets.

A project-local footprint may reference its 3D model as
``${KIPRJMOD}/3d_shapes/<name>.wrl`` (the ecc83 demo's valve does). A bare
``.kicad_mod`` copy leaves that reference dangling in the target project, so
renders and STEP exports silently lose the body.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileLibraryManageOps
from kicad_mcp.models.errors import LibraryImportError

# Model clauses copied verbatim from the ecc83 demo's footprints.pretty
# (Valve_ECC-83-1 for the KIPRJMOD form, C_Disc for the global-var form).
_KIPRJMOD_MOD = """(footprint "Valve_Fixture"
\t(layer "F.Cu")
\t(model "${KIPRJMOD}/3d_shapes/ecc83.wrl"
\t\t(offset
\t\t\t(xyz 0 0 0)
\t\t)
\t)
)
"""

_GLOBAL_MOD = """(footprint "Disc_Fixture"
\t(layer "F.Cu")
\t(model "${KICAD6_3DMODEL_DIR}/Capacitor_THT.3dshapes/C_Disc.wrl"
\t\t(offset
\t\t\t(xyz 0 0 0)
\t\t)
\t)
)
"""


def _make_projects(tmp_path: Path, mod_text: str, with_asset: bool = True) -> tuple[Path, Path]:
    src_project = tmp_path / "src_project"
    src_lib = src_project / "footprints.pretty"
    src_lib.mkdir(parents=True)
    (src_lib / "FP.kicad_mod").write_text(mod_text, encoding="utf-8")
    if with_asset:
        shapes = src_project / "3d_shapes"
        shapes.mkdir()
        (shapes / "ecc83.wrl").write_text("#VRML V2.0 utf8\n", encoding="utf-8")
    tgt_project = tmp_path / "tgt_project"
    tgt_lib = tgt_project / "Footprints.pretty"
    tgt_lib.mkdir(parents=True)
    return src_lib, tgt_lib


def test_kiprjmod_asset_copied_into_target_project(tmp_path: Path) -> None:
    src_lib, tgt_lib = _make_projects(tmp_path, _KIPRJMOD_MOD)
    result = FileLibraryManageOps().import_footprint(str(src_lib), "FP", str(tgt_lib))
    copied_asset = tgt_lib.parent / "3d_shapes" / "ecc83.wrl"
    assert copied_asset.exists()
    assert result["model_assets_copied"] == [str(copied_asset)]
    assert "warnings" not in result


def test_global_model_var_not_copied(tmp_path: Path) -> None:
    src_lib, tgt_lib = _make_projects(tmp_path, _GLOBAL_MOD, with_asset=False)
    result = FileLibraryManageOps().import_footprint(str(src_lib), "FP", str(tgt_lib))
    assert result["model_assets_copied"] == []
    assert "warnings" not in result
    assert not (tgt_lib.parent / "Capacitor_THT.3dshapes").exists()


def test_dangling_source_asset_warns_but_succeeds(tmp_path: Path) -> None:
    src_lib, tgt_lib = _make_projects(tmp_path, _KIPRJMOD_MOD, with_asset=False)
    result = FileLibraryManageOps().import_footprint(str(src_lib), "FP", str(tgt_lib))
    assert (tgt_lib / "FP.kicad_mod").exists()
    assert result["model_assets_copied"] == []
    assert len(result["warnings"]) == 1
    assert "${KIPRJMOD}/3d_shapes/ecc83.wrl" in result["warnings"][0]


def test_existing_target_asset_not_clobbered(tmp_path: Path) -> None:
    src_lib, tgt_lib = _make_projects(tmp_path, _KIPRJMOD_MOD)
    tgt_shapes = tgt_lib.parent / "3d_shapes"
    tgt_shapes.mkdir()
    (tgt_shapes / "ecc83.wrl").write_text("existing-target-content", encoding="utf-8")
    result = FileLibraryManageOps().import_footprint(str(src_lib), "FP", str(tgt_lib))
    assert result["model_assets_copied"] == []
    text = (tgt_shapes / "ecc83.wrl").read_text(encoding="utf-8")
    assert text == "existing-target-content"


def test_overwrite_replaces_mod_file_default_still_refuses(tmp_path: Path) -> None:
    src_lib, tgt_lib = _make_projects(tmp_path, _KIPRJMOD_MOD)
    ops = FileLibraryManageOps()
    ops.import_footprint(str(src_lib), "FP", str(tgt_lib))

    with pytest.raises(LibraryImportError):
        ops.import_footprint(str(src_lib), "FP", str(tgt_lib))

    (src_lib / "FP.kicad_mod").write_text(
        _KIPRJMOD_MOD.replace('"F.Cu"', '"B.Cu"'), encoding="utf-8")
    result = ops.import_footprint(str(src_lib), "FP", str(tgt_lib), overwrite=True)
    assert result["overwritten"] is True
    assert '"B.Cu"' in (tgt_lib / "FP.kicad_mod").read_text(encoding="utf-8")
