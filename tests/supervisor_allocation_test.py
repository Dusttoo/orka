#!/usr/bin/env python3
"""Fair queue allocation coverage for one, two, three, and N lanes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from supervisor_allocation import AllocationError, allocate_lanes  # noqa: E402


def candidates(**overrides):
    value = {
        "repair": [],
        "recovery": [],
        "continuation": [],
        "dependency_unlocking": [],
        "fresh": [],
    }
    value.update(overrides)
    return value


class AllocationTests(unittest.TestCase):
    def test_one_lane_retains_strict_finish_first(self) -> None:
        result = allocate_lanes(
            candidates(
                repair=["T-1"],
                recovery=["T-2"],
                dependency_unlocking=["T-3"],
                fresh=["T-4"],
            ),
            available=1,
            concurrency=1,
        )
        self.assertEqual(
            [(item["ticket"], item["reason"]) for item in result["selections"]],
            [("T-1", "single-lane-finish-first")],
        )

    def test_two_lanes_reserve_dependency_unlocking_capacity(self) -> None:
        result = allocate_lanes(
            candidates(
                repair=["T-1"],
                dependency_unlocking=["T-2", "T-3"],
                fresh=["T-4"],
            ),
            available=2,
            concurrency=2,
        )
        self.assertEqual(
            [(item["ticket"], item["class"]) for item in result["selections"]],
            [("T-2", "dependency_unlocking"), ("T-1", "repair")],
        )

    def test_three_lanes_weight_repair_and_recovery_without_global_pause(self) -> None:
        result = allocate_lanes(
            candidates(
                repair=["R-1", "R-2"],
                recovery=["C-1"],
                fresh=["F-1"],
            ),
            available=3,
            concurrency=3,
        )
        self.assertEqual(
            [(item["ticket"], item["action"]) for item in result["selections"]],
            [("R-1", "repair"), ("C-1", "recovery"), ("R-2", "repair")],
        )

    def test_n_lane_cursor_prevents_starvation_under_continuous_load(self) -> None:
        cursor = 0
        observed = set()
        for cycle in range(6):
            result = allocate_lanes(
                candidates(
                    repair=[f"R-{cycle}"],
                    recovery=[f"C-{cycle}"],
                    continuation=[f"P-{cycle}"],
                    fresh=[f"F-{cycle}"],
                ),
                available=1,
                concurrency=4,
                cursor=cursor,
            )
            observed.add(result["selections"][0]["class"])
            cursor = result["next_cursor"]
        self.assertEqual(observed, {"repair", "recovery", "continuation", "fresh"})

    def test_duplicate_ticket_is_kept_in_its_highest_priority_class(self) -> None:
        result = allocate_lanes(
            candidates(repair=["T-1"], fresh=["T-1", "T-2"]),
            available=3,
            concurrency=3,
        )
        self.assertEqual(
            [item["ticket"] for item in result["selections"]], ["T-1", "T-2"]
        )

    def test_invalid_capacity_fails_closed(self) -> None:
        with self.assertRaises(AllocationError):
            allocate_lanes(candidates(), available=-1, concurrency=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
