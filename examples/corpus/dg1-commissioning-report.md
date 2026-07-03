# Commissioning Report — Aurora Power Systems APG-500 Standby Generator

**System:** 00_ELECTRICAL — Power Generation
**Facility:** Meridian Industrial Park, Building 4 plant room
**Report type:** Factory-witnessed commissioning, on-site acceptance test

## 1. Unit Under Test

Manufacturer: Aurora Power Systems
Model: APG-500
Serial: APG500-2024-0417
Rated output: 500 kVA / 400 kW at 0.8 power factor, 400V 3-phase 50Hz
Prime mover: 6-cylinder turbocharged diesel, water-cooled

This unit is installed as the primary standby generator in the Building 4
plant room, feeding the facility's main switchboard on the odd-week duty
cycle under the switchboard's automatic changeover controller. The unit
was installed alongside an identical Aurora Power Systems APG-500 acting
as the even-week duty standby unit, per the facility's two-generator
standby architecture.

## 2. Test Sequence and Results

| Test | Method | Result |
|---|---|---|
| No-load start | Manual start, cold engine | Pass — start to stable idle in 8s |
| Load acceptance (25% step) | Load bank, single step | Pass — frequency dip 1.8 Hz, recovered in 3s |
| Load acceptance (100% step) | Load bank, single step | Pass — frequency dip 4.1 Hz, recovered in 6s |
| Full load run | 2 h at rated 500 kVA | Pass — no alarms, coolant temp stable at 84°C |
| Auto-start on simulated mains fail | Switchboard controller signal | Pass — closed onto bus in 11s from signal |
| Synchronising window verification | Manual sync attempt outside window | Pass — controller correctly blocked closure |
| Governor/AVR stability | Step load, observe voltage/frequency recovery | Pass — within manufacturer spec |

## 3. Notes for the Facility Register

The unit's local control panel is labelled with the facility's own asset
tag per site convention, distinct from the manufacturer's model plate.
Facility engineering staff should cross-reference this commissioning
report (identifying the unit by manufacturer make/model, Aurora Power
Systems APG-500) against the corresponding equipment manual (identifying
the same physical unit by its facility tag) when reviewing maintenance
history, since both documents describe the same physical asset in the
Building 4 plant room feeding the main switchboard alongside its sister
unit.

## 4. Punch List (Closed Prior to Handover)

1. Local panel door interlock microswitch mis-adjusted — corrected during
   commissioning, retested, closed.
2. Exhaust lagging clamp missing one fastener — fitted, closed.
3. Battery charger float voltage 0.3V outside spec — adjusted to
   manufacturer setpoint, retested, closed.

## 5. Handover Statement

The Aurora Power Systems APG-500 unit described in this report is handed
over to facility operations in a fully commissioned, tested state, ready
for inclusion in the facility's standard preventive maintenance schedule
alongside its sister standby unit in the same plant room.
