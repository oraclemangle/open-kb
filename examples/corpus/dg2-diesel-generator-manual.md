# Diesel Generator DG2 — Operation & Maintenance Manual

**Tag:** DG2
**System:** 00_ELECTRICAL — Power Generation
**Facility:** Meridian Industrial Park, Building 4 plant room

## 1. Overview

DG2 is the sister unit to DG1, identical in rating and installed in the
same plant room. DG2 takes standby duty on even-numbered weeks under the
automatic changeover logic hosted in the MSB-1 switchboard controller.
Like DG1, DG2 alone can carry the full facility load.

Rated output: 500 kVA / 400 kW at 0.8 power factor, 400V 3-phase 50Hz.
Prime mover: 6-cylinder turbocharged diesel, water-cooled — same engine
family as DG1, so part numbers are shared between the two units unless
noted otherwise below.

## 2. Part Numbers

| Component | Part Number | Notes |
|---|---|---|
| Fuel injector set | FIS-4410-06 | Shared with DG1 |
| Primary fuel filter | FF-2201 | Shared with DG1 |
| Secondary fuel filter | FF-2202 | Shared with DG1 |
| Lube oil filter | LOF-1180 | Shared with DG1 |
| Coolant thermostat | THX-330 | Shared with DG1 |
| Control PCB | CPB-DG-9 | Shared with DG1 |
| Alternator bearing (DE) | BRG-6216-DE | Shared with DG1 |
| DG2-specific: exhaust bellows | EXB-DG2-01 | DG2 has a longer exhaust run than DG1 |

## 3. Electrical Interconnection

DG2's output breaker (DG2-ACB) feeds MSB-1 in parallel with DG1's breaker.
Both units are interlocked so that only one can be in the process of
synchronising onto the bus at a time. See the MSB-1 switchboard manual for
the full downstream distribution, including the feeders to AHU-3 and the
FP-101 fire pump changeover panel.

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

**Symptom: DG2 fails to start on auto-start signal from MSB-1.**
1. Check control PCB (CPB-DG-9) fault LED and RESET as required.
2. Check starter battery voltage — minimum 22V under crank load.
3. Confirm the DG2 local E-stop loop is not latched.

**Symptom: DG2 output breaker will not close onto MSB-1.**
1. Check DG2's voltage/frequency are within the synchronising window
   (±5% voltage, ±0.3 Hz).
2. Confirm DG1 is not mid-synchronisation on the same bus section.
3. Check the DG2-ACB close coil fuse in MSB-1.

**Symptom: Excessive exhaust noise or visible bellows damage.**
1. Inspect the DG2-specific exhaust bellows (EXB-DG2-01) for cracking —
   DG2's longer exhaust run makes this a DG2-only wear item not shared
   with DG1.

## 6. Safety Notes

- Isolate and lock off DG2-ACB before any work on the alternator or
  control panel.
- DG2 shares a common day tank with DG1; draining the day tank affects
  both units.
- Confirm plant room ventilation fans are running before manual start.
