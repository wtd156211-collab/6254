#!/usr/bin/env python3
"""超时预算与取消传播引擎。

读入一份调用树脚本，在注入的虚拟时钟上推演
「预算分配 → 排队执行 → 超时 → 取消传播 → 部分结果」，
逐行轨迹写标准输出，诊断写标准错误。

用法:
    python3 engine.py <脚本文件>

口径见 README.md：一次性分配、同刻阶段顺序「完成 → 外部取消 → 超时 → 启动」、
取消只沿触发者的子树向下传播、已结算节点不再改变。
"""

from __future__ import annotations

import heapq
import json
import sys
from bisect import insort

FRESH = "FRESH"
PENDING = "PENDING"
RUNNING = "RUNNING"
DONE = "DONE"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"

UNSETTLED = (FRESH, PENDING, RUNNING)


class VirtualClock:
    """确定性虚拟时钟：只有脚本给出的事件时刻才让时钟前进。"""

    def __init__(self) -> None:
        self._now = 0

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> int:
        if ms < 0:
            raise ValueError("virtual clock cannot move backwards")
        self._now += ms
        return self._now


class EventQueue:
    """注入的调度器：待办事件队列，键为 (时刻, 先序序号)，同刻按先序出队。"""

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, "Node"]] = []

    def push(self, at_ms: int, order: int, node: "Node") -> None:
        heapq.heappush(self._heap, (at_ms, order, node))

    def pop(self) -> tuple[int, int, "Node"]:
        return heapq.heappop(self._heap)

    def next_ms(self) -> int | None:
        heap = self._heap
        while heap and heap[0][2].state != RUNNING:
            heapq.heappop(heap)
        return heap[0][0] if heap else None


class Node:
    __slots__ = (
        "id", "weight", "mem", "work", "depth", "order", "subtree_end",
        "parent", "children", "state", "outcome", "grant", "deadline",
        "completion", "unsettled",
    )

    def __init__(self, raw: dict, parent: "Node | None", work: int) -> None:
        self.id = raw["id"]
        self.weight = raw.get("weight", 1)
        self.mem = raw.get("mem", 1)
        self.work = work
        self.parent = parent
        self.children: list[Node] = []
        self.depth = parent.depth + 1 if parent is not None else 0
        self.order = -1
        self.subtree_end = -1
        self.state = FRESH
        self.outcome: str | None = None
        self.grant = 0
        self.deadline: int | None = None
        self.completion: int | None = None
        self.unsettled = 0


def _by_order(node: Node) -> int:
    return node.order


class Engine:
    """在虚拟时钟上推演一份调用树脚本，产出逐行轨迹。"""

    def __init__(self, spec: dict, clock: VirtualClock | None = None) -> None:
        config = spec["config"]
        self.deadline_ms = config["deadline_ms"]
        self.floor_ms = config["floor_ms"]
        self.cap_ms = config["cap_ms"]
        self.max_concurrency = config["max_concurrency"]
        self.memory_limit = config["memory_limit"]
        self.events = spec["script"]["events"]
        self.clock = clock if clock is not None else VirtualClock()
        self.nodes: list[Node] = []
        self.by_id: dict[str, Node] = {}
        self.root = self._build(spec["tree"], spec["script"]["work"])
        self.lines: list[str] = []
        self.pending: list[Node] = []
        self.pending_mems: list[tuple[int, int, Node]] = []
        self.completions = EventQueue()
        self.timeouts = EventQueue()
        self.running_count = 0
        self.running_mem = 0
        self._validate_events()

    def _node(self, raw: dict, parent: Node | None, work: dict) -> Node:
        node_id = raw["id"]
        if node_id in self.by_id:
            raise ValueError(f"duplicate id: {node_id!r}")
        if node_id not in work:
            raise ValueError(f"missing work for node: {node_id!r}")
        node = Node(raw, parent, work[node_id])
        if node.weight <= 0:
            raise ValueError(f"non-positive weight on node: {node_id!r}")
        if node.mem <= 0:
            raise ValueError(f"non-positive mem on node: {node_id!r}")
        node.order = len(self.nodes)
        self.nodes.append(node)
        self.by_id[node_id] = node
        return node

    def _build(self, raw_root: dict, work: dict) -> Node:
        root = None
        stack = [(raw_root, None)]
        while stack:
            raw, parent = stack.pop()
            node = self._node(raw, parent, work)
            if parent is None:
                root = node
            else:
                parent.children.append(node)
            for child_raw in reversed(raw.get("children", [])):
                stack.append((child_raw, node))
        open_nodes: list[Node] = []
        for node in self.nodes:
            while open_nodes and open_nodes[-1].depth >= node.depth:
                open_nodes.pop().subtree_end = node.order
            open_nodes.append(node)
        while open_nodes:
            open_nodes.pop().subtree_end = len(self.nodes)
        return root

    def _validate_events(self) -> None:
        previous = 0
        for event in self.events:
            if event.get("kind") != "cancel":
                raise ValueError(f"unknown event kind: {event.get('kind')!r}")
            at_ms = event["at_ms"]
            if at_ms < previous:
                raise ValueError("events out of order")
            previous = at_ms
            if event["target"] not in self.by_id:
                raise ValueError(f"unknown event target: {event['target']!r}")

    def run(self) -> list[str]:
        root = self.root
        root.grant = self.deadline_ms
        root.state = PENDING
        self._make_pending(root)
        event_idx = 0
        events = self.events
        while True:
            now = self.clock.now_ms()
            while self.completions.next_ms() == now:
                _, _, node = self.completions.pop()
                if node.state == RUNNING:
                    self._settle_end(node, now)
            if root.state not in UNSETTLED:
                break
            while event_idx < len(events) and events[event_idx]["at_ms"] == now:
                event = events[event_idx]
                event_idx += 1
                target = self.by_id[event["target"]]
                if target.state in UNSETTLED:
                    self._cancel_range(
                        target.order, target.subtree_end,
                        event["by"], "external", now,
                    )
            if root.state not in UNSETTLED:
                break
            while self.timeouts.next_ms() == now:
                _, _, node = self.timeouts.pop()
                if node.state == RUNNING:
                    self._settle_timeout(node, now)
            if root.state not in UNSETTLED:
                break
            self._sweep(now)
            if root.state not in UNSETTLED:
                break
            candidates = [self.completions.next_ms(), self.timeouts.next_ms()]
            if event_idx < len(events):
                candidates.append(events[event_idx]["at_ms"])
            upcoming = [at for at in candidates if at is not None]
            if not upcoming:
                break
            self.clock.advance(min(upcoming) - now)
        return self.lines

    def _make_pending(self, node: Node) -> None:
        node.state = PENDING
        insort(self.pending, node, key=_by_order)
        heapq.heappush(self.pending_mems, (node.mem, node.order, node))

    def _min_pending_mem(self) -> int | None:
        heap = self.pending_mems
        while heap and heap[0][2].state != PENDING:
            heapq.heappop(heap)
        return heap[0][0] if heap else None

    def _sweep(self, now: int) -> None:
        pending = self.pending
        index = 0
        while index < len(pending):
            node = pending[index]
            if node.state != PENDING:
                pending.pop(index)
                continue
            if self.running_count >= self.max_concurrency:
                return
            min_mem = self._min_pending_mem()
            if min_mem is None or self.running_mem + min_mem > self.memory_limit:
                return
            if self.running_mem + node.mem > self.memory_limit:
                index += 1
                continue
            pending.pop(index)
            self._start(node, now)

    def _start(self, node: Node, now: int) -> None:
        node.state = RUNNING
        node.deadline = now + node.grant
        self.running_count += 1
        self.running_mem += node.mem
        self.lines.append(f"{now} START {node.id}")
        budget = node.grant
        children = node.children
        total_weight = sum(child.weight for child in children)
        paid = 0
        unsettled = 0
        for child in children:
            expected = budget * child.weight // total_weight
            expected = max(self.floor_ms, min(expected, self.cap_ms))
            grant = min(expected, budget - paid)
            paid += grant
            if child.state != FRESH:
                continue
            child.grant = grant
            if grant == 0:
                child.state = CANCELLED
                child.outcome = "cancelled"
                self.lines.append(f"{now} CANCEL {child.id} by={node.id} reason=budget")
            else:
                self._make_pending(child)
                unsettled += 1
        node.unsettled = unsettled
        self.timeouts.push(node.deadline, node.order, node)
        if unsettled == 0:
            node.completion = now + node.work
            self.completions.push(node.completion, node.order, node)

    def _release(self, node: Node) -> None:
        self.running_count -= 1
        self.running_mem -= node.mem

    def _after_settled(self, node: Node, now: int) -> None:
        parent = node.parent
        if parent is None:
            return
        parent.unsettled -= 1
        if parent.unsettled == 0 and parent.state == RUNNING:
            parent.completion = now + parent.work
            self.completions.push(parent.completion, parent.order, parent)

    def _settle_end(self, node: Node, now: int) -> None:
        outcome = "done"
        for child in node.children:
            if child.outcome != "done":
                outcome = "partial"
                break
        node.state = DONE
        node.outcome = outcome
        self._release(node)
        line = f"{now} END {node.id}"
        if outcome == "partial":
            line += " partial"
        self.lines.append(line)
        self._after_settled(node, now)

    def _settle_timeout(self, node: Node, now: int) -> None:
        node.state = TIMEOUT
        node.outcome = "timeout"
        self._release(node)
        self.lines.append(f"{now} TIMEOUT {node.id}")
        self._cancel_range(node.order + 1, node.subtree_end, node.id, "budget", now)
        self._after_settled(node, now)

    def _cancel_range(self, lo: int, hi: int, by: str, reason: str, now: int) -> None:
        for index in range(lo, hi):
            node = self.nodes[index]
            state = node.state
            if state not in UNSETTLED:
                continue
            counted = state != FRESH
            if state == RUNNING:
                self._release(node)
            node.state = CANCELLED
            node.outcome = "cancelled"
            self.lines.append(f"{now} CANCEL {node.id} by={by} reason={reason}")
            if counted:
                self._after_settled(node, now)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 engine.py <脚本文件>", file=sys.stderr)
        return 2
    with open(argv[1], "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    try:
        engine = Engine(spec)
        lines = engine.run()
    except (KeyError, TypeError, ValueError) as exc:
        print(f"engine: 非法脚本: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    name = spec.get("name", argv[1])
    print(
        f"engine: {name}: {len(lines)} 行轨迹, 虚拟时长 {engine.clock.now_ms()} ms",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
