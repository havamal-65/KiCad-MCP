"""Unit conversion utilities for KiCad measurements."""

from __future__ import annotations

# KiCad internal unit is nanometers (nm)
NM_PER_MM = 1_000_000
NM_PER_MIL = 25_400
NM_PER_INCH = 25_400_000


def mm_to_nm(mm: float) -> int:
    """Convert millimeters to nanometers (KiCad internal unit)."""
    return int(mm * NM_PER_MM)


def nm_to_mm(nm: int) -> float:
    """Convert nanometers to millimeters."""
    return nm / NM_PER_MM


def mil_to_nm(mil: float) -> int:
    """Convert mils (thousandths of an inch) to nanometers."""
    return int(mil * NM_PER_MIL)


def nm_to_mil(nm: int) -> float:
    """Convert nanometers to mils."""
    return nm / NM_PER_MIL


def mil_to_mm(mil: float) -> float:
    """Convert mils to millimeters."""
    return mil * 0.0254


def mm_to_mil(mm: float) -> float:
    """Convert millimeters to mils."""
    return mm / 0.0254


def inch_to_mm(inch: float) -> float:
    """Convert inches to millimeters."""
    return inch * 25.4


def mm_to_inch(mm: float) -> float:
    """Convert millimeters to inches."""
    return mm / 25.4


def normalize_to_mm(value: float, unit: str) -> float:
    """Normalize any supported unit to millimeters.

    Args:
        value: Numeric value to convert.
        unit: Source unit - one of 'mm', 'mil', 'inch', 'nm'.

    Returns:
        Value in millimeters.

    Raises:
        ValueError: If unit is not recognized.
    """
    unit = unit.lower().strip()
    if unit == "mm":
        return value
    elif unit == "mil":
        return mil_to_mm(value)
    elif unit in ("inch", "in"):
        return inch_to_mm(value)
    elif unit == "nm":
        return nm_to_mm(int(value))
    else:
        raise ValueError(f"Unknown unit '{unit}'. Supported: mm, mil, inch, nm")
