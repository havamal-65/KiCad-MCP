"""RW-S4 §11 — cross-version symbol-cache grammar fidelity.

Symbols resolved from a KiCad 10 library must be downgraded to KiCad 9
grammar when cached into an MCP-authored (K9-stamped) schematic, or KiCad 9
refuses to load the whole file. Found live 2026-10-03 on a dual-install
machine (``(in_pos_files yes)`` at line 21 of a generated schematic).
"""

from __future__ import annotations

from pathlib import Path

from kicad_mcp.backends.file_backend import (
    FileSchematicOps,
    _KICAD10_ONLY_SYMBOL_TOKENS,
    _strip_kicad10_symbol_tokens,
)

K10_LIB = """\
(kicad_symbol_lib
	(version 20260301)
	(generator "kicad_symbol_editor")
	(symbol "R10"
		(pin_numbers
			(hide yes)
		)
		(pin_names
			(offset 0)
		)
		(exclude_from_sim no)
		(in_bom yes)
		(on_board yes)
		(in_pos_files yes)
		(duplicate_pin_numbers_are_jumpers no)
		(body_styles demorgan)
		(property "Reference" "R"
			(at 2.032 0 90)
		)
		(property "Value" "R10"
			(at 0 0 90)
		)
		(symbol "R10_1_1"
			(unit_name "MAIN")
			(pin passive line
				(at 0 3.81 270)
				(length 1.27)
				(name "~" (effects (font (size 1.27 1.27))))
				(number "1" (effects (font (size 1.27 1.27))))
			)
			(pin passive line
				(at 0 -3.81 90)
				(length 1.27)
				(name "~" (effects (font (size 1.27 1.27))))
				(number "2" (effects (font (size 1.27 1.27))))
			)
		)
	)
	(symbol "RBase"
		(body_styles demorgan)
		(property "Reference" "R"
			(at 0 0 0)
		)
		(symbol "RBase_1_1"
			(unit_name "BASE")
			(pin passive line
				(at 0 3.81 270)
				(length 1.27)
				(name "~" (effects (font (size 1.27 1.27))))
				(number "1" (effects (font (size 1.27 1.27))))
			)
		)
	)
	(symbol "RChild"
		(extends "RBase")
		(in_pos_files yes)
		(property "Reference" "R"
			(at 0 0 0)
		)
	)
)
"""

K9_LIB = """\
(kicad_symbol_lib
	(version 20241209)
	(generator "kicad_symbol_editor")
	(symbol "R9"
		(pin_numbers
			(hide yes)
		)
		(pin_names
			(offset 0)
		)
		(exclude_from_sim no)
		(in_bom yes)
		(on_board yes)
		(property "Reference" "R"
			(at 2.032 0 90)
		)
		(symbol "R9_1_1"
			(pin passive line
				(at 0 3.81 270)
				(length 1.27)
				(name "~" (effects (font (size 1.27 1.27))))
				(number "1" (effects (font (size 1.27 1.27))))
			)
		)
	)
)
"""


def _ops_with_lib(tmp_path: Path, lib_name: str, lib_content: str):
    lib = tmp_path / f"{lib_name}.kicad_sym"
    lib.write_text(lib_content, encoding="utf-8")
    ops = FileSchematicOps()
    ops._symbol_libs = [lib]
    return ops


def _new_sch(tmp_path: Path, ops: FileSchematicOps) -> Path:
    sch = tmp_path / "t.kicad_sch"
    ops.create_schematic(sch)
    return sch


def test_k10_tokens_stripped_on_cache(tmp_path: Path):
    ops = _ops_with_lib(tmp_path, "FakeK10", K10_LIB)
    sch = _new_sch(tmp_path, ops)
    ops.add_component(sch, "FakeK10:R10", "R1", "10k", 50.0, 50.0)
    content = sch.read_text(encoding="utf-8")
    assert '(symbol "FakeK10:R10"' in content
    for token in _KICAD10_ONLY_SYMBOL_TOKENS:
        assert f"({token}" not in content, token
    # downgrade never unbalances the document
    assert content.count("(") == content.count(")")
    # K9-valid neighbours survive
    assert "(pin_numbers" in content
    assert content.count("(pin ") >= 2


def test_k10_strip_covers_inlined_parent(tmp_path: Path):
    """An extends child inlines its parent's sub-symbols; K10 tokens from
    the parent (body_styles, unit_name) must not ride along."""
    ops = _ops_with_lib(tmp_path, "FakeK10", K10_LIB)
    sch = _new_sch(tmp_path, ops)
    ops.add_component(sch, "FakeK10:RChild", "R2", "1k", 60.0, 50.0)
    content = sch.read_text(encoding="utf-8")
    assert '(symbol "FakeK10:RChild"' in content
    assert "(extends" not in content
    for token in _KICAD10_ONLY_SYMBOL_TOKENS:
        assert f"({token}" not in content, token
    assert content.count("(") == content.count(")")


def test_k9_symbol_cached_unchanged(tmp_path: Path):
    """A K9-grammar library block passes through the strip untouched."""
    sym_block = K9_LIB[K9_LIB.index('(symbol "R9"'):K9_LIB.rindex(")")]
    assert _strip_kicad10_symbol_tokens(sym_block) == sym_block

    ops = _ops_with_lib(tmp_path, "FakeK9", K9_LIB)
    sch = _new_sch(tmp_path, ops)
    ops.add_component(sch, "FakeK9:R9", "R1", "10k", 50.0, 50.0)
    content = sch.read_text(encoding="utf-8")
    assert '(symbol "FakeK9:R9"' in content
    assert "(pin_numbers" in content
    assert content.count("(") == content.count(")")


def test_strip_helper_leaves_unbalanced_input_untouched():
    broken = '(symbol "X"\n\t(in_pos_files yes\n'
    assert _strip_kicad10_symbol_tokens(broken) == broken
