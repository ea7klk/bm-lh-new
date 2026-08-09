"""Destination and threshold rules shared by ingestion and maintenance."""

import os

NON_QSO_DESTINATION_IDS = frozenset({4000, 9990})


def raw_event_threshold_seconds() -> float:
    return float(os.getenv("RAW_EVENT_KERCHUNK_THRESHOLD_SECONDS", os.getenv("KERCHUNK_THRESHOLD_SECONDS", "3")))
