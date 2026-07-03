# Main Switchboard MSB-1 — Operation & Maintenance Manual

**Tag:** MSB-1
**System:** 00_ELECTRICAL — Power Distribution
**Facility:** Meridian Industrial Park, Building 4 plant room

## 1. Overview

MSB-1 is the facility's main low-voltage switchboard, rated 1000A at
400V 3-phase 50Hz. It receives incoming supply from two sources: the
utility mains incomer, and standby power from diesel generators DG1 and
DG2 via their respective air circuit breakers (DG1-ACB, DG2-ACB). The
switchboard controller manages automatic mains-failure changeover and
alternates duty between DG1 and DG2 on a weekly basis.

## 2. Part Numbers

| Component | Part Number | Notes |
|---|---|---|
| Main incomer ACB | ACB-MAINS-1000 | Utility supply |
| DG1 output ACB | ACB-DG1-400 | See DG1 manual |
| DG2 output ACB | ACB-DG2-400 | See DG2 manual |
| Bus coupler ACB | ACB-BUSCPL-1000 | Section A/B tie |
| Switchboard controller | SBC-9000 | AMF + sync logic |
| Metering CT set | CT-1000/5 | Per outgoing feeder |
| Protection relay (main) | REL-P900 | Overcurrent/earth fault |

## 3. Downstream Distribution

MSB-1 feeds all major facility loads, including:

- **AHU-3** (air handling unit, HVAC) via feeder breaker F-AHU3-100
- **FP-101** fire pump changeover panel via feeder breaker F-FP101-150,
  which in turn provides power to the FP-101 electric fire pump and its
  controller
- General distribution boards throughout Building 4

## 4. Maintenance Schedule

| Interval | Task | Est. duration |
|---|---|---|
| Monthly | Visual inspection, thermal imaging of bus bars and connections | 1 h |
| 6-monthly | ACB mechanism lubrication and operation test | 2 h |
| Annually | Protection relay (REL-P900) calibration check | 2 h |
| Annually | Insulation resistance test, all outgoing feeders | 3 h |
| Bi-annually | Full changeover sequence test: mains fail -> DG1 start -> DG1 duty -> DG2 duty swap | 4 h |

## 5. Troubleshooting

**Symptom: Automatic changeover to standby generation does not occur on
mains failure.**
1. Check SBC-9000 controller for a fault code — a common cause is a
   stale mains-present signal from a failed voltage sensing relay.
2. Confirm DG1 (or the duty unit for the current week) responds to the
   auto-start signal — see the DG1/DG2 manuals' troubleshooting sections
   for start-failure diagnosis.
3. Check the bus coupler ACB-BUSCPL-1000 is in the expected position for
   the site's changeover configuration (open or closed depending on
   whether Section A/B are normally run tied or split).

**Symptom: Nuisance trip of REL-P900 on a feeder with no apparent fault.**
1. Check CT wiring polarity on the affected feeder — a reversed CT is the
   most common cause of a spurious earth-fault trip.
2. Review the relay event log for the actual measured fault current at
   time of trip.

**Symptom: FP-101 feeder (F-FP101-150) trips on fire pump start.**
1. This is almost always an inrush/starting-current issue rather than a
   genuine fault — confirm the soft-starter (see the FP-101 fire pump
   manual) is configured correctly before assuming a switchboard fault.

## 6. Safety Notes

- MSB-1 remains live from the utility mains even with both DG1 and DG2
  isolated — always confirm the incomer ACB is open and locked off before
  internal work.
- The bus coupler must never be closed while both DG1-ACB and DG2-ACB are
  closed on opposite sections without first confirming synchronising
  conditions via SBC-9000.
