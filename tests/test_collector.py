import logging

import bminfo.collector as collector


def test_collector_logs_only_completed_qso_blocks(monkeypatch, caplog):
    monkeypatch.setattr(collector, "STORED_QSO_LOG_BLOCK", 100)

    with caplog.at_level(logging.INFO, logger=collector.logger.name):
        for count in (1, 99, 100, 101, 200):
            collector._log_stored_qso_progress(count)

    assert [record.message for record in caplog.records] == [
        "stored 100 QSOs (progress block of 100)",
        "stored 200 QSOs (progress block of 100)",
    ]
