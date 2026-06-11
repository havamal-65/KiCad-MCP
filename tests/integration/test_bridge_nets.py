"""REQ-COV-010 — assign_net multi-pad regression.

Anchors commit 9cea376: the bridge's assign_net must update *every*
pad sharing the requested pad number, not just the first one. The
ESP32-C3-WROOM-02 thermal pad is the documented case — KiCad models
it as pad number "19" rendered as a multi-rect array (13 physical
pads share the same logical number). Before the fix, only one of
those got the net assignment and DRC reported false-positive shorts.

Skips cleanly if the system KiCad install lacks the RF_Module library.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_mcp.backends.plugin_backend import _tcp_call


pytestmark = pytest.mark.integration

_ESP32_FP = "RF_Module:ESP32-C3-WROOM-02"


def _board_path() -> str:
    return _tcp_call("get_active_project", 5.0)["board_path"]


def _count_pad_with_net(board_path: str, reference: str, pad_number: str,
                        net_name: str) -> tuple[int, int]:
    """Return (pads_with_number, pads_with_number_and_net) for the named footprint."""
    text = Path(board_path).read_text(encoding="utf-8")
    # Locate the footprint block.
    fp_iter = re.finditer(r"\(footprint\s+", text)
    for match in fp_iter:
        start = match.start()
        depth = 0
        i = start
        while i < len(text):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    block = text[start:i + 1]
                    break
            i += 1
        else:
            continue
        if not re.search(rf'\(property\s+"Reference"\s+"{re.escape(reference)}"', block):
            continue
        # Walk every (pad "<pad_number>" ...) in the block — pad blocks are balanced too.
        total = 0
        with_net = 0
        pad_iter = re.finditer(rf'\(pad\s+"{re.escape(pad_number)}"\s+', block)
        for pmatch in pad_iter:
            pstart = pmatch.start()
            pdepth = 0
            j = pstart
            while j < len(block):
                ch = block[j]
                if ch == "(":
                    pdepth += 1
                elif ch == ")":
                    pdepth -= 1
                    if pdepth == 0:
                        pad_block = block[pstart:j + 1]
                        break
                j += 1
            else:
                continue
            total += 1
            if re.search(rf'\(net\s+\d+\s+"{re.escape(net_name)}"\s*\)', pad_block):
                with_net += 1
        return total, with_net
    return 0, 0


def test_assign_net_covers_all_pads_with_shared_number(bridge_session):
    """REQ-COV-010: assign_net updates EVERY pad sharing the requested number.

    Multi-rect thermal pad regression anchored by commit 9cea376. The
    ESP32-C3-WROOM-02 thermal pad is pad number "19" — KiCad models the
    exposed pad as ~13 physical pad shapes that all carry pad number "19".
    """
    path = _board_path()
    ref = "T10_U1"
    pad = "19"
    net = "T10_GND"

    # Place the ESP32 footprint. If the system KiCad lacks RF_Module, skip.
    try:
        _tcp_call(
            "place_component", 10.0,
            path=path, reference=ref, footprint=_ESP32_FP,
            x=100.0, y=80.0, rotation=0,
        )
    except RuntimeError as exc:
        if "Could not find library" in str(exc) or "not found in" in str(exc):
            pytest.skip(f"{_ESP32_FP} not available on this KiCad install: {exc}")
        raise

    result = _tcp_call(
        "assign_net", 5.0,
        path=path, reference=ref, pad=pad, net=net,
    )
    assert isinstance(result, dict), f"unexpected assign_net response: {result!r}"
    pads_updated = result.get("pads_updated", 0)
    assert pads_updated >= 2, (
        f"assign_net only updated {pads_updated} pad(s) for pad number {pad!r} — "
        f"multi-rect thermal pad regression. Commit 9cea376 should have made this >=2."
    )

    total, with_net = _count_pad_with_net(path, ref, pad, net)
    assert total >= 2, (
        f"expected multiple pads numbered {pad!r} on ESP32, got {total}. "
        f"KiCad's ESP32-C3-WROOM-02 thermal pad inventory may have changed."
    )
    assert with_net == total, (
        f"{with_net} of {total} pad-{pad} entries have net='{net}' in saved file — "
        f"unassigned pads will cause DRC false-positive shorts. Commit 9cea376 regression."
    )
