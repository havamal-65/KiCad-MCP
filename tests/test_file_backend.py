"""Tests for the file-parsing backend."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import (
    FileBackend,
    FileBoardOps,
    FileSchematicOps,
    _load_kicad_mod,
)

FIXTURES_FP = Path(__file__).parent / "fixtures" / "footprints"


@pytest.fixture
def file_backend() -> FileBackend:
    return FileBackend()


@pytest.fixture
def board_ops() -> FileBoardOps:
    return FileBoardOps()


@pytest.fixture
def schematic_ops() -> FileSchematicOps:
    return FileSchematicOps()


class TestFileBackend:
    def test_always_available(self, file_backend: FileBackend):
        assert file_backend.is_available()

    def test_name(self, file_backend: FileBackend):
        assert file_backend.name == "file"

    def test_has_board_read(self, file_backend: FileBackend):
        from kicad_mcp.backends.base import BackendCapability
        assert BackendCapability.BOARD_READ in file_backend.capabilities

    def test_has_schematic_read(self, file_backend: FileBackend):
        from kicad_mcp.backends.base import BackendCapability
        assert BackendCapability.SCHEMATIC_READ in file_backend.capabilities

    def test_get_board_ops(self, file_backend: FileBackend):
        ops = file_backend.get_board_ops()
        assert ops is not None
        assert isinstance(ops, FileBoardOps)

    def test_get_schematic_ops(self, file_backend: FileBackend):
        ops = file_backend.get_schematic_ops()
        assert ops is not None
        assert isinstance(ops, FileSchematicOps)


class TestFileBoardOps:
    def test_read_board(self, board_ops: FileBoardOps, sample_board_path: Path):
        result = board_ops.read_board(sample_board_path)
        assert "info" in result
        assert "components" in result
        assert "nets" in result
        assert "tracks" in result

    def test_get_components(self, board_ops: FileBoardOps, sample_board_path: Path):
        components = board_ops.get_components(sample_board_path)
        assert len(components) == 3
        refs = {c.get("reference") for c in components}
        assert "R1" in refs
        assert "R2" in refs
        assert "U1" in refs

    def test_get_nets(self, board_ops: FileBoardOps, sample_board_path: Path):
        nets = board_ops.get_nets(sample_board_path)
        assert len(nets) == 9  # nets 0-8 as declared in the fixture
        net_names = {n["name"] for n in nets}
        assert "VCC" in net_names
        assert "GND" in net_names

    def test_get_tracks(self, board_ops: FileBoardOps, sample_board_path: Path):
        tracks = board_ops.get_tracks(sample_board_path)
        assert len(tracks) == 3
        assert tracks[0]["width"] == 0.25

    def test_get_board_info(self, board_ops: FileBoardOps, sample_board_path: Path):
        info = board_ops.get_board_info(sample_board_path)
        assert info["title"] == "Sample Board"
        assert info["revision"] == "1.0"
        assert "num_components" in info
        assert "num_nets" in info

    def test_get_design_rules(self, board_ops: FileBoardOps, sample_board_path: Path):
        rules = board_ops.get_design_rules(sample_board_path)
        assert isinstance(rules, dict)

    def test_set_board_design_rules_keeps_invalid_rule_keys_out_of_pcb(
        self,
        board_ops: FileBoardOps,
        tmp_path: Path,
    ):
        pcb_path = tmp_path / "rules_board.kicad_pcb"
        pro_path = tmp_path / "rules_board.kicad_pro"

        board_ops.create_board(pcb_path, title="Rules Board", revision="1.0")
        pro_path.write_text("{}", encoding="utf-8")

        result = board_ops.set_board_design_rules(pcb_path, "fab_jlcpcb")

        assert result["preset"] == "fab_jlcpcb"

        project_rules = json.loads(pro_path.read_text(encoding="utf-8"))["board"]["design_settings"]["rules"]
        assert project_rules["min_via_diameter"] == pytest.approx(0.45)
        assert project_rules["min_through_hole_diameter"] == pytest.approx(0.2)
        assert project_rules["min_hole_clearance"] == pytest.approx(0.22)

        pcb_text = pcb_path.read_text(encoding="utf-8")
        assert "via_min_size" not in pcb_text
        assert "via_min_drill" not in pcb_text
        assert "hole_clearance" not in pcb_text


class TestFileSchematicOps:
    def test_read_schematic(self, schematic_ops: FileSchematicOps, sample_schematic_path: Path):
        result = schematic_ops.read_schematic(sample_schematic_path)
        assert "info" in result
        assert "symbols" in result
        assert "wires" in result
        assert "labels" in result

    def test_get_symbols(self, schematic_ops: FileSchematicOps, sample_schematic_path: Path):
        symbols = schematic_ops.get_symbols(sample_schematic_path)
        assert len(symbols) >= 2
        refs = {s.get("reference") for s in symbols}
        assert "R1" in refs
        assert "U1" in refs

    def test_schematic_info(self, schematic_ops: FileSchematicOps, sample_schematic_path: Path):
        result = schematic_ops.read_schematic(sample_schematic_path)
        info = result["info"]
        assert info["num_symbols"] >= 2
        assert info["num_wires"] >= 2

    def test_add_wire(self, schematic_ops: FileSchematicOps, tmp_path: Path, sample_schematic_path: Path):
        # Copy fixture to temp location for modification
        import shutil
        tmp_sch = tmp_path / "test.kicad_sch"
        shutil.copy2(str(sample_schematic_path), str(tmp_sch))

        result = schematic_ops.add_wire(tmp_sch, 10, 20, 30, 40)
        assert result["start"]["x"] == 10
        assert result["end"]["x"] == 30

        # Verify the wire was added to the file
        content = tmp_sch.read_text(encoding="utf-8")
        assert "10 20" in content and "30 40" in content

    def test_add_label(self, schematic_ops: FileSchematicOps, tmp_path: Path, sample_schematic_path: Path):
        import shutil
        tmp_sch = tmp_path / "test.kicad_sch"
        shutil.copy2(str(sample_schematic_path), str(tmp_sch))

        result = schematic_ops.add_label(tmp_sch, "TEST_NET", 50, 60)
        assert result["text"] == "TEST_NET"


# ---------------------------------------------------------------------------
# place_component with real .kicad_mod fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fp_libs(monkeypatch):
    """Patch find_footprint_libraries to return only the bundled test fixtures."""
    import kicad_mcp.utils.kicad_paths as kp
    monkeypatch.setattr(kp, "find_footprint_libraries", lambda: list(FIXTURES_FP.iterdir()))


@pytest.fixture
def tmp_board(tmp_path: Path, sample_board_path: Path) -> Path:
    """Fresh copy of the sample board in a temp directory."""
    dst = tmp_path / "board.kicad_pcb"
    shutil.copy2(sample_board_path, dst)
    return dst


class TestPlaceComponentReal:
    def test_load_kicad_mod_finds_resistor(self, fp_libs):
        content = _load_kicad_mod("Resistor_SMD:R_0805_2012Metric")
        assert content is not None
        assert "(pad" in content

    def test_load_kicad_mod_finds_sot23(self, fp_libs):
        content = _load_kicad_mod("Package_TO_SOT_SMD:SOT-23")
        assert content is not None
        assert "(pad" in content

    def test_load_kicad_mod_unknown_returns_none(self, fp_libs):
        assert _load_kicad_mod("Resistor_SMD:DoesNotExist") is None

    def test_load_kicad_mod_no_colon_returns_none(self, fp_libs):
        assert _load_kicad_mod("R_0805_2012Metric") is None

    def test_place_embeds_pads(self, fp_libs, board_ops: FileBoardOps, tmp_board: Path):
        board_ops.place_component(tmp_board, "R10", "Resistor_SMD:R_0805_2012Metric", 50.0, 50.0)
        content = tmp_board.read_text(encoding="utf-8")
        # Extract the R10 footprint block and count pads inside it
        fp_start = content.find('(footprint "Resistor_SMD:R_0805_2012Metric"')
        assert fp_start != -1, "Footprint block not written"
        from kicad_mcp.utils.sexp_parser import _walk_balanced_parens
        fp_end = _walk_balanced_parens(content, fp_start)
        fp_block = content[fp_start : fp_end + 1]
        pad_count = len(re.findall(r'\(pad\s+', fp_block))
        assert pad_count == 2, f"Expected 2 pads, got {pad_count}"

    def test_place_pads_have_coordinates(self, fp_libs, board_ops: FileBoardOps, tmp_board: Path):
        board_ops.place_component(tmp_board, "R11", "Resistor_SMD:R_0805_2012Metric", 60.0, 60.0)
        content = tmp_board.read_text(encoding="utf-8")
        from kicad_mcp.utils.sexp_parser import _walk_balanced_parens
        fp_start = content.find('(footprint "Resistor_SMD:R_0805_2012Metric"')
        fp_end = _walk_balanced_parens(content, fp_start)
        fp_block = content[fp_start : fp_end + 1]
        # Each pad should carry an (at x y) with non-zero x
        at_vals = re.findall(r'\(pad\b.*?\(at\s+([\d\.\-]+)', fp_block, re.DOTALL)
        assert len(at_vals) == 2
        assert any(float(v) != 0.0 for v in at_vals), "Expected at least one pad at non-zero x"

    def test_place_sot23_has_three_pads(self, fp_libs, board_ops: FileBoardOps, tmp_board: Path):
        board_ops.place_component(tmp_board, "U5", "Package_TO_SOT_SMD:SOT-23", 80.0, 80.0)
        content = tmp_board.read_text(encoding="utf-8")
        from kicad_mcp.utils.sexp_parser import _walk_balanced_parens
        fp_start = content.find('(footprint "Package_TO_SOT_SMD:SOT-23"')
        fp_end = _walk_balanced_parens(content, fp_start)
        fp_block = content[fp_start : fp_end + 1]
        pad_count = len(re.findall(r'\(pad\s+', fp_block))
        assert pad_count == 3

    def test_place_pads_have_uuids(self, fp_libs, board_ops: FileBoardOps, tmp_board: Path):
        # Use a unique reference so we don't collide with pre-existing footprints
        board_ops.place_component(tmp_board, "RUUID", "Resistor_SMD:R_0805_2012Metric", 70.0, 70.0)
        content = tmp_board.read_text(encoding="utf-8")
        from kicad_mcp.utils.sexp_parser import _walk_balanced_parens
        # Find the specific footprint block that contains RUUID
        fp_block = None
        for m in re.finditer(r'\(footprint "Resistor_SMD:R_0805_2012Metric"', content):
            end = _walk_balanced_parens(content, m.start())
            block = content[m.start() : end + 1]
            if '"RUUID"' in block:
                fp_block = block
                break
        assert fp_block is not None, "RUUID footprint not found"
        # Extract each pad block and verify it has a uuid
        pad_blocks = []
        for m in re.finditer(r'\(pad\s', fp_block):
            end = _walk_balanced_parens(fp_block, m.start())
            pad_blocks.append(fp_block[m.start() : end + 1])
        assert len(pad_blocks) == 2
        for pad_block in pad_blocks:
            assert '(uuid ' in pad_block, f"Pad missing uuid:\n{pad_block}"

    def test_stub_placed_when_footprint_missing(self, fp_libs, board_ops: FileBoardOps, tmp_board: Path):
        # Footprint not in our fixture libraries — should write a stub, not raise
        result = board_ops.place_component(
            tmp_board, "R99", "Resistor_SMD:NonExistent", 10.0, 10.0
        )
        assert result["reference"] == "R99"
        content = tmp_board.read_text(encoding="utf-8")
        assert '(footprint "Resistor_SMD:NonExistent"' in content

    def test_assign_net_after_place(self, fp_libs, board_ops: FileBoardOps, tmp_board: Path):
        board_ops.place_component(tmp_board, "R20", "Resistor_SMD:R_0805_2012Metric", 90.0, 90.0)
        # assign_net raises if pad is missing — this verifies real pads were embedded
        result = board_ops.assign_net(tmp_board, "R20", "1", "GND")
        assert result["reference"] == "R20"
        assert result["pad"] == "1"
        content = tmp_board.read_text(encoding="utf-8")
        assert "GND" in content
