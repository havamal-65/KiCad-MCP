"""F-DV10 (DV build) — IPC add_zone must persist thermal relief settings.

kipy's ``ZoneConnectionSettings.thermal_spokes`` getter returns a wrapper
built with the positional ``proto`` argument, i.e. a CopyFrom()'d DETACHED
copy — assigning ``.gap``/``.width`` on it mutates a throwaway, and the zone
file stores ``thermal_gap 0`` / ``thermal_bridge_width 0``. Caught by the
Phase-5 audit of the DV ecc83 recreation (file held 0/0 where the reference
holds 0.254/0.50038). ``_apply_zone_connection`` binds its own wrapper to
the live proto via ``proto_ref``.
"""

from __future__ import annotations

import pytest

kbt = pytest.importorskip("kipy.board_types")

from kipy.proto.board.board_types_pb2 import ZoneConnectionStyle  # noqa: E402

from kicad_mcp.backends.ipc_backend import _apply_zone_connection  # noqa: E402


def _zone_connection_proto(zone: object):  # type: ignore[no-untyped-def]
    return zone._proto.copper_settings.connection  # type: ignore[attr-defined]


def test_thermal_gap_and_width_reach_the_zone_proto() -> None:
    zone = kbt.Zone()
    _apply_zone_connection(zone, "thermal", 254000, 500380)
    conn = _zone_connection_proto(zone)
    assert conn.thermal_spokes.gap.value_nm == 254000
    assert conn.thermal_spokes.width.value_nm == 500380
    assert conn.zone_connection == ZoneConnectionStyle.ZCS_THERMAL


def test_pad_connection_modes_map() -> None:
    for mode, style in (
        ("thermal", ZoneConnectionStyle.ZCS_THERMAL),
        ("solid", ZoneConnectionStyle.ZCS_FULL),
        ("none", ZoneConnectionStyle.ZCS_NONE),
    ):
        zone = kbt.Zone()
        _apply_zone_connection(zone, mode, 100000, 200000)
        assert _zone_connection_proto(zone).zone_connection == style


def test_upstream_kipy_thermal_spokes_getter_still_detached() -> None:
    """Documents the upstream quirk the workaround exists for.

    If a kipy upgrade makes this fail (the getter starts returning a live
    proto_ref wrapper), the workaround in ``_apply_zone_connection`` is
    still correct — delete this pin and optionally simplify the helper.
    """
    zone = kbt.Zone()
    connection = zone.connection
    assert connection is not None
    connection.thermal_spokes.gap = 254000
    assert _zone_connection_proto(zone).thermal_spokes.gap.value_nm == 0
