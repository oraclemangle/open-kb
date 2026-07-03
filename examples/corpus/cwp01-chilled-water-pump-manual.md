# Chilled Water Pump CWP-01 — Operation & Maintenance Manual

**Tag:** CWP-01
**System:** 01_MECHANICAL — HVAC / Chilled Water
**Facility:** Meridian Industrial Park, Building 4 plant room

## 1. Overview

CWP-01 is the primary chilled water circulation pump serving the chilled
water loop that feeds AHU-3 and other air handling units in Building 4.
CWP-01 sits on the same distribution board section as fire pump FP-101,
and is subject to an automatic load-shedding interlock: on a confirmed
fire alarm, CWP-01's supply contactor is opened by the building management
system before FP-101 is permitted to start, so that FP-101 always has the
full starting current available on that board section. See the FP-101
fire pump manual for the FP-101 side of this interlock.

Rated duty: 450 USGPM at 80 ft head, in-line centrifugal, variable speed
drive (VSD) controlled.

## 2. Part Numbers

| Component | Part Number | Notes |
|---|---|---|
| VSD unit | VSD-CWP01-15 | 15 kW frame |
| Mechanical seal | MSL-CWP01-25 | Replace on any visible weep |
| Impeller | IMP-CWP01-6 | 6-inch, bronze |
| Differential pressure sensor | DPS-0-60 | Loop DP control signal to VSD |
| Bearing set | BRG-CWP01-SET | Pair, DE + NDE |
| Suction strainer basket | STR-CWP01-B | Clean at each PM interval |

## 3. Interlocks and Cross-System Notes

- **Fire-alarm shed interlock**: CWP-01's contactor opens automatically on
  confirmed fire alarm to guarantee FP-101 starting capacity. This is a
  hard interlock managed at the building management system, not at the
  VSD itself — a VSD fault will not, by itself, trigger the shed logic.
- **AHU-3 supply**: CWP-01 is the sole chilled water source for AHU-3;
  loss of CWP-01 shows up first as loss of cooling capacity at AHU-3. See
  the AHU-3 manual for the air-side symptoms of a chilled-water-side
  failure.

## 4. Maintenance Schedule

| Interval | Task | Est. duration |
|---|---|---|
| Monthly | Suction strainer basket (STR-CWP01-B) inspection/clean | 30 min |
| Quarterly | Mechanical seal inspection | 30 min |
| Quarterly | VSD fan filter clean, parameter backup check | 30 min |
| Annually | Bearing set inspection/vibration check | 2 h |
| Annually | Full performance curve verification against rated duty point | 2 h |

## 5. Troubleshooting

**Symptom: CWP-01 will not run; contactor appears open with no VSD fault
present.**
1. Check whether the fire-alarm shed interlock is latched — a stale or
   test fire-alarm signal at the building management system can hold the
   contactor open even after the alarm condition clears. This is the most
   common cause of "no VSD fault, but pump not running."
2. Confirm with the fire panel that no active or test alarm is present.

**Symptom: Chilled water flow low; AHU-3 cooling capacity reduced.**
1. Check suction strainer basket (STR-CWP01-B) for blockage — this is the
   most common cause of reduced flow.
2. Check differential pressure sensor (DPS-0-60) reading against the VSD
   setpoint; a miscalibrated DPS-0-60 can cause the VSD to under-speed the
   pump even with a clear strainer.

**Symptom: VSD (VSD-CWP01-15) trips on overcurrent.**
1. Inspect impeller (IMP-CWP01-6) for mechanical binding or debris.
2. Check bearing set (BRG-CWP01-SET) for seizure — a seized bearing is
   the next most likely cause once impeller binding is ruled out.

## 6. Safety Notes

- Confirm the fire-alarm shed interlock is functionally tested annually,
  ideally alongside the FP-101 annual performance test, since the two
  systems' safe operation depends on the same interlock logic behaving
  correctly under both a real and a test fire signal.
