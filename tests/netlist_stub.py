"""Shared sync-test helper (KIR4 NS, spec amendment A3).

Sync v2 consumes KiCad's own netlist (`kicad-cli sch export netlist`). CI has
no kicad-cli, so end-to-end sync tests stub `get_drc_ops().generate_netlist`
with an object that writes a minimal kicadsexpr netlist rendered from the
test's component/net lists, and resolve footprints from a tmp `.pretty` dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# A minimal but real footprint: two pads, courtyard-free. Enough for
# _embed_kicad_mod_as_pcb_footprint and the netlist pad assignments.
MINIMAL_MOD = """(footprint "TESTFP"
  (layer "F.Cu")
  (attr smd)
  (property "Reference" "REF**" (at 0 -2 0) (layer "F.SilkS")
    (effects (font (size 1 1) (thickness 0.15)))
  )
  (property "Value" "VAL**" (at 0 2 0) (layer "F.Fab")
    (effects (font (size 1 1) (thickness 0.15)))
  )
  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""


def render_netlist(
    components: list[dict[str, str]],
    nets: list[dict[str, Any]] | None = None,
) -> str:
    """Render a minimal kicadsexpr netlist.

    components: [{"ref", "value", "footprint"}]
    nets: [{"name", "nodes": [(ref, pad), ...]}]
    """
    comp_blocks = "".join(
        f'    (comp (ref "{c["ref"]}") (value "{c.get("value", "")}") '
        f'(footprint "{c.get("footprint", "")}"))\n'
        for c in components
    )
    net_blocks = ""
    for i, net in enumerate(nets or []):
        nodes = "".join(
            f' (node (ref "{ref}") (pin "{pad}"))'
            for ref, pad in net["nodes"]
        )
        net_blocks += f'    (net (code "{i + 1}") (name "{net["name"]}"){nodes})\n'
    return (
        "(export (version \"E\")\n"
        "  (components\n" + comp_blocks + "  )\n"
        "  (nets\n" + net_blocks + "  )\n"
        ")\n"
    )


class DrcOpsStub:
    """generate_netlist stand-in: writes *netlist_text* and reports success."""

    def __init__(self, netlist_text: str) -> None:
        self._text = netlist_text

    def generate_netlist(self, schematic_path: Path, output: Path,
                         fmt: str = "kicadsexpr") -> dict[str, Any]:
        Path(output).write_text(self._text, encoding="utf-8")
        return {
            "generated": True,
            "output_file": str(output),
            "format": "kicadsexpr",
            "returncode": 0,
        }


def make_pretty_lib(tmp_path: Path, names: list[str]) -> Path:
    """Create a tmp .pretty dir holding MINIMAL_MOD under each *names* entry."""
    pretty = tmp_path / "Test.pretty"
    pretty.mkdir(exist_ok=True)
    for name in names:
        (pretty / f"{name}.kicad_mod").write_text(
            MINIMAL_MOD.replace("TESTFP", name), encoding="utf-8")
    return pretty
