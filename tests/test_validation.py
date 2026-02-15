"""Tests for input validation utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.models.errors import (
    InvalidNetNameError,
    InvalidPathError,
    InvalidReferenceError,
)
from kicad_mcp.utils.validation import (
    validate_kicad_path,
    validate_layer,
    validate_net_name,
    validate_positive,
    validate_reference,
)


class TestValidateReference:
    def test_valid_references(self):
        assert validate_reference("R1") == "R1"
        assert validate_reference("U3") == "U3"
        assert validate_reference("C10") == "C10"
        assert validate_reference("Q2A") == "Q2A"
        assert validate_reference("SW1") == "SW1"

    def test_invalid_references(self):
        with pytest.raises(InvalidReferenceError):
            validate_reference("")
        with pytest.raises(InvalidReferenceError):
            validate_reference("1R")
        with pytest.raises(InvalidReferenceError):
            validate_reference("R")
        with pytest.raises(InvalidReferenceError):
            validate_reference("123")


class TestValidateNetName:
    def test_valid_nets(self):
        assert validate_net_name("VCC") == "VCC"
        assert validate_net_name("GND") == "GND"
        assert validate_net_name("/sheet1/SDA") == "/sheet1/SDA"
        assert validate_net_name("USB_D+") == "USB_D+"
        assert validate_net_name("Net-R1-Pad1") == "Net-R1-Pad1"

    def test_invalid_nets(self):
        with pytest.raises(InvalidNetNameError):
            validate_net_name("")
        with pytest.raises(InvalidNetNameError):
            validate_net_name("has space")
        with pytest.raises(InvalidNetNameError):
            validate_net_name("net@bad")


class TestValidateKicadPath:
    def test_valid_path(self, sample_board_path: Path):
        result = validate_kicad_path(str(sample_board_path), ".kicad_pcb")
        assert result == sample_board_path.resolve()

    def test_wrong_extension(self, sample_board_path: Path):
        with pytest.raises(InvalidPathError):
            validate_kicad_path(str(sample_board_path), ".kicad_sch")

    def test_nonexistent_path(self):
        with pytest.raises(InvalidPathError):
            validate_kicad_path("/nonexistent/path.kicad_pcb", ".kicad_pcb")

    def test_empty_path(self):
        with pytest.raises(InvalidPathError):
            validate_kicad_path("", ".kicad_pcb")


class TestValidateLayer:
    def test_valid_layers(self):
        assert validate_layer("F.Cu") == "F.Cu"
        assert validate_layer("B.Cu") == "B.Cu"
        assert validate_layer("Edge.Cuts") == "Edge.Cuts"

    def test_invalid_layer(self):
        with pytest.raises(Exception):
            validate_layer("NotALayer")


class TestValidatePositive:
    def test_valid(self):
        assert validate_positive(1.0) == 1.0
        assert validate_positive(0.001) == 0.001

    def test_invalid(self):
        with pytest.raises(Exception):
            validate_positive(0.0)
        with pytest.raises(Exception):
            validate_positive(-1.0)
