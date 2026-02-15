"""Integration tests for library management using real file operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.backends.file_backend import FileLibraryManageOps
from kicad_mcp.models.errors import LibraryImportError, LibraryManageError
from kicad_mcp.utils.library_sources import LibrarySourceRegistry
from kicad_mcp.utils.sexp_parser import extract_sexp_block


# --- Fixtures ---

MINIMAL_SYMBOL_LIB = (
    '(kicad_symbol_lib\n'
    '  (version 20231120)\n'
    '  (generator "kicad_mcp")\n'
    '  (generator_version "9.0")\n'
    ')\n'
)

SOURCE_SYMBOL_LIB = (
    '(kicad_symbol_lib\n'
    '  (version 20231120)\n'
    '  (generator "kicad_mcp")\n'
    '  (symbol "SCD41"\n'
    '    (property "Reference" "U"\n'
    '      (at 0 0 0)\n'
    '      (effects (font (size 1.27 1.27)))\n'
    '    )\n'
    '    (property "Value" "SCD41"\n'
    '      (at 0 2 0)\n'
    '      (effects (font (size 1.27 1.27)))\n'
    '    )\n'
    '    (symbol "SCD41_0_1"\n'
    '      (rectangle (start -5.08 5.08) (end 5.08 -5.08)\n'
    '        (stroke (width 0) (type default))\n'
    '        (fill (type background))\n'
    '      )\n'
    '    )\n'
    '  )\n'
    '  (symbol "SGP41"\n'
    '    (property "Reference" "U"\n'
    '      (at 0 0 0)\n'
    '      (effects (font (size 1.27 1.27)))\n'
    '    )\n'
    '  )\n'
    ')\n'
)


@pytest.fixture
def registry(tmp_path: Path) -> LibrarySourceRegistry:
    return LibrarySourceRegistry(tmp_path / "library_sources.json")


@pytest.fixture
def ops(registry: LibrarySourceRegistry) -> FileLibraryManageOps:
    return FileLibraryManageOps(registry=registry)


@pytest.fixture
def source_libs(tmp_path: Path) -> Path:
    """Create a sample source library structure."""
    libs_dir = tmp_path / "source_libs"
    libs_dir.mkdir()

    # Symbol library
    sym_file = libs_dir / "Sensors.kicad_sym"
    sym_file.write_text(SOURCE_SYMBOL_LIB, encoding="utf-8")

    # Footprint library
    fp_dir = libs_dir / "Sensors.pretty"
    fp_dir.mkdir()
    fp_file = fp_dir / "SCD41.kicad_mod"
    fp_file.write_text(
        '(footprint "SCD41"\n'
        '  (layer "F.Cu")\n'
        '  (pad "1" smd rect (at 0 0) (size 0.5 0.5)\n'
        '    (layers "F.Cu" "F.Paste" "F.Mask")\n'
        '  )\n'
        ')\n',
        encoding="utf-8",
    )
    return libs_dir


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    proj = tmp_path / "my_project"
    proj.mkdir()
    (proj / "my_project.kicad_pro").write_text("{}", encoding="utf-8")
    return proj


# --- extract_sexp_block tests ---

class TestExtractSexpBlock:
    def test_extracts_symbol(self):
        block = extract_sexp_block(SOURCE_SYMBOL_LIB, "symbol", "SCD41")
        assert block is not None
        assert block.startswith('(symbol "SCD41"')
        assert "SCD41_0_1" in block  # sub-symbol included

    def test_extracts_second_symbol(self):
        block = extract_sexp_block(SOURCE_SYMBOL_LIB, "symbol", "SGP41")
        assert block is not None
        assert '"SGP41"' in block

    def test_returns_none_for_missing(self):
        block = extract_sexp_block(SOURCE_SYMBOL_LIB, "symbol", "NONEXISTENT")
        assert block is None


# --- LibrarySourceRegistry tests ---

class TestLibrarySourceRegistry:
    def test_register_and_list(self, registry: LibrarySourceRegistry, source_libs: Path):
        registry.register("sensors", str(source_libs), source_type="local")
        sources = registry.list_all()
        assert len(sources) == 1
        assert sources[0]["name"] == "sensors"

    def test_unregister(self, registry: LibrarySourceRegistry, source_libs: Path):
        registry.register("sensors", str(source_libs))
        assert registry.unregister("sensors") is True
        assert registry.list_all() == []

    def test_unregister_nonexistent(self, registry: LibrarySourceRegistry):
        assert registry.unregister("nonexistent") is False

    def test_find_symbol_libs(self, registry: LibrarySourceRegistry, source_libs: Path):
        registry.register("sensors", str(source_libs))
        sym_libs = registry.find_symbol_libs()
        assert any("Sensors.kicad_sym" in str(p) for p in sym_libs)

    def test_find_footprint_libs(self, registry: LibrarySourceRegistry, source_libs: Path):
        registry.register("sensors", str(source_libs))
        fp_libs = registry.find_footprint_libs()
        assert any("Sensors.pretty" in str(p) for p in fp_libs)

    def test_persistence(self, tmp_path: Path, source_libs: Path):
        reg_path = tmp_path / "reg.json"
        reg1 = LibrarySourceRegistry(reg_path)
        reg1.register("sensors", str(source_libs))

        # Load fresh instance from same file
        reg2 = LibrarySourceRegistry(reg_path)
        assert len(reg2.list_all()) == 1
        assert reg2.get("sensors") is not None


# --- FileLibraryManageOps tests ---

class TestRegisterLibrarySource:
    def test_register_valid_path(self, ops: FileLibraryManageOps, source_libs: Path):
        result = ops.register_library_source(str(source_libs), "sensors")
        assert result["name"] == "sensors"
        assert result["source_type"] == "local"

    def test_register_nonexistent_path(self, ops: FileLibraryManageOps):
        with pytest.raises(LibraryManageError, match="does not exist"):
            ops.register_library_source("/nonexistent/path", "bad")


class TestSearchLibrarySources:
    def test_search_symbols(self, ops: FileLibraryManageOps, source_libs: Path):
        ops.register_library_source(str(source_libs), "sensors")
        result = ops.search_library_sources("SCD41")
        assert len(result["symbols"]) >= 1
        assert result["symbols"][0]["name"] == "SCD41"

    def test_search_footprints(self, ops: FileLibraryManageOps, source_libs: Path):
        ops.register_library_source(str(source_libs), "sensors")
        result = ops.search_library_sources("SCD41")
        assert len(result["footprints"]) >= 1

    def test_search_no_results(self, ops: FileLibraryManageOps, source_libs: Path):
        ops.register_library_source(str(source_libs), "sensors")
        result = ops.search_library_sources("NONEXISTENT_COMPONENT")
        assert result["symbols"] == []
        assert result["footprints"] == []


class TestCreateProjectLibrary:
    def test_create_both(self, ops: FileLibraryManageOps, project_dir: Path):
        result = ops.create_project_library(
            str(project_dir / "my_project.kicad_pro"), "my_lib",
        )
        assert len(result["created"]) == 2
        assert (project_dir / "my_lib.kicad_sym").exists()
        assert (project_dir / "my_lib.pretty").is_dir()

    def test_create_symbol_only(self, ops: FileLibraryManageOps, project_dir: Path):
        result = ops.create_project_library(
            str(project_dir / "my_project.kicad_pro"), "sym_only", lib_type="symbol",
        )
        assert (project_dir / "sym_only.kicad_sym").exists()
        assert not (project_dir / "sym_only.pretty").exists()

    def test_create_footprint_only(self, ops: FileLibraryManageOps, project_dir: Path):
        result = ops.create_project_library(
            str(project_dir / "my_project.kicad_pro"), "fp_only", lib_type="footprint",
        )
        assert not (project_dir / "fp_only.kicad_sym").exists()
        assert (project_dir / "fp_only.pretty").is_dir()

    def test_idempotent(self, ops: FileLibraryManageOps, project_dir: Path):
        ops.create_project_library(str(project_dir / "my_project.kicad_pro"), "my_lib")
        # Second call should not overwrite
        result = ops.create_project_library(str(project_dir / "my_project.kicad_pro"), "my_lib")
        assert result["created"] == []


class TestImportSymbol:
    def test_import_symbol(self, ops: FileLibraryManageOps, source_libs: Path, project_dir: Path):
        # Create target library
        target = project_dir / "my_lib.kicad_sym"
        target.write_text(MINIMAL_SYMBOL_LIB, encoding="utf-8")

        result = ops.import_symbol(
            str(source_libs / "Sensors.kicad_sym"),
            "SCD41",
            str(target),
        )
        assert result["symbol_name"] == "SCD41"

        # Verify the symbol is in the target
        content = target.read_text(encoding="utf-8")
        assert '"SCD41"' in content
        # Sub-symbol should be included
        assert "SCD41_0_1" in content

    def test_import_duplicate_fails(self, ops: FileLibraryManageOps, source_libs: Path, project_dir: Path):
        target = project_dir / "my_lib.kicad_sym"
        target.write_text(MINIMAL_SYMBOL_LIB, encoding="utf-8")

        ops.import_symbol(str(source_libs / "Sensors.kicad_sym"), "SCD41", str(target))
        with pytest.raises(LibraryImportError, match="already exists"):
            ops.import_symbol(str(source_libs / "Sensors.kicad_sym"), "SCD41", str(target))

    def test_import_missing_symbol(self, ops: FileLibraryManageOps, source_libs: Path, project_dir: Path):
        target = project_dir / "my_lib.kicad_sym"
        target.write_text(MINIMAL_SYMBOL_LIB, encoding="utf-8")

        with pytest.raises(LibraryImportError, match="not found"):
            ops.import_symbol(
                str(source_libs / "Sensors.kicad_sym"),
                "NONEXISTENT",
                str(target),
            )


class TestImportFootprint:
    def test_import_footprint(self, ops: FileLibraryManageOps, source_libs: Path, project_dir: Path):
        target_dir = project_dir / "my_lib.pretty"
        target_dir.mkdir()

        result = ops.import_footprint(
            str(source_libs / "Sensors.pretty"),
            "SCD41",
            str(target_dir),
        )
        assert result["footprint_name"] == "SCD41"
        assert (target_dir / "SCD41.kicad_mod").exists()

    def test_import_duplicate_fails(self, ops: FileLibraryManageOps, source_libs: Path, project_dir: Path):
        target_dir = project_dir / "my_lib.pretty"
        target_dir.mkdir()

        ops.import_footprint(str(source_libs / "Sensors.pretty"), "SCD41", str(target_dir))
        with pytest.raises(LibraryImportError, match="already exists"):
            ops.import_footprint(str(source_libs / "Sensors.pretty"), "SCD41", str(target_dir))

    def test_import_missing_footprint(self, ops: FileLibraryManageOps, source_libs: Path, project_dir: Path):
        target_dir = project_dir / "my_lib.pretty"
        target_dir.mkdir()

        with pytest.raises(LibraryImportError, match="not found"):
            ops.import_footprint(str(source_libs / "Sensors.pretty"), "NONEXISTENT", str(target_dir))


class TestRegisterProjectLibrary:
    def test_register_symbol_library(self, ops: FileLibraryManageOps, project_dir: Path):
        sym_file = project_dir / "my_lib.kicad_sym"
        sym_file.write_text(MINIMAL_SYMBOL_LIB, encoding="utf-8")

        result = ops.register_project_library(
            str(project_dir / "my_project.kicad_pro"),
            "my_lib",
            str(sym_file),
            "symbol",
        )
        assert result["lib_type"] == "symbol"
        assert "KIPRJMOD" in result["uri"]

        table_file = project_dir / "sym-lib-table"
        assert table_file.exists()
        content = table_file.read_text(encoding="utf-8")
        assert '(name "my_lib")' in content
        assert "(sym_lib_table" in content

    def test_register_footprint_library(self, ops: FileLibraryManageOps, project_dir: Path):
        fp_dir = project_dir / "my_lib.pretty"
        fp_dir.mkdir()

        result = ops.register_project_library(
            str(project_dir / "my_project.kicad_pro"),
            "my_lib",
            str(fp_dir),
            "footprint",
        )
        assert result["lib_type"] == "footprint"
        table_file = project_dir / "fp-lib-table"
        assert table_file.exists()
        assert '(name "my_lib")' in table_file.read_text(encoding="utf-8")

    def test_append_to_existing_table(self, ops: FileLibraryManageOps, project_dir: Path):
        sym_file = project_dir / "my_lib.kicad_sym"
        sym_file.write_text(MINIMAL_SYMBOL_LIB, encoding="utf-8")

        ops.register_project_library(
            str(project_dir / "my_project.kicad_pro"), "lib_a", str(sym_file), "symbol",
        )
        ops.register_project_library(
            str(project_dir / "my_project.kicad_pro"), "lib_b", str(sym_file), "symbol",
        )

        content = (project_dir / "sym-lib-table").read_text(encoding="utf-8")
        assert '(name "lib_a")' in content
        assert '(name "lib_b")' in content

    def test_skip_duplicate_registration(self, ops: FileLibraryManageOps, project_dir: Path):
        sym_file = project_dir / "my_lib.kicad_sym"
        sym_file.write_text(MINIMAL_SYMBOL_LIB, encoding="utf-8")

        ops.register_project_library(
            str(project_dir / "my_project.kicad_pro"), "my_lib", str(sym_file), "symbol",
        )
        result = ops.register_project_library(
            str(project_dir / "my_project.kicad_pro"), "my_lib", str(sym_file), "symbol",
        )
        assert result.get("already_registered") is True

    def test_invalid_lib_type(self, ops: FileLibraryManageOps, project_dir: Path):
        with pytest.raises(LibraryManageError, match="lib_type must be"):
            ops.register_project_library(
                str(project_dir / "my_project.kicad_pro"), "lib", "/some/path", "invalid",
            )
