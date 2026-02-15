"""Tests for S-expression parser utility functions."""

from __future__ import annotations

from kicad_mcp.utils.sexp_parser import (
    _walk_balanced_parens,
    extract_sexp_block,
    find_symbol_block_by_reference,
    remove_sexp_block,
)

# ---------------------------------------------------------------------------
# Sample schematic content used across multiple tests
# ---------------------------------------------------------------------------
SAMPLE_SCHEMATIC = """\
(kicad_sch (version 20230121) (generator "test")

  (uuid "a1b2c3d4-e5f6-7890-abcd-ef0123456789")

  (paper "A4")

  (lib_symbols
    (symbol "Device:R"
      (pin_names (offset 0))
      (pin "1" (at 0 1.27 270))
      (pin "2" (at 0 -1.27 90))
    )
  )

  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "R1" (at 100 48 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "10k" (at 100 52 0)
      (effects (font (size 1.27 1.27)))
    )
  )

  (symbol (lib_id "Device:R") (at 100 70 0) (unit 1)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "R2" (at 100 68 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "4.7k" (at 100 72 0)
      (effects (font (size 1.27 1.27)))
    )
  )

  (symbol (lib_id "MCU_Microchip_ATtiny:ATtiny85-20SU") (at 130 60 0) (unit 1)
    (uuid "33333333-3333-3333-3333-333333333333")
    (property "Reference" "U1" (at 130 52 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "ATtiny85" (at 130 68 0)
      (effects (font (size 1.27 1.27)))
    )
  )

  (wire (pts (xy 100 45) (xy 100 50))
    (stroke (width 0) (type default))
    (uuid "aaaa1111-1111-1111-1111-111111111111")
  )

  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


class TestWalkBalancedParens:
    def test_simple_block(self):
        content = "(foo bar)"
        end = _walk_balanced_parens(content, 0)
        assert end == len(content) - 1
        assert content[0:end + 1] == "(foo bar)"

    def test_nested_block(self):
        content = "(outer (inner 1) (inner 2))"
        end = _walk_balanced_parens(content, 0)
        assert content[0:end + 1] == content

    def test_quoted_parens_ignored(self):
        content = '(foo "a(b)c" bar)'
        end = _walk_balanced_parens(content, 0)
        assert content[0:end + 1] == content

    def test_escaped_quotes(self):
        content = r'(foo "a\"b" bar)'
        end = _walk_balanced_parens(content, 0)
        assert content[0:end + 1] == content

    def test_inner_block(self):
        content = "prefix (inner stuff) suffix"
        end = _walk_balanced_parens(content, 7)
        assert content[7:end + 1] == "(inner stuff)"

    def test_unbalanced_returns_none(self):
        content = "(foo (bar)"
        assert _walk_balanced_parens(content, 0) is None


class TestFindSymbolBlockByReference:
    def test_find_r1(self):
        result = find_symbol_block_by_reference(SAMPLE_SCHEMATIC, "R1")
        assert result is not None
        start, end = result
        block = SAMPLE_SCHEMATIC[start:end + 1]
        assert block.startswith("(symbol")
        assert '"R1"' in block
        assert '"10k"' in block
        # Should not contain R2 content
        assert '"R2"' not in block

    def test_find_r2(self):
        result = find_symbol_block_by_reference(SAMPLE_SCHEMATIC, "R2")
        assert result is not None
        block = SAMPLE_SCHEMATIC[result[0]:result[1] + 1]
        assert '"R2"' in block
        assert '"4.7k"' in block

    def test_find_u1(self):
        result = find_symbol_block_by_reference(SAMPLE_SCHEMATIC, "U1")
        assert result is not None
        block = SAMPLE_SCHEMATIC[result[0]:result[1] + 1]
        assert '"U1"' in block
        assert '"ATtiny85"' in block

    def test_not_found(self):
        result = find_symbol_block_by_reference(SAMPLE_SCHEMATIC, "C99")
        assert result is None

    def test_skips_lib_symbols_section(self):
        """Ensure that symbol defs inside lib_symbols are not matched."""
        # The lib_symbols section has (symbol "Device:R" ...) but that should
        # not be matched when we search for a reference like "Device:R"
        # (which would be unusual but tests the skip logic).
        content = """\
(kicad_sch
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0))
    )
  )
  (symbol (lib_id "Device:R") (at 10 20 0) (unit 1)
    (property "Reference" "R" (at 10 18 0))
    (property "Value" "1k" (at 10 22 0))
  )
)
"""
        result = find_symbol_block_by_reference(content, "R")
        assert result is not None
        block = content[result[0]:result[1] + 1]
        # Should be the instance, not the lib_symbols def
        assert '(lib_id "Device:R")' in block
        assert '"1k"' in block

    def test_no_lib_symbols_section(self):
        """Works when there is no lib_symbols section at all."""
        content = """\
(kicad_sch
  (symbol (lib_id "Device:C") (at 50 50 0) (unit 1)
    (property "Reference" "C1" (at 50 48 0))
    (property "Value" "100nF" (at 50 52 0))
  )
)
"""
        result = find_symbol_block_by_reference(content, "C1")
        assert result is not None
        block = content[result[0]:result[1] + 1]
        assert '"C1"' in block

    def test_reference_with_special_chars(self):
        """References like '#PWR01' should work (regex-escaped)."""
        content = """\
(kicad_sch
  (lib_symbols)
  (symbol (lib_id "power:GND") (at 50 80 0) (unit 1)
    (property "Reference" "#PWR01" (at 50 86 0))
    (property "Value" "GND" (at 50 83 0))
  )
)
"""
        result = find_symbol_block_by_reference(content, "#PWR01")
        assert result is not None
        block = content[result[0]:result[1] + 1]
        assert '"#PWR01"' in block


class TestRemoveSexpBlock:
    def test_remove_middle_symbol(self):
        result = find_symbol_block_by_reference(SAMPLE_SCHEMATIC, "R2")
        assert result is not None
        modified = remove_sexp_block(SAMPLE_SCHEMATIC, result[0], result[1])
        # R2 should be gone
        assert '"R2"' not in modified
        assert '"4.7k"' not in modified
        # Other symbols should remain
        assert '"R1"' in modified
        assert '"U1"' in modified
        # File should still be valid (start and end with parens)
        assert modified.strip().startswith("(")
        assert modified.strip().endswith(")")

    def test_remove_first_symbol(self):
        result = find_symbol_block_by_reference(SAMPLE_SCHEMATIC, "R1")
        assert result is not None
        modified = remove_sexp_block(SAMPLE_SCHEMATIC, result[0], result[1])
        assert '"R1"' not in modified
        assert '"R2"' in modified
        assert '"U1"' in modified

    def test_remove_last_symbol(self):
        result = find_symbol_block_by_reference(SAMPLE_SCHEMATIC, "U1")
        assert result is not None
        modified = remove_sexp_block(SAMPLE_SCHEMATIC, result[0], result[1])
        assert '"U1"' not in modified
        assert '"R1"' in modified
        assert '"R2"' in modified

    def test_no_double_blank_lines(self):
        result = find_symbol_block_by_reference(SAMPLE_SCHEMATIC, "R2")
        assert result is not None
        modified = remove_sexp_block(SAMPLE_SCHEMATIC, result[0], result[1])
        # Should not have triple+ newlines (double blank lines)
        assert "\n\n\n" not in modified


class TestExtractSexpBlockRefactored:
    """Verify extract_sexp_block still works after refactoring to use _walk_balanced_parens."""

    def test_extract_existing_symbol(self):
        block = extract_sexp_block(SAMPLE_SCHEMATIC, "symbol", "Device:R")
        assert block is not None
        assert block.startswith("(symbol")
        assert "Device:R" in block

    def test_extract_nonexistent(self):
        block = extract_sexp_block(SAMPLE_SCHEMATIC, "symbol", "Nonexistent:Foo")
        assert block is None
