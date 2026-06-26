# Project fixtures

## `diffpair_ground_truth.kicad_pro` / `diffpair_ground_truth.kicad_dru`

**Provenance:** verbatim copies of KiCad 9's shipped demo project
`vme-wren` (`<KiCad>/share/kicad/demos/vme-wren/vme-wren.{kicad_pro,kicad_dru}`),
renamed only. Unmodified content — these are *captured* real-KiCad-authored
files, the ground truth for the §6.4 differential-pair format (REQ-FMT-001).

**License:** KiCad demo projects are distributed with KiCad under
GPL-3.0-or-later. Retained here unmodified for test purposes with attribution.

**What they pin (the writer must reproduce these structures):**

- **Diff-pair fields on a netclass** — each `net_settings.classes[i]` dict carries
  `diff_pair_width`, `diff_pair_gap`, `diff_pair_via_gap`, plus `priority`
  (named classes use small ascending ints `0,1,2,…`; `Default` uses
  `2147483647`) and the full netclass shape (`clearance`, `track_width`,
  `via_diameter`, `via_drill`, colors, `bus_width`, `line_style`, `microvia_*`,
  `wire_width`, `name`). Named diff-pair classes here: `DDR4_BYTE0..3`,
  `DDR4_CMD`, `FPGA_HD`, `FPGA_HP`, `VMEPX`, `zse_50r`.
- **Net → netclass assignment** — `net_settings.netclass_patterns`: a list of
  `{"netclass": "<name>", "pattern": "<glob>"}` (key order `netclass` then
  `pattern`). `net_settings.netclass_assignments` is `null`. Multiple patterns
  may map to one class.
- **Length matching + diff-pair geometry (`.kicad_dru`)** — `(version 1)` header;
  length rules are
  `(rule "<name>" (constraint length (min Xmm) (max Ymm) (opt Zmm)) (condition "A.NetClass == '<class>' && A.fromTo('REFA-*','REFB-*')"))`;
  diff-pair rules use `(constraint diff_pair_gap (min..) (max..) (opt..))` +
  `(condition "A.inDiffPair('*')")`.
