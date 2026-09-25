import json
import sys
import time
import tracemalloc
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine


def load_sample(name):
    path = ROOT / "samples" / "calls" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def run_text(spec):
    return "\n".join(engine.run(spec)) + "\n"


class SampleTracesTest(unittest.TestCase):
    def test_all_samples_match_expected_byte_for_byte(self):
        calls = sorted((ROOT / "samples" / "calls").glob("*.json"))
        self.assertEqual(len(calls), 6)
        for call_path in calls:
            with self.subTest(sample=call_path.stem):
                spec = json.loads(call_path.read_text(encoding="utf-8"))
                expected = (ROOT / "samples" / "expected" / f"{call_path.stem}.trace").read_text(encoding="utf-8")
                self.assertEqual(run_text(spec), expected)

    def test_determinism_across_runs(self):
        for call_path in sorted((ROOT / "samples" / "calls").glob("*.json")):
            with self.subTest(sample=call_path.stem):
                spec = json.loads(call_path.read_text(encoding="utf-8"))
                self.assertEqual(engine.run(spec), engine.run(spec))


def make_spec(children, work, events=None, **cfg):
    config = {
        "deadline_ms": 1000,
        "floor_ms": 50,
        "cap_ms": 400,
        "max_concurrency": 8,
        "memory_limit": 64,
    }
    config.update(cfg)
    full_work = {"root": 10}
    full_work.update(work)
    return {
        "name": "unit",
        "config": config,
        "tree": {"id": "root", "mem": 1, "children": children},
        "script": {"work": full_work, "events": events or []},
    }


class BudgetAllocationTest(unittest.TestCase):
    def test_floor_and_cap_clamp(self):
        # B=1000, W=100：w=1 → 10 抬到 floor 50；w=99 → 990 压到 cap 400。
        spec = make_spec(
            [
                {"id": "low", "weight": 1, "mem": 1},
                {"id": "high", "weight": 99, "mem": 1},
            ],
            {"low": 100000, "high": 100000},
        )
        lines = engine.run(spec)
        self.assertIn("50 TIMEOUT low", lines)
        self.assertIn("400 TIMEOUT high", lines)

    def test_remainder_stays_with_parent(self):
        # cap 把两支都压到 300，剩 400 留在父节点手里不回收不重分配。
        spec = make_spec(
            [
                {"id": "a", "weight": 1, "mem": 1},
                {"id": "b", "weight": 1, "mem": 1},
            ],
            {"a": 100000, "b": 100000},
            cap_ms=300,
        )
        lines = engine.run(spec)
        self.assertIn("300 TIMEOUT a", lines)
        self.assertIn("300 TIMEOUT b", lines)

    def test_zero_grant_cancelled_at_parent_start(self):
        # 先声明的支把余额吃光，后续兄弟当刻取消、永不启动。
        spec = make_spec(
            [
                {"id": "fat", "weight": 20, "mem": 1},
                {"id": "mid", "weight": 2, "mem": 1},
                {"id": "thin", "weight": 1, "mem": 1},
            ],
            {"fat": 5, "mid": 5, "thin": 5},
            deadline_ms=230,
            floor_ms=60,
            cap_ms=230,
        )
        lines = engine.run(spec)
        self.assertEqual(lines[0], "0 START root")
        self.assertEqual(lines[1], "0 CANCEL thin by=root reason=budget")
        self.assertNotIn("0 START thin", lines)

    def test_child_deadline_anchors_to_own_start(self):
        # 子额度在父启动时冻结，截止 = 自己的启动时刻 + 额度；
        # 子排不到资源晚启动，截止相应后移，但仍被父的截止硬约束。
        spec = make_spec(
            [
                {"id": "blocker", "weight": 1, "mem": 59},
                {
                    "id": "parent",
                    "weight": 1,
                    "mem": 1,
                    "children": [{"id": "kid", "weight": 1, "mem": 59}],
                },
            ],
            {"blocker": 100, "parent": 5, "kid": 100000},
            memory_limit=61,
            cap_ms=400,
        )
        lines = engine.run(spec)
        # kid 额度 400，等 blocker 100ms 结束让出内存才启动 → 名义截止 500，
        # 但 parent 截止 400 先到，整棵子树被砍掉。
        self.assertIn("0 START parent", lines)
        self.assertIn("100 START kid", lines)
        self.assertIn("400 TIMEOUT parent", lines)
        self.assertIn("400 CANCEL kid by=parent reason=budget", lines)


class CancelSemanticsTest(unittest.TestCase):
    def test_completion_beats_timeout_at_same_ms(self):
        spec = make_spec(
            [{"id": "a", "weight": 1, "mem": 1}],
            {"a": 100},
            cap_ms=100,
        )
        lines = engine.run(spec)
        self.assertIn("100 END a", lines)
        self.assertNotIn("100 TIMEOUT a", lines)

    def test_external_cancel_of_settled_node_is_noop(self):
        spec = make_spec(
            [{"id": "a", "weight": 1, "mem": 1}],
            {"a": 100},
            events=[{"at_ms": 100, "kind": "cancel", "by": "caller", "target": "a"}],
            cap_ms=100,
        )
        lines = engine.run(spec)
        self.assertIn("100 END a", lines)
        self.assertFalse(any(line.startswith("100 CANCEL a") for line in lines))

    def test_external_cancel_covers_whole_subtree_preorder(self):
        spec = make_spec(
            [
                {
                    "id": "a",
                    "weight": 1,
                    "mem": 1,
                    "children": [
                        {"id": "a1", "weight": 1, "mem": 1},
                        {"id": "a2", "weight": 1, "mem": 1},
                    ],
                },
                {"id": "b", "weight": 1, "mem": 1},
            ],
            {"a": 100000, "a1": 100000, "a2": 100000, "b": 100000},
            events=[{"at_ms": 10, "kind": "cancel", "by": "ops", "target": "a"}],
        )
        lines = engine.run(spec)
        self.assertEqual(
            [line for line in lines if " CANCEL " in line],
            [
                "10 CANCEL a by=ops reason=external",
                "10 CANCEL a1 by=ops reason=external",
                "10 CANCEL a2 by=ops reason=external",
            ],
        )
        self.assertIn("400 TIMEOUT b", lines)  # 兄弟不受影响

    def test_parent_deadline_hard_bounds_subtree(self):
        # 子的截止可以晚于父，但父超时把整棵子树砍掉：子不会比父更晚结束。
        spec = make_spec(
            [
                {
                    "id": "p",
                    "weight": 1,
                    "mem": 1,
                    "children": [{"id": "c", "weight": 1, "mem": 1}],
                }
            ],
            {"p": 100000, "c": 100000},
            cap_ms=400,
            floor_ms=50,
            deadline_ms=500,
        )
        lines = engine.run(spec)
        # p 额度 400（cap），c 额度 400；c 截止 400 与 p 相同，但同刻祖先先记。
        self.assertIn("400 TIMEOUT p", lines)
        self.assertIn("400 CANCEL c by=p reason=budget", lines)
        end_of = {}
        for line in lines:
            parts = line.split()
            end_of[parts[2] if parts[1] != "CANCEL" else parts[2]] = int(parts[0])
        self.assertLessEqual(end_of["c"], end_of["p"])

    def test_concurrency_slot_held_by_root(self):
        # max_concurrency=1：根占着唯一槽位，子永远排不上，根到点超时砍掉全部。
        spec = make_spec(
            [
                {"id": "a", "weight": 1, "mem": 1},
                {"id": "b", "weight": 1, "mem": 1},
            ],
            {"a": 5, "b": 5},
            max_concurrency=1,
            deadline_ms=200,
        )
        lines = engine.run(spec)
        self.assertEqual(
            lines,
            [
                "0 START root",
                "200 TIMEOUT root",
                "200 CANCEL a by=root reason=budget",
                "200 CANCEL b by=root reason=budget",
            ],
        )


def build_wide_spec(branches=100, per_branch=40):
    # 1 + 100 + 4000 = 4101 个节点，模拟几千条并发调用。
    children = []
    work = {"root": 5}
    for b in range(branches):
        pid = f"p{b}"
        work[pid] = 3
        grand = []
        for k in range(per_branch):
            cid = f"p{b}c{k}"
            work[cid] = (b * 7 + k * 13) % 90 + 1
            grand.append({"id": cid, "weight": (k % 5) + 1, "mem": 1})
        children.append({"id": pid, "weight": (b % 3) + 1, "mem": 1, "children": grand})
    return {
        "name": "perf",
        "config": {
            "deadline_ms": 100000,
            "floor_ms": 10,
            "cap_ms": 5000,
            "max_concurrency": 500,
            "memory_limit": 500,
        },
        "tree": {"id": "root", "mem": 1, "children": children},
        "script": {"work": work, "events": []},
    }


class PerfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = build_wide_spec()

    def test_runtime_under_two_seconds(self):
        start = time.perf_counter()
        lines = engine.run(self.spec)
        elapsed = time.perf_counter() - start
        self.assertGreater(len(lines), 4000)
        self.assertLess(elapsed, 2.0)

    def test_peak_memory_within_64_mib(self):
        tracemalloc.start()
        try:
            engine.run(self.spec)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 64 * 1024 * 1024)

    def test_large_run_deterministic(self):
        self.assertEqual(engine.run(self.spec), engine.run(self.spec))


if __name__ == "__main__":
    unittest.main()
