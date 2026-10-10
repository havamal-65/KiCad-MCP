"""TraceMaker runtime module — detection, WSL path mapping, invocation.

All subprocess activity is mocked at the ``subprocess.run`` boundary; no
KiCad, WSL, or TraceMaker install is needed. Platform-dependent branches
are driven by monkeypatching ``sys.platform``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kicad_mcp.utils.tracemaker_runtime as tm


@pytest.fixture(autouse=True)
def _reset_runtime_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh detection cache and clean env for every test."""
    for var in (tm.ENV_DISTRO, tm.ENV_BIN, tm.ENV_DISABLE):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(tm, "_probed", False)
    monkeypatch.setattr(tm, "_cached_runtime", None)


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=rc, stdout=stdout, stderr=stderr)


def _native_rt(binary: str = "/opt/tracemaker") -> tm.TracemakerRuntime:
    return tm.TracemakerRuntime(mode="native", distro=None, binary=binary, version="1.0.0")


def _wsl_rt() -> tm.TracemakerRuntime:
    return tm.TracemakerRuntime(
        mode="wsl",
        distro="tracemaker-build",
        binary="~/TraceMaker/build/cpu-only/src/app/tracemaker",
        version="1.0.0",
    )


# ---------------------------------------------------------------- disable env


def test_disabled_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tm.ENV_DISABLE, "1")
    assert tm.tracemaker_disabled() is True
    run = MagicMock()
    monkeypatch.setattr(subprocess, "run", run)
    assert tm.detect_tracemaker() is None
    run.assert_not_called()


def test_not_disabled_when_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tm.ENV_DISABLE, "")
    assert tm.tracemaker_disabled() is False


def test_not_disabled_when_env_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tm.ENV_DISABLE, "0")
    assert tm.tracemaker_disabled() is False


def test_not_disabled_when_env_unset() -> None:
    assert tm.tracemaker_disabled() is False


@pytest.mark.parametrize("value", ["false", "no"])
def test_disabled_for_any_nonzero_token(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Any non-empty value other than "0" disables — including "false"/"no".
    monkeypatch.setenv(tm.ENV_DISABLE, value)
    assert tm.tracemaker_disabled() is True


def test_disable_env_set_after_cached_success_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock(return_value=_completed(0, stdout="tracemaker 0.8.0\n"))
    monkeypatch.setattr(subprocess, "run", run)

    assert tm.detect_tracemaker() is not None  # probed and cached

    monkeypatch.setenv(tm.ENV_DISABLE, "1")
    assert tm.detect_tracemaker() is None  # disabled check precedes the cache
    assert run.call_count == 1


# ----------------------------------------------------------------- detection


def test_detect_wsl_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock(return_value=_completed(0, stdout="tracemaker 0.8.0\n"))
    monkeypatch.setattr(subprocess, "run", run)

    rt = tm.detect_tracemaker()

    assert rt is not None
    assert rt.mode == "wsl"
    assert rt.distro == "tracemaker-build"
    assert rt.binary == "~/TraceMaker/build/cpu-only/src/app/tracemaker"
    assert rt.version == "0.8.0"

    cmd = run.call_args.args[0]
    assert cmd[:6] == ["wsl.exe", "-d", "tracemaker-build", "-e", "bash", "-lc"]
    assert cmd[6] == (
        "test -x ~/TraceMaker/build/cpu-only/src/app/tracemaker"
        " && ~/TraceMaker/build/cpu-only/src/app/tracemaker version"
    )
    assert run.call_args.kwargs["timeout"] == 20


def test_detect_native_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv(tm.ENV_BIN, "/opt/tm/tracemaker")
    run = MagicMock(return_value=_completed(0, stdout="tracemaker 2.0\n"))
    monkeypatch.setattr(subprocess, "run", run)

    rt = tm.detect_tracemaker()

    assert rt is not None
    assert rt.mode == "native"
    assert rt.distro is None
    assert rt.binary == "/opt/tm/tracemaker"
    assert rt.version == "2.0"
    assert run.call_args.args[0] == ["/opt/tm/tracemaker", "version"]


def test_detect_version_with_v_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # "v0.9.1-rc1" must parse as "0.9.1-rc1", not "9.1-rc1".
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv(tm.ENV_BIN, "/opt/tm/tracemaker")
    run = MagicMock(return_value=_completed(0, stdout="tracemaker v0.9.1-rc1\n"))
    monkeypatch.setattr(subprocess, "run", run)
    rt = tm.detect_tracemaker()
    assert rt is not None
    assert rt.version == "0.9.1-rc1"


def test_detect_native_default_binary_tilde_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No ENV_BIN: the default "~/..." binary is expanded via os.path.expanduser.
    monkeypatch.setattr(sys, "platform", "linux")
    run = MagicMock(return_value=_completed(0, stdout="tracemaker 0.8.0\n"))
    monkeypatch.setattr(subprocess, "run", run)
    rt = tm.detect_tracemaker()
    assert rt is not None
    assert not rt.binary.startswith("~")
    assert rt.binary == os.path.expanduser(
        "~/TraceMaker/build/cpu-only/src/app/tracemaker"
    )
    assert run.call_args.args[0] == [rt.binary, "version"]


def test_detect_version_none_when_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv(tm.ENV_BIN, "/opt/tm/tracemaker")
    run = MagicMock(return_value=_completed(0, stdout="hello\n"))
    monkeypatch.setattr(subprocess, "run", run)
    rt = tm.detect_tracemaker()
    assert rt is not None
    assert rt.version is None


def test_detect_probe_nonzero_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock(return_value=_completed(1, stderr="no such distro"))
    monkeypatch.setattr(subprocess, "run", run)
    assert tm.detect_tracemaker() is None


def test_detect_probe_oserror_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock(side_effect=FileNotFoundError("wsl.exe not found"))
    monkeypatch.setattr(subprocess, "run", run)
    assert tm.detect_tracemaker() is None


def test_detect_probe_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock(side_effect=subprocess.TimeoutExpired(cmd="wsl.exe", timeout=20))
    monkeypatch.setattr(subprocess, "run", run)
    assert tm.detect_tracemaker() is None


def test_detect_caches_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock(return_value=_completed(0, stdout="TraceMaker 1.2.3\n"))
    monkeypatch.setattr(subprocess, "run", run)

    first = tm.detect_tracemaker()
    second = tm.detect_tracemaker()

    assert first is not None
    assert second is first
    assert run.call_count == 1


def test_detect_caches_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock(return_value=_completed(1))
    monkeypatch.setattr(subprocess, "run", run)

    assert tm.detect_tracemaker() is None
    assert tm.detect_tracemaker() is None
    assert run.call_count == 1


def test_detect_force_reprobes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock(return_value=_completed(0, stdout="TraceMaker 1.2.3\n"))
    monkeypatch.setattr(subprocess, "run", run)

    tm.detect_tracemaker()
    tm.detect_tracemaker(force=True)

    assert run.call_count == 2


def test_detect_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv(tm.ENV_DISTRO, "my-distro")
    monkeypatch.setenv(tm.ENV_BIN, "/home/me/tm")
    run = MagicMock(return_value=_completed(0, stdout="0.9"))
    monkeypatch.setattr(subprocess, "run", run)

    rt = tm.detect_tracemaker()

    assert rt is not None
    assert rt.distro == "my-distro"
    assert rt.binary == "/home/me/tm"
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["wsl.exe", "-d", "my-distro"]
    assert cmd[6] == "test -x /home/me/tm && /home/me/tm version"


# ---------------------------------------------------------------- to_wsl_path


def test_to_wsl_path_basic() -> None:
    assert tm.to_wsl_path(Path(r"D:\x\y")) == "/mnt/d/x/y"


def test_to_wsl_path_lowercases_drive_and_keeps_spaces() -> None:
    assert (
        tm.to_wsl_path(Path(r"C:\Users\some one\board v2.kicad_pcb"))
        == "/mnt/c/Users/some one/board v2.kicad_pcb"
    )


def test_to_wsl_path_drive_root() -> None:
    assert tm.to_wsl_path(Path(r"E:\\")) == "/mnt/e"


def test_to_wsl_path_rejects_unc() -> None:
    with pytest.raises(ValueError):
        tm.to_wsl_path(Path(r"\\server\share\board.kicad_pcb"))


def test_to_wsl_path_rejects_relative() -> None:
    with pytest.raises(ValueError):
        tm.to_wsl_path(Path("boards/board.kicad_pcb"))


def test_to_wsl_path_rejects_drive_relative() -> None:
    with pytest.raises(ValueError):
        tm.to_wsl_path(Path("D:board.kicad_pcb"))


# -------------------------------------------------------------- run_tracemaker


def test_run_success_native(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pcb_in = tmp_path / "in.kicad_pcb"
    pcb_in.write_text("(kicad_pcb)", encoding="utf-8")
    pcb_out = tmp_path / "out.kicad_pcb"
    pcb_out.write_text("(kicad_pcb)", encoding="utf-8")  # router "wrote" it

    run = MagicMock(return_value=_completed(0, stdout="Routed 14 nets, 0 unrouted\n"))
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(_native_rt(), pcb_in, pcb_out)

    assert result["status"] == "success"
    assert result["returncode"] == 0
    assert result["unrouted"] == 0
    assert "Routed 14 nets" in result["stdout_tail"]
    assert isinstance(result["wall_time_s"], float)
    assert result["wall_time_s"] >= 0.0
    assert run.call_args.args[0] == [
        "/opt/tracemaker",
        "route",
        str(pcb_in),
        "-o",
        str(pcb_out),
    ]
    assert run.call_args.kwargs["timeout"] == 900


def test_run_wsl_translates_paths_and_injects_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MagicMock(return_value=_completed(0))
    monkeypatch.setattr(subprocess, "run", run)

    tm.run_tracemaker(
        _wsl_rt(),
        Path(r"D:\proj\in.kicad_pcb"),
        Path(r"D:\proj\out.kicad_pcb"),
        work=7,
        time_limit_s=120,
        timeout_s=300,
    )

    cmd = run.call_args.args[0]
    assert cmd[:6] == ["wsl.exe", "-d", "tracemaker-build", "-e", "bash", "-lc"]
    assert cmd[6] == (
        "~/TraceMaker/build/cpu-only/src/app/tracemaker route"
        " /mnt/d/proj/in.kicad_pcb -o /mnt/d/proj/out.kicad_pcb"
        " --work 7 --time 120"
    )
    assert run.call_args.kwargs["timeout"] == 300


def test_run_wsl_quotes_paths_with_space_and_dollar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Spaces and "$" in the translated paths must survive the bash -lc
    # string; the binary itself stays unquoted so "~" still tilde-expands.
    run = MagicMock(return_value=_completed(0))
    monkeypatch.setattr(subprocess, "run", run)

    tm.run_tracemaker(
        _wsl_rt(),
        Path(r"C:\my proj$\in file.kicad_pcb"),
        Path(r"C:\my proj$\out file.kicad_pcb"),
    )

    cmd = run.call_args.args[0]
    assert cmd[6] == (
        "~/TraceMaker/build/cpu-only/src/app/tracemaker route"
        " '/mnt/c/my proj$/in file.kicad_pcb'"
        " -o '/mnt/c/my proj$/out file.kicad_pcb'"
    )


def test_run_native_flag_injection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run = MagicMock(return_value=_completed(0))
    monkeypatch.setattr(subprocess, "run", run)

    tm.run_tracemaker(
        _native_rt(),
        tmp_path / "in.kicad_pcb",
        tmp_path / "out.kicad_pcb",
        work=3,
        time_limit_s=60,
    )

    cmd = run.call_args.args[0]
    assert cmd[-4:] == ["--work", "3", "--time", "60"]


def test_run_omits_flags_when_not_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = MagicMock(return_value=_completed(0))
    monkeypatch.setattr(subprocess, "run", run)

    tm.run_tracemaker(_native_rt(), tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb")

    cmd = run.call_args.args[0]
    assert "--work" not in cmd
    assert "--time" not in cmd


def test_run_nonzero_returncode_is_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pcb_out = tmp_path / "out.kicad_pcb"
    pcb_out.write_text("(kicad_pcb)", encoding="utf-8")
    run = MagicMock(return_value=_completed(2, stderr="segfault-ish"))
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(_native_rt(), tmp_path / "in.kicad_pcb", pcb_out)

    assert result["status"] == "error"
    assert result["returncode"] == 2
    assert "segfault-ish" in result["stderr_tail"]


def test_run_missing_output_file_is_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = MagicMock(return_value=_completed(0, stdout="done"))
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(
        _native_rt(), tmp_path / "in.kicad_pcb", tmp_path / "never_written.kicad_pcb"
    )

    assert result["status"] == "error"
    assert result["returncode"] == 0


def test_run_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run = MagicMock(
        side_effect=subprocess.TimeoutExpired(
            cmd="tracemaker", timeout=5, output=b"partial out", stderr=b"partial err"
        )
    )
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(
        _native_rt(), tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb", timeout_s=5
    )

    assert result["status"] == "timeout"
    assert result["returncode"] is None
    assert result["stdout_tail"] == "partial out"
    assert result["stderr_tail"] == "partial err"
    assert result["unrouted"] is None
    # Native mode: no WSL-side cleanup call follows the kill.
    assert run.call_count == 1


def test_run_wsl_timeout_invokes_distro_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # run()'s timeout kills wsl.exe only — the module must then pkill the
    # Linux-side tracemaker inside the distro, best-effort.
    run = MagicMock(
        side_effect=[
            subprocess.TimeoutExpired(cmd="wsl.exe", timeout=5),
            _completed(0),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(
        _wsl_rt(),
        Path(r"D:\proj\in.kicad_pcb"),
        Path(r"D:\proj\out.kicad_pcb"),
        timeout_s=5,
    )

    assert result["status"] == "timeout"
    assert run.call_count == 2
    cleanup = run.call_args_list[1]
    assert cleanup.args[0] == [
        "wsl.exe",
        "-d",
        "tracemaker-build",
        "-e",
        "bash",
        "-lc",
        "pkill -f tracemaker || true",
    ]
    assert cleanup.kwargs["timeout"] == 10


def test_run_wsl_timeout_cleanup_failure_still_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MagicMock(
        side_effect=[
            subprocess.TimeoutExpired(cmd="wsl.exe", timeout=5),
            FileNotFoundError("wsl.exe vanished"),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(
        _wsl_rt(),
        Path(r"D:\proj\in.kicad_pcb"),
        Path(r"D:\proj\out.kicad_pcb"),
        timeout_s=5,
    )

    assert result["status"] == "timeout"
    assert result["returncode"] is None
    assert run.call_count == 2


def test_run_launch_failure_is_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = MagicMock(side_effect=FileNotFoundError("no such binary"))
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(
        _native_rt(), tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb"
    )

    assert result["status"] == "error"
    assert result["returncode"] is None
    assert "no such binary" in result["stderr_tail"]


def test_run_parses_unrouted_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pcb_out = tmp_path / "out.kicad_pcb"
    pcb_out.write_text("(kicad_pcb)", encoding="utf-8")
    run = MagicMock(return_value=_completed(0, stdout="Routing finished. Unrouted: 3\n"))
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(_native_rt(), tmp_path / "in.kicad_pcb", pcb_out)

    assert result["unrouted"] == 3


def test_run_unrouted_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pcb_out = tmp_path / "out.kicad_pcb"
    pcb_out.write_text("(kicad_pcb)", encoding="utf-8")
    run = MagicMock(return_value=_completed(0, stdout="all nets placed happily\n"))
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(_native_rt(), tmp_path / "in.kicad_pcb", pcb_out)

    assert result["unrouted"] is None


def test_run_tails_long_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pcb_out = tmp_path / "out.kicad_pcb"
    pcb_out.write_text("(kicad_pcb)", encoding="utf-8")
    long_out = "x" * 5000 + "TAIL_END"
    run = MagicMock(return_value=_completed(0, stdout=long_out))
    monkeypatch.setattr(subprocess, "run", run)

    result = tm.run_tracemaker(_native_rt(), tmp_path / "in.kicad_pcb", pcb_out)

    assert len(result["stdout_tail"]) == 2000
    assert result["stdout_tail"].endswith("TAIL_END")


# ------------------------------------------------------------ unrouted parse


def test_parse_unrouted_pass_count_is_not_a_count() -> None:
    # Regression: an iteration count near "incomplete" must NOT parse.
    assert tm._parse_unrouted("routing incomplete after 42 passes") is None


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("Unrouted: 42", 42),
        ("unrouted = 7", 7),
        ("42 unrouted", 42),
        ("3 nets unrouted", 3),
        ("42 connections remain incomplete", 42),
        ("incomplete: 5 connections", 5),
        ("routed 86 of 86", None),
        ("gave up after 9 iterations", None),
    ],
)
def test_parse_unrouted_noun_adjacency(stdout: str, expected: int | None) -> None:
    assert tm._parse_unrouted(stdout) == expected
