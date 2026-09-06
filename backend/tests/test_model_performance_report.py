from __future__ import annotations

import json

import pytest

from scripts.report_model_performance import read_entries, summarize


def test_report_counts_usage_and_timings_without_exporting_content():
    entries = [
        {
            "provider": "local",
            "model": "resident",
            "operation": "/v1/chat/completions",
            "request": {"messages": [{"content": "private question"}]},
            "response": {"usage": {"prompt_tokens": 100, "completion_tokens": 20}},
            "timing": {"duration_seconds": 2.0, "first_byte_seconds": 0.5, "queue_wait_seconds": 0.1},
        },
        {
            "provider": "local", "model": "resident", "operation": "/v1/chat/completions",
            "error": {"message": "private failure"},
            "timing": {"duration_seconds": 3.0},
        },
        {"provider": "local", "model": "resident", "operation": "/v1/embeddings"},
    ]
    report = summarize(entries)
    assert len(report) == 1
    assert report[0]["calls"] == 2
    assert report[0]["errors"] == 1
    assert report[0]["timing"]["duration_seconds"] == {"samples": 2, "p50": 2.0, "p95": 3.0, "total": 5.0}
    assert report[0]["prompt_tokens"]["total"] == 100
    assert "private" not in json.dumps(report)
    assert "request" not in json.dumps(report)


def test_legacy_logs_do_not_report_missing_timings_as_zero():
    report = summarize([{"operation": "/v1/chat/completions", "model": "legacy"}])
    assert report[0]["timing"]["duration_seconds"]["samples"] == 0
    assert report[0]["timing"]["duration_seconds"]["total"] is None
    assert report[0]["prompt_tokens"]["total"] is None


def test_malformed_log_reports_line_number_without_echoing_sensitive_content(tmp_path):
    path = tmp_path / "calls.jsonl"
    path.write_text('{"operation": "/v1/chat/completions"}\nsecret not json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2") as error:
        list(read_entries(path))
    assert "secret" not in str(error.value)
