"""Tests for IPCBackend Linux-safe project/document discovery."""

from __future__ import annotations

from pathlib import Path

from kicad_mcp.backends import ipc_backend


class _DummyProject:
    def __init__(self) -> None:
        self.name = "linux_demo"
        self.path = Path("/tmp/linux_demo/linux_demo.kicad_pro")
        self._vars = {"REV": "A"}

    def get_text_variables(self) -> dict[str, str]:
        return dict(self._vars)

    def set_text_variables(self, variables: dict[str, str]) -> None:
        self._vars.update(variables)


class _DummyDoc:
    def __init__(self, project: _DummyProject) -> None:
        self.project = project
        self.path = Path("/tmp/linux_demo/linux_demo.kicad_pcb")


class _DummyBoard:
    def __init__(self, project: _DummyProject) -> None:
        self.document = _DummyDoc(project)


class _NoOpenDocsKiCad:
    """Simulates KiCad Linux IPC where GetOpenDocuments has no handler."""

    def __init__(self) -> None:
        self.project = _DummyProject()
        self.board = _DummyBoard(self.project)
        self.open_documents_calls = 0

    def get_open_documents(self, _doc_type: object):
        self.open_documents_calls += 1
        raise RuntimeError("ApiError: no handler available")

    def get_board(self) -> _DummyBoard:
        return self.board


def test_get_active_project_falls_back_to_board_document(monkeypatch):
    fake_kicad = _NoOpenDocsKiCad()
    monkeypatch.setattr(ipc_backend, "_get_kicad", lambda: fake_kicad)

    backend = ipc_backend.IPCBackend()
    result = backend.get_active_project()
    expected_project_path = str(Path("/tmp/linux_demo/linux_demo.kicad_pro"))
    expected_pcb_path = str(Path("/tmp/linux_demo/linux_demo.kicad_pcb"))

    assert result["project_name"] == "linux_demo"
    assert result["project_path"] == expected_project_path
    assert {"type": "pcb", "path": expected_pcb_path} in result["open_documents"]
    # Unsupported GetOpenDocuments should be detected once, then skipped.
    assert fake_kicad.open_documents_calls == 1


def test_get_text_variables_falls_back_to_board_document(monkeypatch):
    fake_kicad = _NoOpenDocsKiCad()
    monkeypatch.setattr(ipc_backend, "_get_kicad", lambda: fake_kicad)

    backend = ipc_backend.IPCBackend()
    result = backend.get_text_variables(Path("/tmp/linux_demo/linux_demo.kicad_pro"))

    assert result["status"] == "success"
    assert result["variables"] == {"REV": "A"}


def test_set_text_variables_falls_back_to_board_document(monkeypatch):
    fake_kicad = _NoOpenDocsKiCad()
    monkeypatch.setattr(ipc_backend, "_get_kicad", lambda: fake_kicad)

    backend = ipc_backend.IPCBackend()
    result = backend.set_text_variables(
        Path("/tmp/linux_demo/linux_demo.kicad_pro"),
        {"REV": "B", "DATE": "2026-03-03"},
    )

    assert result["status"] == "success"
    assert result["variables_set"] == 2
    assert fake_kicad.project.get_text_variables() == {"REV": "B", "DATE": "2026-03-03"}
