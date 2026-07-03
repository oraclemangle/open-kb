# Diesel Generator DG1 — Operation & Maintenance Manual

**Tag:** DG1
**System:** 00_ELECTRICAL — Power Generation
**Facility:** Meridian Industrial Park, Building 4 plant room

## 1. Overview

DG1 is one of two standby diesel generators (DG1, DG2) providing backup
power to the facility's main switchboard, MSB-1. DG1 is the duty unit on
odd-numbered weeks; DG2 takes duty on even-numbered weeks under the
automatic changeover logic in the switchboard controller. Both units are
rated for full facility load individually, so either can carry the entire
plant on its own if the other is down for maintenance.

Rated output: 500 kVA / 400 kW at 0.8 power factor, 400V 3-phase 50Hz.
Prime mover: 6-cylinder turbocharged diesel, water-cooled.

## 2. Part Numbers

| Component | Part Number | Notes |
|---|---|---|
| Fuel injector set | FIS-4410-06 | 6 off per engine |
| Primary fuel filter | FF-2201 | Change every 500 h |
| Secondary fuel filter | FF-2202 | Change every 500 h |
| Lube oil filter | LOF-1180 | Change every 250 h |
| Coolant thermostat | THX-330 | Opens at 82°C |
| Alternator bearing (DE) | BRG-6216-DE | Drive end |
| Alternator bearing (NDE) | BRG-6216-NDE | Non-drive end |
| Starter motor | SM-24V-450 | 24V DC |
| Control PCB | CPB-DG-9 | Governor + AVR interface |

## 3. Electrical Interconnection

DG1's output breaker feeds MSB-1 via a 400A ACB (air circuit breaker)
labelled DG1-ACB. See the MSB-1 switchboard manual for the full
distribution scheme downstream of MSB-1, including feeders to AHU-3 and
the fire pump FP-101 changeover panel. DG1 and DG2 are electrically
interlocked at MSB-1 so both cannot close onto the bus simultaneously
except during a brief make-before-break synchronising window managed by
the switchboard controller.

## 4. Maintenance Schedule

| Interval | Task | Est. duration |
|---|---|---|
| Daily (when running) | Check coolant level, oil level, visual leak check | 10 min |
| 250 running hours | Lube oil + filter change | 1.5 h |
| 500 running hours | Fuel filter change (both stages), fuel/water separator drain | 2 h |
| 1000 running hours | Valve clearance check/adjust, injector inspection | 4 h |
| 4000 running hours | Alternator bearing inspection, insulation resistance test | 3 h |
| Annually | Full load bank test at 100% rated kVA for 2 h | 3 h |

## 5. Troubleshooting

**Symptom: DG1 fails to start on auto-start signal from MSB-1.**
1. Check control PCB (CPB-DG-9) fault LED — a solid red LED indicates a
   stored fault; press RESET before retrying.
2. Check starter battery voltage (nominal 24V DC, minimum 22V under crank
   load). A voltage below 22V under load is the most common cause of a
   failed auto-start.
3. Confirm the emergency stop loop is not latched — a tripped E-stop
   anywhere in the DG1 local panel or remote stations blocks auto-start
   until manually reset at the local panel.

**Symptom: DG1 starts but will not close its output breaker onto MSB-1.**
1. Check that DG1's voltage and frequency are within the synchronising
   window (typically ±5% voltage, ±0.3 Hz) — the switchboard controller
   will not permit closure outside this window.
2. Confirm DG2 is not already closed onto the same bus section in a
   configuration that the interlock logic treats as a conflict.
3. Check the DG1-ACB close coil fuse in MSB-1.

**Symptom: DG1 runs but alternator output voltage is low or unstable.**
1. Inspect AVR (automatic voltage regulator) wiring at CPB-DG-9.
2. Check alternator bearing condition (BRG-6216-DE / BRG-6216-NDE) —
   bearing wear can introduce air-gap eccentricity that shows up first as
   voltage instability under load.

## 6. Safety Notes

- Isolate and lock off DG1-ACB before any work on the alternator or
  control panel.
- Diesel exhaust in the plant room requires the ventilation fans to be
  confirmed running before DG1 is started manually.
- DG1 shares a common day tank with DG2; draining the day tank affects
  both units.
