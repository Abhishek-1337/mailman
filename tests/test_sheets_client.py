"""Focused tests for company-only dedup and status coloring.

Run with: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import JobStatus
from src.sheets_client import (
    COLUMNS,
    STATUS_COLORS,
    SheetsClient,
    _covers_status_column,
    status_format_requests,
)


def _client_with_rows(rows):
    client = SheetsClient.__new__(SheetsClient)
    client.sheet_id = "test-sheet"
    client.tab_name = "Applications"
    client._range_base = "'Applications'"
    client.list_rows = lambda: rows  # type: ignore[method-assign]
    return client


HEADER = [list(COLUMNS)]


class FindRowCompanyOnlyTest(unittest.TestCase):
    def test_same_company_different_role_matches(self):
        rows = HEADER + [
            ["2026-10-01 10:00", "Acme", "Backend Engineer", "Greenhouse",
             "Applied", "2026-10-01 10:00", "", "Thanks for applying"],
        ]
        client = _client_with_rows(rows)
        # A later email about a *different* role at Acme must hit the same row.
        self.assertEqual(client.find_row("Acme", "Frontend Engineer"), 2)

    def test_match_is_case_and_whitespace_insensitive(self):
        rows = HEADER + [
            ["2026-10-01 10:00", "Acme Corp", "X", "Y",
             "Applied", "2026-10-01 10:00", "", "Hi"],
        ]
        client = _client_with_rows(rows)
        self.assertEqual(client.find_row("  acme   corp ", "anything"), 2)

    def test_unknown_company_never_matches(self):
        rows = HEADER + [
            ["2026-10-01 10:00", "Unknown", "Unknown", "Unknown",
             "Unknown", "2026-10-01 10:00", "", "Hi"],
        ]
        client = _client_with_rows(rows)
        self.assertIsNone(client.find_row("Unknown", "Unknown"))
        self.assertIsNone(client.find_row("", "Something"))

    def test_no_match_returns_none(self):
        rows = HEADER + [
            ["2026-10-01 10:00", "Acme", "X", "Y",
             "Applied", "2026-10-01 10:00", "", "Hi"],
        ]
        client = _client_with_rows(rows)
        self.assertIsNone(client.find_row("Globex", "Y"))


class StatusFormatRequestsTest(unittest.TestCase):
    def test_one_rule_per_status_covering_column_e(self):
        requests = status_format_requests(12345)
        self.assertEqual(len(requests), len(STATUS_COLORS))
        seen = set()
        for req in requests:
            rule = req["addConditionalFormatRule"]["rule"]
            rng = rule["ranges"][0]
            self.assertEqual(rng["sheetId"], 12345)
            self.assertEqual(rng["startRowIndex"], 1)  # header excluded
            self.assertEqual((rng["startColumnIndex"], rng["endColumnIndex"]), (4, 5))
            value = rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            seen.add(value)
        self.assertEqual(seen, {s.value for s in STATUS_COLORS})

    def test_offer_is_green_rejected_is_red(self):
        by_status = {}
        for req in status_format_requests(1, [JobStatus.OFFER, JobStatus.REJECTED]):
            rule = req["addConditionalFormatRule"]["rule"]
            value = rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            by_status[value] = rule["booleanRule"]["format"]["backgroundColor"]
        offer = by_status["Offer"]
        self.assertGreater(offer["green"], offer["red"])  # green dominates
        rejected = by_status["Rejected"]
        self.assertGreater(rejected["red"], rejected["green"])  # red dominates


class CoversStatusColumnTest(unittest.TestCase):
    def test_detects_column_e_range(self):
        self.assertTrue(_covers_status_column(
            [{"sheetId": 7, "startRowIndex": 1,
              "startColumnIndex": 4, "endColumnIndex": 5}], 7))
        self.assertFalse(_covers_status_column(
            [{"sheetId": 7, "startRowIndex": 1,
              "startColumnIndex": 0, "endColumnIndex": 4}], 7))
        self.assertFalse(_covers_status_column(
            [{"sheetId": 8, "startRowIndex": 1,
              "startColumnIndex": 4, "endColumnIndex": 5}], 7))


class FakeSpreadsheets:
    def __init__(self, meta):
        self.meta = meta
        self.batch_bodies = []

    def get(self, **kwargs):
        test = self

        class _Get:
            def execute(self):
                return test.meta

        return _Get()

    def batchUpdate(self, **kwargs):
        test = self

        class _Batch:
            def execute(self):
                test.batch_bodies.append(kwargs["body"])
                return {}

        return _Batch()


class FakeService:
    def __init__(self, meta):
        self._sheets = FakeSpreadsheets(meta)

    def spreadsheets(self):
        return self._sheets


def _meta_with_rules(sheet_id, values):
    return {"sheets": [{
        "properties": {"sheetId": sheet_id, "title": "Applications"},
        "conditionalFormats": [{
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": 4, "endColumnIndex": 5}],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ",
                                  "values": [{"userEnteredValue": v}]},
                    "format": {},
                },
            }
        } for v in values],
    }]}


class EnsureStatusFormattingTest(unittest.TestCase):
    def _client(self, meta):
        client = SheetsClient.__new__(SheetsClient)
        client.sheet_id = "test-sheet"
        client.tab_name = "Applications"
        client._service = FakeService(meta)
        return client

    def test_adds_only_missing_rules(self):
        client = self._client(_meta_with_rules(9, ["Offer"]))
        client.ensure_status_formatting()
        bodies = client._service._sheets.batch_bodies
        self.assertEqual(len(bodies), 1)
        added = {
            r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]
            ["values"][0]["userEnteredValue"]
            for r in bodies[0]["requests"]
        }
        self.assertNotIn("Offer", added)
        self.assertEqual(added | {"Offer"}, {s.value for s in STATUS_COLORS})

    def test_noop_when_all_rules_present(self):
        client = self._client(
            _meta_with_rules(9, [s.value for s in STATUS_COLORS]))
        client.ensure_status_formatting()
        self.assertEqual(client._service._sheets.batch_bodies, [])


if __name__ == "__main__":
    unittest.main()
