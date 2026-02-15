"""Input validation utilities for KiCad data."""

from __future__ import annotations

import re
from pathlib import Path

from kicad_mcp.models.errors import (
    InvalidNetNameError,
    InvalidPathError,
    InvalidReferenceError,
)

# Reference designator pattern: one or more letters followed by one or more digits
# Supports multi-unit like U3A, U3B
REFERENCE_PATTERN = re.compile(r"^[A-Za-z]+\d+[A-Za-z]?$")

# Net name: alphanumeric, underscores, hyphens, slashes, dots, plus signs
# Allows hierarchical nets like /sheet1/VCC and differential pairs like USB_D+
NET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-/\.+~]+$")

# KiCad file extensions
KICAD_PROJECT_EXT = ".kicad_pro"
KICAD_BOARD_EXT = ".kicad_pcb"
KICAD_SCHEMATIC_EXT = ".kicad_sch"
KICAD_SYMBOL_LIB_EXT = ".kicad_sym"
KICAD_FOOTPRINT_EXT = ".kicad_mod"


def validate_reference(ref: str) -> str:
    """Validate a component reference designator.

    Args:
        ref: Reference like 'R1', 'U3', 'C10', 'Q2A'.

    Returns:
        The validated reference string.

    Raises:
        InvalidReferenceError: If the reference format is invalid.
    """
    if not ref or not REFERENCE_PATTERN.match(ref):
        raise InvalidReferenceError(
            f"Invalid reference designator: '{ref}'. "
            "Expected format: letter(s) + number(s), e.g. R1, U3, C10"
        )
    return ref


def validate_net_name(name: str) -> str:
    """Validate a net name.

    Args:
        name: Net name like 'VCC', 'GND', '/sheet1/SDA'.

    Returns:
        The validated net name.

    Raises:
        InvalidNetNameError: If the net name format is invalid.
    """
    if not name:
        raise InvalidNetNameError("Net name cannot be empty")
    if not NET_NAME_PATTERN.match(name):
        raise InvalidNetNameError(
            f"Invalid net name: '{name}'. "
            "Allowed characters: alphanumeric, _, -, /, ., +, ~"
        )
    return name


def validate_kicad_path(path: str, expected_ext: str | None = None) -> Path:
    """Validate a path to a KiCad file.

    Args:
        path: File path string.
        expected_ext: Expected file extension (e.g. '.kicad_pcb').

    Returns:
        Resolved Path object.

    Raises:
        InvalidPathError: If the path is invalid or file doesn't exist.
    """
    if not path:
        raise InvalidPathError("File path cannot be empty")

    p = Path(path).resolve()

    if not p.exists():
        raise InvalidPathError(f"File not found: {p}")

    if expected_ext and p.suffix != expected_ext:
        raise InvalidPathError(
            f"Expected {expected_ext} file, got '{p.suffix}': {p}"
        )

    return p


def validate_writable_path(path: str, expected_ext: str | None = None) -> Path:
    """Validate a path that will be written to (file may not exist yet).

    Args:
        path: File path string.
        expected_ext: Expected file extension.

    Returns:
        Resolved Path object.

    Raises:
        InvalidPathError: If the parent directory doesn't exist.
    """
    if not path:
        raise InvalidPathError("File path cannot be empty")

    p = Path(path).resolve()

    if not p.parent.exists():
        raise InvalidPathError(f"Parent directory does not exist: {p.parent}")

    if expected_ext and p.suffix != expected_ext:
        raise InvalidPathError(
            f"Expected {expected_ext} extension, got '{p.suffix}': {p}"
        )

    return p


def validate_layer(layer: str) -> str:
    """Validate a KiCad layer name.

    Returns:
        The validated layer name.

    Raises:
        ValidationError: If the layer name is not recognized.
    """
    valid_layers = {
        "F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu",
        "In5.Cu", "In6.Cu", "In7.Cu", "In8.Cu",
        "F.SilkS", "B.SilkS", "F.Mask", "B.Mask",
        "F.Paste", "B.Paste", "F.CrtYd", "B.CrtYd",
        "F.Fab", "B.Fab", "Edge.Cuts", "Margin",
        "Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User",
    }
    if layer not in valid_layers:
        from kicad_mcp.models.errors import ValidationError
        raise ValidationError(
            f"Unknown layer: '{layer}'. Valid layers: {', '.join(sorted(valid_layers))}"
        )
    return layer


def validate_positive(value: float, name: str = "value") -> float:
    """Validate that a numeric value is positive."""
    if value <= 0:
        from kicad_mcp.models.errors import ValidationError
        raise ValidationError(f"{name} must be positive, got {value}")
    return value
