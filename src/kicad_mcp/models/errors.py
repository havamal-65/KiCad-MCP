"""Custom exception hierarchy for KiCad MCP server."""

from __future__ import annotations

from typing import Any


class KiCadMCPError(Exception):
    """Base exception for all KiCad MCP errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class BackendError(KiCadMCPError):
    """Error originating from a backend operation."""


class BackendNotAvailableError(BackendError):
    """Requested backend is not available in this environment."""


class KiCadFileOpenError(BackendError):
    """File write blocked because KiCad has the file open in memory.

    Writing directly to .kicad_sch or .kicad_pcb while KiCad is running
    causes KiCad to overwrite changes on save with its stale in-memory state.
    """


class ConnectionError(BackendError):
    """Failed to connect to KiCad instance."""


class CapabilityNotSupportedError(BackendError):
    """The active backend(s) do not support this operation."""


class SafeRefuseError(BackendError):
    """A board write was refused: no live path (IPC/bridge) can reach the open
    board, and KiCad appears to be running — writing the .kicad_pcb on disk
    would be silently clobbered by KiCad's stale in-memory state on its next
    save (the #14C lesson). Disk writes are only safe with KiCad closed.

    The message carries the remedy; details carry
    {capability, remedy, paths_tried}.
    """

    def __init__(
        self, message: str, *,
        capability: str, remedy: str, paths_tried: list[str],
    ) -> None:
        super().__init__(message, {
            "capability": capability,
            "remedy": remedy,
            "paths_tried": paths_tried,
        })
        self.capability = capability
        self.remedy = remedy
        self.paths_tried = paths_tried


class ProjectError(KiCadMCPError):
    """Error related to project operations."""


class FileNotFoundError(ProjectError):
    """KiCad project file not found."""


class InvalidFileFormatError(ProjectError):
    """File is not a valid KiCad file format."""


class ValidationError(KiCadMCPError):
    """Input validation failed."""


class InvalidReferenceError(ValidationError):
    """Component reference designator is invalid."""


class InvalidNetNameError(ValidationError):
    """Net name is invalid or doesn't exist."""


class InvalidPathError(ValidationError):
    """File path is invalid or inaccessible."""


class ExportError(KiCadMCPError):
    """Error during file export operation."""


class DRCError(KiCadMCPError):
    """Error during design rule check."""


class LibraryError(KiCadMCPError):
    """Error accessing component libraries."""


class LibraryManageError(KiCadMCPError):
    """Error during library management operations."""


class GitOperationError(LibraryManageError):
    """Error executing a git operation (clone, pull, etc.)."""


class LibraryImportError(LibraryManageError):
    """Error importing a symbol or footprint between libraries."""


class RoutingError(KiCadMCPError):
    """Error during auto-routing operations."""
