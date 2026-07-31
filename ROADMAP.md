# Roadmap

Future reporting, analytics, collector-health, and privacy improvements. Check an item off when
the corresponding function has been implemented and verified.

## Activity and trends

- [ ] QSOs by hour and day of week.
- [ ] Peak activity periods.
- [ ] Traffic trend compared with the previous period.
- [ ] Busiest talkgroups by growth rate, not just total QSOs.
- [ ] Concurrent active talkgroups or sources over time.

## Callsign insights

- [ ] Average, median, and longest QSO duration per callsign.
- [ ] Callsigns with the widest talkgroup diversity.
- [ ] Most frequent callsign-to-talkgroup combinations.
- [ ] New or first-seen callsigns during the selected period.
- [ ] Callsigns active across multiple countries or continents.

## Talkgroup insights

- [ ] Average, median, and maximum QSO duration.
- [ ] Number of unique sources per talkgroup.
- [ ] Talkgroup source-to-QSO ratio.
- [ ] Most active country or continent.
- [ ] Talkgroup activity heatmaps by hour and weekday.

## Radio and transmission quality

The following quality metrics can use the available `rssi`, `ber`, `slot`, `master`, and related
fields:

- [ ] RSSI distribution and average RSSI.
- [ ] BER distribution and percentage of QSOs above a quality threshold.
- [ ] Quality comparison by talkgroup, slot, or source.
- [ ] Slot utilization.
- [ ] Traffic by master or network link type.

## Network and linking information

The following analyses can use `context_id`, `link_call`, `link_name`, and `link_type_name`:

- [ ] Most frequently linked destinations.
- [ ] Link activity over time.
- [ ] Link topology or connection summaries.
- [ ] QSOs grouped by repeater, master, or link type.
- [ ] Destinations receiving traffic from multiple network links.

## Data quality and collector health

These are especially useful in the admin panel:

- [x] Raw events versus stored QSOs.
- [x] Number of kerchunks filtered by the configured threshold.
- [x] Duplicate raw events.
- [x] Session-stop events without valid start/stop times.
- [x] Negative or unusually long durations.
- [x] Ingestion delay: `received_at - stop_at`.
- [x] Percentage of raw events that become displayable QSOs.
- [x] Last event time and current collector lag.

## Privacy-sensitive options

Callsigns and source metadata can identify operators. Consider displaying aggregated values by
default and applying the following safeguards:

- [ ] Show counts and percentages rather than detailed operator histories.
- [ ] Avoid exposing raw payloads to normal users.
- [ ] Restrict source-quality and detailed callsign reports to authenticated users or
      administrators.
- [ ] Consider configurable retention for detailed QSO data.
