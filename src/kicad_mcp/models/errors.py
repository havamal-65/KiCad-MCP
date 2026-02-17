"""Custom exception hierarchy for KiCad MCP server."""

from __future__ import annotations


class KiCadMCPError(Exception):
    """Base exception for all KiCad MCP errors."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class BackendError(KiCadMCPError):
    """Error originating from a backend operation."""


class BackendNotAvailableError(BackendError):
    """Requested backend is not available in this environment."""


class ConnectionError(BackendError):
    """Failed to connect to KiCad instance."""


class CapabilityNotSupportedError(BackendError):
    """The active backend(s) do not support this operation."""


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
