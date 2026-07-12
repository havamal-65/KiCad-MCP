# Symbol rotation audit fixture (F4 / #17 residual)

Ground-truth fixture for auditing `FileSchematicOps.get_symbol_pin_positions`
against **eeschema's own** pin placement at every rotation × mirror.

## Files

- `rotation_audit.kicad_sch` — one real KiCad symbol
  (`Regulator_Linear:LM7805_TO220`, three non-collinear pins) placed **12
  times**: rotation `0/90/180/270` × mirror `none/X/Y`, at well-separated
  on-grid origins. Reference names encode the orientation:
  `U<angle><N|X|Y>` (e.g. `U90N` = 90°, no mirror; `U270Y` = 270°, mirror-Y).
  The symbol definition is embedded in the file's `lib_symbols`, so the offline
  test needs no installed KiCad libraries.
- `ground_truth.json` — the absolute pin position eeschema assigns to every
  pin of every instance: `{ "U90N": { "pins": { "1": [x, y], … } }, … }`.

## Why the ground truth is trustworthy (the oracle)

A placed symbol instance stores only `(at x y angle)` + `(mirror …)`; KiCad never
writes a pin's absolute position to disk — it recomputes it on load from the
symbol's `TRANSFORM`. There is also no eeschema scripting API. So the oracle is
KiCad's own connectivity engine, driven headlessly:

> Place a `no_connect` marker at each position `get_symbol_pin_positions`
> computes, then run `kicad-cli sch erc`. If a marker sits exactly on eeschema's
> pin, that pin is silenced and the marker is "used"; if it is off by even one
> grid step, ERC fires `pin_not_connected` **and** `no_connect_dangling`.

`ground_truth.json` is the output of the (corrected) transform, **proven equal
to eeschema** by that round-trip returning zero violations for all 36 pins.
`tests/integration/test_symbol_pin_rotation_live.py` re-runs the round-trip;
`tests/test_symbol_pin_rotation.py` pins the values offline.

## Regenerating

Only needed if the audit symbol/instances change (KiCad + stock libraries
required). The generator lives in this repo's history; it creates the schematic
with `FileSchematicOps.create_schematic` + `add_component(rotation=…, mirror=…)`,
then writes `ground_truth.json` from `get_symbol_pin_positions` and verifies the
no-connect round-trip is clean before committing.

## The bug this fixture pins (#17 residual)

The original transform used the transposed rotation matrix (`px·cos − py·sin /
px·sin + py·cos`, with a pre-rotation mirror). It matched eeschema only at
0°/180° and for mirrored 90°/270°, but reflected pins through the symbol origin
at **un-mirrored 90°/270°** (15.24 mm off for this symbol) — the schematic-domain
twin of the board rotation bug fixed in `b65df77`. The corrected transform
Y-flips the library point first and uses KiCad's counterclockwise convention,
applying mirror as a post-rotation axis negation.
