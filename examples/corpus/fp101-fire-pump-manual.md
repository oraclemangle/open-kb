# Fire Pump FP-101 — Operation & Maintenance Manual

**Tag:** FP-101
**System:** 04_SAFETY — Fire Protection
**Facility:** Meridian Industrial Park, Building 4 plant room

## 1. Overview

FP-101 is the facility's electric-driven fire pump, providing pressurised
water to the sprinkler and hydrant network. It is fed from MSB-1 via
feeder breaker F-FP101-150 (see the MSB-1 switchboard manual), through a
dedicated soft-starter panel. FP-101 is interlocked with the chilled water
pump CWP-01: on a confirmed fire signal, CWP-01 is automatically shed from
the electrical supply to guarantee FP-101 has full starting capacity
available, since both pumps sit on the same distribution board section
under abnormal-condition load-shedding logic.

Rated duty: 1000 USGPM at 120 psi, centrifugal, end-suction.

## 2. Part Numbers

| Component | Part Number | Notes |
|---|---|---|
| Pump mechanical seal | MSL-FP101-40 | Replace on any visible weep |
| Impeller | IMP-FP101-9 | 9-inch, bronze |
| Soft starter module | SSM-750-3PH | 750A frame |
| Pressure sensor (suction) | PS-0-30 | 0-30 psi range |
| Pressure sensor (discharge) | PS-0-200 | 0-200 psi range |
| Jockey pump controller | JPC-101 | Maintains system pressure between fire pump runs |
| Bearing set (pump end) | BRG-FP101-SET | Pair, DE + NDE |

## 3. Interlocks and Cross-System Notes

- **CWP-01 shed interlock**: on confirmed fire alarm, the building
  management system opens the CWP-01 supply contactor before permitting
  FP-101 to start, guaranteeing available starting current. See the
  CWP-01 chilled water pump manual for the CWP-01 side of this interlock.
- **MSB-1 supply**: FP-101's soft-starter panel draws from feeder breaker
  F-FP101-150 on MSB-1. A nuisance trip on that feeder during pump start
  is a soft-starter configuration issue in the great majority of cases —
  see the MSB-1 manual's troubleshooting section.

## 4. Maintenance Schedule

| Interval | Task | Est. duration |
|---|---|---|
| Weekly | No-flow churn test, 10 minutes run | 20 min |
| Monthly | Flow test at rated capacity | 1 h |
| Quarterly | Mechanical seal inspection | 45 min |
| Annually | Full performance curve test against rated duty point | 3 h |
| Annually | Soft starter (SSM-750-3PH) contactor and thyristor inspection | 2 h |

## 5. Troubleshooting

**Symptom: FP-101 fails to start on fire alarm signal.**
1. Confirm CWP-01 has actually been shed from supply — if the shed
   interlock relay is faulty, FP-101's soft starter may be blocked from
   closing due to an undervoltage lockout on the shared board section.
2. Check jockey pump controller (JPC-101) is not indicating a system
   already at pressure in a way that suppresses the fire pump start
   (should not normally suppress a genuine fire signal — treat as a
   controller fault if it does).
3. Check F-FP101-150 at MSB-1 has not tripped.

**Symptom: Pump runs but discharge pressure is low.**
1. Check suction pressure sensor (PS-0-30) reading — a low suction
   condition (blocked strainer, closed suction valve) will produce low
   discharge pressure regardless of pump condition.
2. Inspect impeller (IMP-FP101-9) for wear or damage if suction is
   confirmed normal.

**Symptom: Mechanical seal weeping.**
1. Replace mechanical seal (MSL-FP101-40) — do not run the pump
   extensively with a weeping seal, as bearing (BRG-FP101-SET)
   contamination follows quickly.

## 6. Safety Notes

- FP-101 must never be electrically isolated without a documented
  fire-watch in place, per facility fire safety procedure.
- Confirm the CWP-01 shed interlock is tested as part of any FP-101
  isolation for maintenance, since a stuck-closed CWP-01 contactor could
  starve FP-101 of starting current when both would otherwise be needed.
