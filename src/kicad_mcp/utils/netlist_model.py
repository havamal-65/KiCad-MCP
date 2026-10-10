"""Parse kicad-cli netlists into an immutable model (KIR4 NS / REQ-NS-1).

The netlist is KiCad's own connectivity output (`kicad-cli sch export netlist
--format kicadsexpr`) — the authoritative source sync uses for components and
pad nets, replacing any MCP-side schematic connectivity analysis. Multi-unit
symbols arrive already merged per reference (all units' pins under one ref).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class NetlistParseError(ValueError):
    """Raised when a netlist file cannot be parsed into the model."""


@dataclass(frozen=True)
class NetlistComponent:
    reference: str
    value: str
    footprint: str          # "Lib:Name" or "" when the schematic omits it


@dataclass(frozen=True)
class NetlistNet:
    name: str
    nodes: tuple[tuple[str, str], ...]   # ((reference, pad), ...) in file order


@dataclass(frozen=True)
class NetlistModel:
    components: tuple[NetlistComponent, ...]
    nets: tuple[NetlistNet, ...]

    def pad_net_map(self) -> dict[tuple[str, str], str]:
        """(reference, pad) -> net name, EXCLUDING single-node 'unconnected-*' nets.

        KiCad represents genuinely-unconnected pads as single-node nets named
        ``unconnected-(REF-PadN)``; MCP-synced boards leave those pads netless
        instead (named representational difference, NS spec §2).
        """
        mapping: dict[tuple[str, str], str] = {}
        for net in self.nets:
            if net.name.startswith("unconnected-") and len(net.nodes) == 1:
                continue
            for reference, pad in net.nodes:
                mapping[(reference, pad)] = net.name
        return mapping


def _first_str(node: list[Any], tag: str) -> str | None:
    """First string argument of the first child list whose head is *tag*."""
    for child in node[1:]:
        if isinstance(child, list) and len(child) >= 2 and child[0] == tag:
            if isinstance(child[1], str):
                return child[1]
    return None


def _children(node: list[Any], tag: str) -> list[list[Any]]:
    return [
        child for child in node[1:]
        if isinstance(child, list) and child and child[0] == tag
    ]


def parse_kicad_netlist(path: Path) -> NetlistModel:
    """Parse a kicadsexpr netlist into a :class:`NetlistModel`.

    Reads ``(components (comp (ref)(value)(footprint)))`` and
    ``(nets (net (name) (node (ref)(pin))))``. Missing (value)/(footprint)
    become "". Raises :class:`NetlistParseError` on an unreadable file, a
    missing (components) or (nets) section, or a (comp) without (ref).
    """
    from kicad_mcp.utils.sexp_parser import parse_sexp_file

    try:
        tree = parse_sexp_file(path)
    except Exception as exc:
        raise NetlistParseError(f"could not parse netlist {path}: {exc}") from exc

    components_node: list[Any] | None = None
    nets_node: list[Any] | None = None
    for node in tree:
        if isinstance(node, list) and node:
            if node[0] == "components":
                components_node = node
            elif node[0] == "nets":
                nets_node = node

    if components_node is None:
        raise NetlistParseError(f"netlist has no (components) section: {path}")
    if nets_node is None:
        raise NetlistParseError(f"netlist has no (nets) section: {path}")

    components: list[NetlistComponent] = []
    for comp in _children(components_node, "comp"):
        reference = _first_str(comp, "ref")
        if reference is None:
            raise NetlistParseError(f"(comp) without (ref) in {path}")
        components.append(NetlistComponent(
            reference=reference,
            value=_first_str(comp, "value") or "",
            footprint=_first_str(comp, "footprint") or "",
        ))

    nets: list[NetlistNet] = []
    for net in _children(nets_node, "net"):
        name = _first_str(net, "name") or ""
        nodes: list[tuple[str, str]] = []
        for node in _children(net, "node"):
            node_ref = _first_str(node, "ref")
            node_pin = _first_str(node, "pin")
            if node_ref is not None and node_pin is not None:
                nodes.append((node_ref, node_pin))
        nets.append(NetlistNet(name=name, nodes=tuple(nodes)))

    return NetlistModel(components=tuple(components), nets=tuple(nets))
