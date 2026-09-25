#!/usr/bin/env python3
"""engine.py 的 unittest 测试：样例轨迹、确定性、口径边界与规模冒烟。"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402

CALLS = ROOT / "samples" / "calls"
EXPECTED = ROOT / "samples" / "expected"


def run_spec(spec):
    return engine.Engine(spec).run()


def run_sample(path):
    spec = json.loads(path.read_text(encoding="utf-8"))
    return "".join(f"{line}\n" for line in run_spec(spec))


def make_spec(tree, work, config=None, events=None):
    return {
        "name": "test",
        "config": {
            "deadline_ms": 1000,
            "floor_ms": 10,
            "cap_ms": 10**9,
            "max_concurrency": 1000,
            "memory_limit": 10**9,
            **(config or {}),
        },
        "tree": tree,
        "script": {"work": work, "events": events or []},
    }


class SampleTracesTest(unittest.TestCase):
    def test_samples_match_expected_byte_for_byte(self):
        samples = sorted(CALLS.glob("*.json"))
        self.assertEqual(len(samples), 6)
        for call in samples:
            with self.subTest(sample=call.name):
                expected = (EXPECTED / f"{call.stem}.trace").read_text(encoding="utf-8")
                self.assertEqual(run_sample(call), expected)

    def test_cli_stdout_matches_expected(self):
        for call in sorted(CALLS.glob("*.json")):
            with self.subTest(sample=call.name):
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "engine.py"), str(call)],
                    capture_output=True, text=True, check=True,
                )
                expected = (EXPECTED / f"{call.stem}.trace").read_text(encoding="utf-8")
                self.assertEqual(proc.stdout, expected)

    def test_determinism_across_hash_seeds(self):
        call = CALLS / "06-three-simultaneous-timeouts.json"
        outputs = []
        for seed in ("0", "1", "42"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, str(ROOT / "engine.py"), str(call)],
                capture_output=True, text=True, check=True, env=env,
            )
            outputs.append(proc.stdout)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])


class AllocationTest(unittest.TestCase):
    def test_floor_and_cap_clamp(self):
        spec = make_spec(
            {"id": "r", "children": [
                {"id": "big", "weight": 9},
                {"id": "small", "weight": 1},
            ]},
            {"r": 10, "big": 500, "small": 500},
            config={"deadline_ms": 100, "floor_ms": 30, "cap_ms": 50},
        )
        lines = run_spec(spec)
        self.assertIn("0 START big", lines)
        self.assertIn("0 START small", lines)
        self.assertIn("50 TIMEOUT big", lines)
        self.assertIn("30 TIMEOUT small", lines)
        self.assertIn("60 END r partial", lines)

    def test_unspent_balance_stays_with_parent(self):
        spec = make_spec(
            {"id": "r", "children": [
                {"id": "a", "weight": 1},
                {"id": "b", "weight": 1},
            ]},
            {"r": 10, "a": 500, "b": 500},
            config={"deadline_ms": 100, "cap_ms": 40},
        )
        lines = run_spec(spec)
        self.assertIn("40 TIMEOUT a", lines)
        self.assertIn("40 TIMEOUT b", lines)

    def test_external_cancel_of_pending_node(self):
        spec = make_spec(
            {"id": "r", "mem": 1, "children": [
                {"id": "a", "weight": 1, "mem": 2},
                {"id": "b", "weight": 1, "mem": 2},
            ]},
            {"r": 10, "a": 100, "b": 100},
            config={"memory_limit": 3, "max_concurrency": 10},
            events=[{"at_ms": 5, "kind": "cancel", "by": "ops", "target": "b"}],
        )
        lines = run_spec(spec)
        self.assertIn("5 CANCEL b by=ops reason=external", lines)
        self.assertNotIn("0 START b", lines)
        self.assertIn("110 END r partial", lines)

    def test_completion_wins_over_timeout_at_same_ms(self):
        spec = make_spec(
            {"id": "r", "children": [{"id": "a", "weight": 1}]},
            {"r": 10, "a": 50},
            config={"deadline_ms": 100, "cap_ms": 50},
        )
        lines = run_spec(spec)
        self.assertIn("50 END a", lines)
        self.assertIn("60 END r", lines)
        self.assertNotIn("50 TIMEOUT a", lines)

    def test_cancelled_subtree_does_not_touch_siblings(self):
        spec = make_spec(
            {"id": "r", "children": [
                {"id": "a", "weight": 1, "children": [{"id": "a1", "weight": 1}]},
                {"id": "b", "weight": 1},
            ]},
            {"r": 10, "a": 500, "a1": 500, "b": 20},
            config={"deadline_ms": 100, "cap_ms": 40},
            events=[{"at_ms": 10, "kind": "cancel", "by": "x", "target": "a"}],
        )
        lines = run_spec(spec)
        self.assertIn("10 CANCEL a by=x reason=external", lines)
        self.assertIn("10 CANCEL a1 by=x reason=external", lines)
        self.assertIn("20 END b", lines)
        self.assertIn("30 END r partial", lines)

    def test_concurrency_slot_released_same_ms(self):
        spec = make_spec(
            {"id": "r", "mem": 1, "children": [
                {"id": "a", "weight": 1, "mem": 1},
                {"id": "b", "weight": 1, "mem": 1},
            ]},
            {"r": 5, "a": 10, "b": 10},
            config={"max_concurrency": 2, "deadline_ms": 500},
        )
        lines = run_spec(spec)
        self.assertEqual(lines[0], "0 START r")
        self.assertEqual(lines[1], "0 START a")
        self.assertEqual(lines[2], "10 END a")
        self.assertEqual(lines[3], "10 START b")
        self.assertEqual(lines[4], "20 END b")
        self.assertEqual(lines[5], "25 END r")


class ScaleTest(unittest.TestCase):
    def test_thousands_of_concurrent_calls(self):
        branches = 60
        leaves = 100
        children = []
        work = {"root": 25}
        for b in range(branches):
            branch_leaves = []
            for l in range(leaves):
                leaf_id = f"b{b:02d}-l{l:03d}"
                work[leaf_id] = 50 + (b * 37 + l * 91) % 400
                branch_leaves.append({
                    "id": leaf_id,
                    "weight": 1 + (l % 3),
                    "mem": 1 + (l % 3),
                })
            branch_id = f"b{b:02d}"
            work[branch_id] = 20 + b
            children.append({
                "id": branch_id,
                "weight": 1 + (b % 5),
                "mem": 1,
                "children": branch_leaves,
            })
        events = [
            {"at_ms": 100 + i, "kind": "cancel", "by": "ops",
             "target": f"b{(i * 7) % branches:02d}-l{(i * 13) % leaves:03d}"}
            for i in range(2000)
        ]
        spec = make_spec(
            {"id": "root", "mem": 1, "children": children},
            work,
            config={"deadline_ms": 300000, "floor_ms": 20, "cap_ms": 4000,
                    "max_concurrency": 64, "memory_limit": 96},
            events=events,
        )
        first = run_spec(spec)
        second = run_spec(spec)
        self.assertEqual(first, second)
        self.assertTrue(first[-1].endswith("END root partial"))
        started = sum(1 for line in first if " START " in line)
        self.assertGreater(started, 5000)


if __name__ == "__main__":
    unittest.main()
