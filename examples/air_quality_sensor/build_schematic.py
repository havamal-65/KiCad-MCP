"""Build the air quality sensor schematic using the KiCad MCP file backend.

Components:
  U1  ATtiny85-20S          MCU (I2C master, SOIC-8)
  U2  SCD41                 CO2 / temperature / humidity sensor (I2C)
  U3  SGP41                 VOC / NOx sensor (I2C)
  U4  AMS1117-3.3           3.3 V LDO regulator (SOT-223)
  R1  4.7 kΩ resistor       SDA pull-up
  R2  4.7 kΩ resistor       SCL pull-up
  C1  100 nF capacitor      MCU decoupling
  C2  100 nF capacitor      SCD41 decoupling
  C3  100 nF capacitor      SGP41 decoupling
  C4  10 µF capacitor       Bulk 3.3 V bypass
  C5  100 nF capacitor      LDO output decoupling
  C6  10 µF capacitor       LDO input bypass
  J1  Conn_01x02            Power input (+5 V / GND)
  J2  Conn_01x04            I2C debug header (3V3 / GND / SDA / SCL)

Run from the repo root:
    python examples/air_quality_sensor/build_schematic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from kicad_mcp.backends.file_backend import FileBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
SENSOR_LIB = PROJECT_DIR / "libs" / "Sensors.kicad_sym"
SCH_PATH = PROJECT_DIR / "air_quality_sensor.kicad_sch"

# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------
# KiCad library symbols use Y-up coordinates.
# The file backend already applies py_sch = -py_lib when computing absolute
# positions via get_symbol_pin_positions(), so pin coordinates returned by
# that method are already in schematic (Y-down) space.
#
# For symbols whose pins we computed manually below, the formula is:
#   abs_x = sx + px_lib
#   abs_y = sy - py_lib        (Y-down)


# ---------------------------------------------------------------------------
# Component placements (center of each symbol)
# ---------------------------------------------------------------------------
# Power / LDO section
J1_POS  = (20,  55)   # Conn_01x02  – power input
U4_POS  = (55,  50)   # AMS1117-3.3 – LDO
C6_POS  = (35,  65)   # 10 µF LDO input
C5_POS  = (75,  65)   # 100 nF LDO output
C4_POS  = (90,  65)   # 10 µF bulk 3.3 V

# Pull-ups
R1_POS  = (115, 50)   # 4.7 kΩ SDA
R2_POS  = (130, 50)   # 4.7 kΩ SCL

# MCU
U1_POS  = (120, 100)  # ATtiny85-20S
C1_POS  = (145,  90)  # 100 nF MCU decoupling

# Sensors
U2_POS  = (175,  80)  # SCD41
U3_POS  = (175, 130)  # SGP41
C2_POS  = (200,  75)  # 100 nF SCD41 decoupling
C3_POS  = (200, 125)  # 100 nF SGP41 decoupling

# Debug header
J2_POS  = (20,  140)  # Conn_01x04


def main() -> None:
    if SCH_PATH.exists():
        print(f"Removing existing schematic: {SCH_PATH}")
        SCH_PATH.unlink()

    # ------------------------------------------------------------------
    # Initialise file backend and inject custom sensor library
    # ------------------------------------------------------------------
    backend = FileBackend()
    ops = backend.get_schematic_ops()

    # Ensure the library list is populated, then prepend our custom lib
    ops._resolve_symbol_libs()
    ops._symbol_libs = [SENSOR_LIB] + ops._symbol_libs

    # ------------------------------------------------------------------
    # Create a fresh schematic
    # ------------------------------------------------------------------
    print("Creating schematic …")
    ops.create_schematic(SCH_PATH, title="Air Quality Sensor", revision="1.0")

    # ------------------------------------------------------------------
    # Place components
    # ------------------------------------------------------------------
    print("Placing components …")

    sx, sy = J1_POS
    ops.add_component(SCH_PATH, "Connector_Generic:Conn_01x02", "J1", "Conn_01x02", sx, sy)

    sx, sy = U4_POS
    ops.add_component(SCH_PATH, "Regulator_Linear:AMS1117-3.3", "U4", "AMS1117-3.3", sx, sy)

    for ref, val, pos in [
        ("C6", "10uF",  C6_POS),
        ("C5", "100nF", C5_POS),
        ("C4", "10uF",  C4_POS),
        ("C1", "100nF", C1_POS),
        ("C2", "100nF", C2_POS),
        ("C3", "100nF", C3_POS),
    ]:
        ops.add_component(SCH_PATH, "Device:C", ref, val, pos[0], pos[1])

    for ref, val, pos in [
        ("R1", "4.7k", R1_POS),
        ("R2", "4.7k", R2_POS),
    ]:
        ops.add_component(SCH_PATH, "Device:R", ref, val, pos[0], pos[1])

    sx, sy = U1_POS
    ops.add_component(
        SCH_PATH, "MCU_Microchip_ATtiny:ATtiny85-20S", "U1", "ATtiny85-20S", sx, sy
    )

    sx, sy = U2_POS
    ops.add_component(SCH_PATH, "Sensors:SCD41", "U2", "SCD41", sx, sy)

    sx, sy = U3_POS
    ops.add_component(SCH_PATH, "Sensors:SGP41", "U3", "SGP41", sx, sy)

    sx, sy = J2_POS
    ops.add_component(SCH_PATH, "Connector_Generic:Conn_01x04", "J2", "Conn_01x04", sx, sy)

    # ------------------------------------------------------------------
    # Resolve pin positions for each IC
    # ------------------------------------------------------------------
    print("Resolving pin positions …")

    def gpp(ref: str) -> dict[str, tuple[float, float]]:
        """Call get_symbol_pin_positions and return {pin_num: (x, y)}."""
        result = ops.get_symbol_pin_positions(SCH_PATH, ref)
        pp = result.get("pin_positions", {})
        if not pp:
            raise RuntimeError(f"No pin positions returned for {ref}")
        return {num: (pos["x"], pos["y"]) for num, pos in pp.items()}

    u1_pins = gpp("U1")   # ATtiny85-20S  (extends ATtiny25V-10S – resolved by MCP)
    u4_pins = gpp("U4")   # AMS1117-3.3   (extends AP1117-15     – resolved by MCP)
    j1_pins = gpp("J1")   # Conn_01x02
    j2_pins = gpp("J2")   # Conn_01x04
    u2_pins = gpp("U2")   # SCD41  (custom library)
    u3_pins = gpp("U3")   # SGP41  (custom library)

    # Device:R/C – same pin layout (pin 1 = top, pin 2 = bottom)
    def rc_pins(pos: tuple[float, float]) -> dict[str, tuple[float, float]]:
        x, y = pos
        return {"1": (x, y - 3.81), "2": (x, y + 3.81)}

    # ------------------------------------------------------------------
    # Power symbols
    # ------------------------------------------------------------------
    print("Adding power symbols …")

    def pwr(name: str, xy: tuple[float, float]) -> None:
        ops.add_power_symbol(SCH_PATH, name, xy[0], xy[1])

    # J1 – power input connector
    pwr("+5V", j1_pins["1"])
    pwr("GND", j1_pins["2"])

    # U4 AMS1117-3.3
    pwr("+5V",  u4_pins["3"])    # VI  ← 5 V input
    pwr("+3V3", u4_pins["2"])    # VO  → 3.3 V rail
    pwr("GND",  u4_pins["1"])    # GND tab

    # LDO input / output capacitors
    c6 = rc_pins(C6_POS)
    pwr("+5V",  c6["1"])
    pwr("GND",  c6["2"])

    c5 = rc_pins(C5_POS)
    pwr("+3V3", c5["1"])
    pwr("GND",  c5["2"])

    c4 = rc_pins(C4_POS)
    pwr("+3V3", c4["1"])
    pwr("GND",  c4["2"])

    # I2C pull-up resistors (top pin to +3V3; bottom connects to SDA/SCL net)
    pwr("+3V3", rc_pins(R1_POS)["1"])
    pwr("+3V3", rc_pins(R2_POS)["1"])

    # U1 ATtiny85
    pwr("+3V3", u1_pins["8"])    # VCC
    pwr("GND",  u1_pins["4"])    # GND

    # C1 MCU decoupling
    c1 = rc_pins(C1_POS)
    pwr("+3V3", c1["1"])
    pwr("GND",  c1["2"])

    # U2 SCD41
    pwr("+3V3", u2_pins["1"])    # VDD
    pwr("GND",  u2_pins["2"])    # GND
    pwr("GND",  u2_pins["5"])    # SEL → GND = I2C addr 0x62

    # C2 SCD41 decoupling
    c2 = rc_pins(C2_POS)
    pwr("+3V3", c2["1"])
    pwr("GND",  c2["2"])

    # U3 SGP41
    pwr("+3V3", u3_pins["1"])    # VDD
    pwr("+3V3", u3_pins["2"])    # VDDH tied to VDD (3.3 V operation)
    pwr("GND",  u3_pins["3"])    # GND

    # C3 SGP41 decoupling
    c3 = rc_pins(C3_POS)
    pwr("+3V3", c3["1"])
    pwr("GND",  c3["2"])

    # J2 debug header (power pins)
    pwr("+3V3", j2_pins["1"])
    pwr("GND",  j2_pins["2"])

    # ------------------------------------------------------------------
    # Net labels  (SDA / SCL)
    # ------------------------------------------------------------------
    print("Adding net labels …")

    def lbl(net: str, xy: tuple[float, float]) -> None:
        ops.add_label(SCH_PATH, net, xy[0], xy[1])

    # SDA net
    lbl("SDA", rc_pins(R1_POS)["2"])    # R1 bottom → SDA pull-up
    lbl("SDA", u1_pins["5"])             # U1 PB0 / MOSI / SDA
    lbl("SDA", u2_pins["4"])             # U2 SCD41 SDA
    lbl("SDA", u3_pins["4"])             # U3 SGP41 SDA
    lbl("SDA", j2_pins["3"])             # J2 pin 3 SDA

    # SCL net
    lbl("SCL", rc_pins(R2_POS)["2"])    # R2 bottom → SCL pull-up
    lbl("SCL", u1_pins["7"])             # U1 PB2 / SCK / SCL
    lbl("SCL", u2_pins["3"])             # U2 SCD41 SCL (left-side pin)
    lbl("SCL", u3_pins["5"])             # U3 SGP41 SCL
    lbl("SCL", j2_pins["4"])             # J2 pin 4 SCL

    # ------------------------------------------------------------------
    # No-connect markers on unused / N.C. pins
    # ------------------------------------------------------------------
    print("Adding no-connect markers …")

    def nc(xy: tuple[float, float]) -> None:
        ops.add_no_connect(SCH_PATH, xy[0], xy[1])

    # U1 unused GPIO
    nc(u1_pins["6"])   # PB1 / MISO
    nc(u1_pins["2"])   # PB3 / XTAL1
    nc(u1_pins["3"])   # PB4 / XTAL2
    nc(u1_pins["1"])   # PB5 / RESET

    # U2 SCD41 N.C. pin
    nc(u2_pins["6"])   # PWM output – not used

    # U3 SGP41 N.C. pin
    nc(u3_pins["6"])   # VOUT – not used

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------
    print(f"\nSchematic written to:\n  {SCH_PATH}")
    print("\nComponent summary:")
    syms = ops.get_symbols(SCH_PATH)
    for s in syms:
        print(f"  {s['reference']:5s}  {s['lib_id']}")


if __name__ == "__main__":
    main()
