#!/usr/bin/env python3
"""超时预算与取消传播引擎。

读入一份调用树脚本（见 README.md 4.1），在注入的虚拟时钟上推演
「预算分配 → 排队执行 → 超时 → 取消传播 → 部分结果」，
把逐行轨迹写到标准输出，诊断走 stderr。

用法：python3 engine.py <脚本文件>
"""

import bisect
import heapq
import json
import sys

PENDING, RUNNING, SETTLED = 0, 1, 2


class Node:
    __slots__ = (
        "idx", "id", "weight", "mem", "work", "parent", "children",
        "weight_sum", "sub_size", "deadline", "state", "outcome", "remaining",
    )

    def __init__(self, idx, node_id, weight, mem, work, parent):
        self.idx = idx
        self.id = node_id
        self.weight = weight
        self.mem = mem
        self.work = work
        self.parent = parent
        self.children = []
        self.weight_sum = 0
        self.sub_size = 1
        self.deadline = 0
        self.state = PENDING
        self.outcome = None
        self.remaining = 0


def build_nodes(spec):
    """按声明先序把调用树拉平成节点数组，下标即先序序号。"""
    work_map = spec["script"]["work"]
    nodes = []

    def visit(obj, parent_idx):
        idx = len(nodes)
        node = Node(
            idx,
            obj["id"],
            obj.get("weight", 0),
            obj.get("mem", 1),
            work_map[obj["id"]],
            parent_idx,
        )
        nodes.append(node)
        for child in obj.get("children", []):
            node.children.append(visit(child, idx))
        node.remaining = len(node.children)
        node.weight_sum = sum(nodes[c].weight for c in node.children)
        return idx

    sys.setrecursionlimit(max(100000, sys.getrecursionlimit()))
    visit(spec["tree"], -1)
    for node in reversed(nodes):
        for c in node.children:
            node.sub_size += nodes[c].sub_size
    return nodes


def run(spec):
    """推演一份脚本，返回轨迹行列表（不含换行）。"""
    cfg = spec["config"]
    floor_ms = cfg["floor_ms"]
    cap_ms = cfg["cap_ms"]
    max_conc = cfg["max_concurrency"]
    mem_limit = cfg["memory_limit"]

    nodes = build_nodes(spec)
    id_to_idx = {node.id: node.idx for node in nodes}
    grant = [0] * len(nodes)
    grant[0] = cfg["deadline_ms"]
    events = spec["script"].get("events", [])

    lines = []
    emit = lines.append

    running_count = 0
    running_mem = 0
    completions = []  # (完成时刻, idx) 小根堆
    timeouts = []     # (截止时刻, idx) 小根堆
    pending = [0]     # PENDING 节点 idx，按先序有序
    need_scan = True

    def settle(i, t, outcome):
        nonlocal running_count, running_mem, need_scan
        node = nodes[i]
        if node.state == RUNNING:
            running_count -= 1
            running_mem -= node.mem
            need_scan = True  # 同刻释放出的资源当刻可用
        node.state = SETTLED
        node.outcome = outcome
        p = node.parent
        if p >= 0:
            parent = nodes[p]
            parent.remaining -= 1
            if parent.remaining == 0 and parent.state == RUNNING:
                heapq.heappush(completions, (t + parent.work, p))

    def cancel_subtree(root_idx, t, by, reason):
        # 先序遍历中子树是连续区间；已结算的节点不再出行。
        end = root_idx + nodes[root_idx].sub_size
        for i in range(root_idx, end):
            node = nodes[i]
            if node.state != SETTLED:
                emit(f"{t} CANCEL {node.id} by={by} reason={reason}")
                settle(i, t, "cancelled")

    def start_node(i, t):
        nonlocal running_count, running_mem
        node = nodes[i]
        emit(f"{t} START {node.id}")
        node.state = RUNNING
        running_count += 1
        running_mem += node.mem
        node.deadline = t + grant[i]
        heapq.heappush(timeouts, (node.deadline, i))
        # 一次性分配：启动这一毫秒把剩余预算按权重分完，之后冻结。
        budget = grant[i]
        total_w = node.weight_sum
        paid = 0
        for c in node.children:
            child = nodes[c]
            share = budget * child.weight // total_w
            if share < floor_ms:
                share = floor_ms
            elif share > cap_ms:
                share = cap_ms
            g = share if share < budget - paid else budget - paid
            paid += g
            grant[c] = g
            if g == 0:
                emit(f"{t} CANCEL {child.id} by={node.id} reason=budget")
                settle(c, t, "cancelled")
            else:
                bisect.insort(pending, c)
        if not node.children:
            heapq.heappush(completions, (t + node.work, i))

    t = 0
    ev_ptr = 0
    while nodes[0].state != SETTLED:
        # 阶段一：完成（同刻完成赢过取消与超时）
        due = []
        while completions and completions[0][0] == t:
            _, i = heapq.heappop(completions)
            if nodes[i].state == RUNNING:
                due.append(i)
        for i in due:
            node = nodes[i]
            if any(nodes[c].outcome != "done" for c in node.children):
                emit(f"{t} END {node.id} partial")
                settle(i, t, "partial")
            else:
                emit(f"{t} END {node.id}")
                settle(i, t, "done")
        if nodes[0].state == SETTLED:
            break

        # 阶段二：外部取消（脚本事件，同刻按数组先后）
        while ev_ptr < len(events) and events[ev_ptr]["at_ms"] == t:
            ev = events[ev_ptr]
            ev_ptr += 1
            target = id_to_idx[ev["target"]]
            if nodes[target].state != SETTLED:
                cancel_subtree(target, t, ev["by"], "external")
        if nodes[0].state == SETTLED:
            break

        # 阶段三：超时（先序在前的祖先先记，子孙随之取消）
        due = []
        while timeouts and timeouts[0][0] == t:
            _, i = heapq.heappop(timeouts)
            if nodes[i].state == RUNNING:
                due.append(i)
        for i in due:
            node = nodes[i]
            if node.state != RUNNING:
                continue
            emit(f"{t} TIMEOUT {node.id}")
            settle(i, t, "timeout")
            cancel_subtree(i, t, node.id, "budget")
        if nodes[0].state == SETTLED:
            break

        # 阶段四：启动（按先序扫 PENDING，能启动的就启动）
        if need_scan:
            need_scan = False
            cursor = 0
            while cursor < len(pending):
                i = pending[cursor]
                node = nodes[i]
                if node.state != PENDING:
                    pending.pop(cursor)
                elif running_count < max_conc and running_mem + node.mem <= mem_limit:
                    pending.pop(cursor)
                    start_node(i, t)
                else:
                    cursor += 1

        # 推进虚拟时钟到下一事件时刻
        while completions and nodes[completions[0][1]].state != RUNNING:
            heapq.heappop(completions)
        while timeouts and nodes[timeouts[0][1]].state != RUNNING:
            heapq.heappop(timeouts)
        next_t = None
        if completions:
            next_t = completions[0][0]
        if timeouts and (next_t is None or timeouts[0][0] < next_t):
            next_t = timeouts[0][0]
        if ev_ptr < len(events):
            at = events[ev_ptr]["at_ms"]
            if next_t is None or at < next_t:
                next_t = at
        if next_t is None:
            break
        t = next_t

    return lines


def main(argv):
    if len(argv) != 2:
        print("用法: python3 engine.py <脚本文件>", file=sys.stderr)
        return 2
    with open(argv[1], "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    lines = run(spec)
    sys.stdout.write("\n".join(lines) + "\n")
    print(f"# {spec.get('name', '?')}: 轨迹 {len(lines)} 行", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
