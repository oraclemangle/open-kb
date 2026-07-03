# Air Handling Unit AHU-3 — Operation & Maintenance Manual

**Tag:** AHU-3
**System:** 01_MECHANICAL — HVAC
**Facility:** Meridian Industrial Park, Building 4 plant room

## 1. Overview

AHU-3 serves the general office and workshop spaces of Building 4 with
conditioned air. Cooling is provided by a chilled water coil fed from
CWP-01, the facility's primary chilled water circulation pump. Electrical
supply for AHU-3's supply/return fans and controls is drawn from MSB-1 via
feeder breaker F-AHU3-100.

Design airflow: 12,000 CFM supply, variable air volume (VAV) control
across six zones.

## 2. Part Numbers

| Component | Part Number | Notes |
|---|---|---|
| Supply fan (plug fan, VSD) | FAN-AHU3-SUP-11 | 11 kW |
| Return fan (plug fan, VSD) | FAN-AHU3-RET-5 | 5.5 kW |
| Chilled water coil | CWC-AHU3-8ROW | 8-row, fed from CWP-01 loop |
| Supply air filter (pre-filter) | FLT-AHU3-G4 | Change quarterly |
| Supply air filter (bag filter) | FLT-AHU3-F7 | Change 6-monthly |
| Coil freeze thermostat | FRT-AHU3-02 | Trips supply fan below 3°C |
| DDC controller | DDC-AHU3-200 | BMS integration point |
| VAV box actuator (per zone) | ACT-VAV-24 | 6 off, one per zone |

## 3. Cross-System Notes

- **Chilled water supply**: AHU-3's cooling coil (CWC-AHU3-8ROW) is fed
  entirely from CWP-01. Any CWP-01 outage — including the automatic
  fire-alarm shed interlock described in the CWP-01 manual — will show up
  at AHU-3 as declining supply air temperature control within minutes,
  well before any CWP-01-side alarm may be visible locally.
- **Electrical supply**: AHU-3 draws from MSB-1 feeder F-AHU3-100. A
  facility-wide mains failure and changeover to DG1/DG2 standby power
  (see the MSB-1 manual) will cause a brief AHU-3 shutdown and restart
  during the changeover window; this is expected behaviour, not a fault.

## 4. Maintenance Schedule

| Interval | Task | Est. duration |
|---|---|---|
| Quarterly | Pre-filter (FLT-AHU3-G4) change | 30 min |
| 6-monthly | Bag filter (FLT-AHU3-F7) change | 45 min |
| 6-monthly | Fan bearing grease, belt tension check (if applicable) | 1 h |
| Annually | Coil clean (both sides), freeze thermostat function test | 2 h |
| Annually | VAV actuator (ACT-VAV-24) stroke and calibration check, all 6 zones | 2 h |

## 5. Troubleshooting

**Symptom: Supply air temperature rising, cooling seems ineffective.**
1. Check CWP-01 is running and chilled water differential temperature
   across the coil is within normal range — see the CWP-01 manual's
   troubleshooting section if flow is confirmed low.
2. Check chilled water coil (CWC-AHU3-8ROW) for airside fouling if
   CWP-01-side flow is confirmed normal.

**Symptom: Supply fan trips on freeze thermostat (FRT-AHU3-02).**
1. Confirm chilled water flow is present and coil is not iced — a fully
   or partially closed chilled water valve with the supply fan still
   running is the most common cause.
2. Check DDC controller (DDC-AHU3-200) sequencing logic for a valve
   command fault.

**Symptom: One VAV zone not maintaining setpoint.**
1. Check that zone's actuator (ACT-VAV-24) for mechanical binding or a
   lost BMS command.
2. Confirm the zone's local sensor is reading plausibly before assuming
   an actuator fault.

## 6. Safety Notes

- Isolate F-AHU3-100 at MSB-1 before any fan or electrical work.
- Confirm coil freeze thermostat (FRT-AHU3-02) is never bypassed for more
  than the duration of active supervised testing.
