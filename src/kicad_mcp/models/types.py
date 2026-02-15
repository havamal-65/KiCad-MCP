"""Pydantic models for tool parameters and results."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# --- Enums ---

class LayerName(str, Enum):
    F_CU = "F.Cu"
    B_CU = "B.Cu"
    IN1_CU = "In1.Cu"
    IN2_CU = "In2.Cu"
    F_SILKSCREEN = "F.SilkS"
    B_SILKSCREEN = "B.SilkS"
    F_MASK = "F.Mask"
    B_MASK = "B.Mask"
    F_PASTE = "F.Paste"
    B_PASTE = "B.Paste"
    F_COURTYARD = "F.CrtYd"
    B_COURTYARD = "B.CrtYd"
    F_FAB = "F.Fab"
    B_FAB = "B.Fab"
    EDGE_CUTS = "Edge.Cuts"
    MARGIN = "Margin"
    DWGS_USER = "Dwgs.User"
    CMTS_USER = "Cmts.User"


class ViaType(str, Enum):
    THROUGH = "through"
    BLIND_BURIED = "blind_buried"
    MICRO = "micro"


class ExportFormat(str, Enum):
    GERBER = "gerber"
    DRILL = "drill"
    PDF = "pdf"
    SVG = "svg"
    STEP = "step"
    VRML = "vrml"


class BOMFormat(str, Enum):
    CSV = "csv"
    JSON = "json"
    XML = "xml"
    HTML = "html"


# --- Position / Geometry ---

class Position(BaseModel):
    x: float = Field(description="X coordinate in mm")
    y: float = Field(description="Y coordinate in mm")


class PositionWithRotation(Position):
    rotation: float = Field(default=0.0, description="Rotation in degrees")


# --- Component Models ---

class ComponentInfo(BaseModel):
    reference: str = Field(description="Component reference designator (e.g. R1, U3)")
    value: str = Field(default="", description="Component value")
    footprint: str = Field(default="", description="Footprint library:name")
    position: Optional[Position] = None
    layer: str = Field(default="F.Cu", description="Component layer")
    properties: dict[str, str] = Field(default_factory=dict)


class NetInfo(BaseModel):
    name: str = Field(description="Net name")
    number: int = Field(default=0, description="Net number/code")
    pad_count: int = Field(default=0, description="Number of pads connected")


class TrackInfo(BaseModel):
    start: Position
    end: Position
    width: float = Field(description="Track width in mm")
    layer: str = Field(description="Layer name")
    net: str = Field(default="", description="Net name")


class ViaInfo(BaseModel):
    position: Position
    size: float = Field(description="Via diameter in mm")
    drill: float = Field(description="Drill diameter in mm")
    net: str = Field(default="", description="Net name")
    via_type: ViaType = Field(default=ViaType.THROUGH)
    layers: list[str] = Field(default_factory=lambda: ["F.Cu", "B.Cu"])


class ZoneInfo(BaseModel):
    net: str = Field(description="Net name")
    layer: str = Field(description="Layer name")
    outline: list[Position] = Field(description="Zone outline points")
    priority: int = Field(default=0)


# --- Board Models ---

class BoardInfo(BaseModel):
    file_path: str
    title: str = ""
    revision: str = ""
    date: str = ""
    page_size: str = "A4"
    num_components: int = 0
    num_nets: int = 0
    num_tracks: int = 0
    num_vias: int = 0
    num_zones: int = 0
    layers: list[str] = Field(default_factory=list)


class BoardReadResult(BaseModel):
    info: BoardInfo
    components: list[ComponentInfo] = Field(default_factory=list)
    nets: list[NetInfo] = Field(default_factory=list)
    tracks: list[TrackInfo] = Field(default_factory=list)
    vias: list[ViaInfo] = Field(default_factory=list)


# --- Schematic Models ---

class SchematicSymbol(BaseModel):
    reference: str = Field(description="Component reference")
    value: str = Field(default="", description="Component value")
    lib_id: str = Field(default="", description="Library symbol identifier")
    position: Optional[Position] = None
    unit: int = Field(default=1, description="Symbol unit number")
    properties: dict[str, str] = Field(default_factory=dict)


class SchematicWire(BaseModel):
    start: Position
    end: Position


class SchematicLabel(BaseModel):
    text: str
    position: Position
    label_type: str = Field(default="net_label", description="Label type: net_label, global_label, hierarchical_label")


class SchematicInfo(BaseModel):
    file_path: str
    title: str = ""
    version: str = ""
    generator: str = ""
    num_symbols: int = 0
    num_wires: int = 0
    num_labels: int = 0
    num_sheets: int = 0


class SchematicReadResult(BaseModel):
    info: SchematicInfo
    symbols: list[SchematicSymbol] = Field(default_factory=list)
    wires: list[SchematicWire] = Field(default_factory=list)
    labels: list[SchematicLabel] = Field(default_factory=list)


# --- Library Models ---

class SymbolInfo(BaseModel):
    name: str
    library: str
    description: str = ""
    keywords: str = ""
    pin_count: int = 0
    datasheet: str = ""


class FootprintInfo(BaseModel):
    name: str
    library: str
    description: str = ""
    keywords: str = ""
    pad_count: int = 0
    smd: bool = False


# --- DRC Models ---

class DRCViolation(BaseModel):
    severity: str = Field(description="error, warning, or exclusion")
    type: str = Field(description="Violation type code")
    description: str
    position: Optional[Position] = None
    items: list[str] = Field(default_factory=list, description="Affected items")


class DRCResult(BaseModel):
    passed: bool
    error_count: int = 0
    warning_count: int = 0
    violations: list[DRCViolation] = Field(default_factory=list)
    report_file: str = ""


# --- Export Models ---

class ExportResult(BaseModel):
    success: bool
    output_files: list[str] = Field(default_factory=list)
    output_dir: str = ""
    message: str = ""


# --- Project Models ---

class ProjectInfo(BaseModel):
    name: str
    path: str
    board_file: Optional[str] = None
    schematic_file: Optional[str] = None
    has_board: bool = False
    has_schematic: bool = False
    kicad_version: str = ""


# --- Backend Info ---

class BackendInfo(BaseModel):
    name: str
    available: bool
    version: str = ""
    capabilities: list[str] = Field(default_factory=list)


class BackendStatusResult(BaseModel):
    active_backends: list[BackendInfo] = Field(default_factory=list)
    primary_backend: str = ""
    kicad_version: str = ""


# --- Generic Tool Response ---

class ToolResponse(BaseModel):
    status: str = "success"
    data: Any = None
    message: str = ""
