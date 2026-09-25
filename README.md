# 超时预算与取消传播（从 0 实现）

起始环境里没有代码，能参考的只有这份说明和 `samples/` 下的调用脚本；引擎与页面都从零新写。

## 1. 范围

交付物（仓库根目录）：

- `engine.py`：纯标准库的 Python 3.13 程序，读入一份调用树脚本，在虚拟时钟上推演「预算分配 → 排队执行 →
  超时 → 取消传播 → 部分结果」，把逐行轨迹写到标准输出；
- `viewer.html`：单文件原生 HTML/Canvas 页面，双击即开，选入脚本与轨迹后按时间轴画出每个节点的开始、
  结束、超时、取消。

```
python3 engine.py <脚本文件>     # 跑一个脚本，轨迹写标准输出，诊断走 stderr
```

不做：第三方库、CDN、构建与打包、联网、真实时钟与 `sleep`、线程/进程/协程、持久化与重试、非法脚本的
容错解析（样例保证合法）；页面不做像素级验收。

## 2. 口径与公式

- 时间单位是虚拟毫秒，一律非负整数；只有脚本给出的时刻才让时钟前进，引擎不读真实时间。
- **一次性分配**：节点启动的那一毫秒，把自己从启动时刻算起的剩余预算按子节点权重分完，之后冻结。设父节
  点预算 `B`，子节点按声明顺序为 `1…n`、权重为 `w`、`W = Σw`：
  - 期望额度 `e = clamp(B*w // W, floor_ms, cap_ms)`，`clamp` 取上下限之间；
  - 按声明顺序支付：`g = min(e, B - 已付总额)`；
  - `g = 0` 的子节点当刻就被取消，永远不会启动；没付出去的余额留在父节点手里，既不回收也不重分配。
- **截止时刻** = 启动时刻 + 授予额度；根的额度就是 `deadline_ms`，从 0 启动。
- **收尾工作量** `work`：节点在全部子节点结算之后还要消耗的毫秒数，正整数；叶节点的耗时就是它。节点自身
  不占前置时间。
- **时间与调度注入**：引擎不读墙钟、不 `sleep`；时间来自注入的虚拟时钟（`now_ms()` / `advance(ms)`），
  下一事件时刻由注入的调度器（待办事件队列）给出。默认实现就是确定性虚拟时钟，验收只看默认实现。

## 3. 状态机、调用树与调度

状态：`PENDING`（已分到预算、还没拿到资源）→ `RUNNING` → `DONE`；`RUNNING` → `TIMEOUT`；
`PENDING` / `RUNNING` → `CANCELLED`；结算后不再改变。

结局：`done` / `partial` / `timeout` / `cancelled`。节点正常收尾时，直接子节点里有非 `done` 的记
`partial`，反之记 `done`；被取消或超时的节点不产出结果，它已完成子节点的结果也随之中止。

调用树：每个节点 `{"id": 字符串, "weight": 正整数, "mem": 正整数（默认 1）, "children": [子节点]}`；
`id` 全局唯一，「先序」都指这份声明顺序上的先序遍历。

**取消传播**：触发者只有三类——① 自己的截止时刻到达（记 `TIMEOUT`）；② 某个祖先的截止时刻到达（祖先记
`TIMEOUT`，子树里未结算的节点记 `CANCEL`，`by=` 祖先）；③ 脚本里的外部取消事件（`by=` 事件给的发起
者）。范围是被取消节点的**整棵子树**，含还没启动的 `PENDING` 节点，但不越界去动它的兄弟与祖先；已结算
的节点不再出行，取消对它无效。

**同刻顺序**：同一毫秒的阶段顺序固定为「完成 → 外部取消 → 超时 → 启动」，同阶段内按先序。于是同刻完成
赢过取消与超时，外部取消赢过超时，先序在前的祖先赢过同刻到期的后代。

**调度上限**：并发数 `max_concurrency` 是同时 `RUNNING` 的节点数上限（根也占一个槽位），内存上限
`memory_limit` 是同时 `RUNNING` 的 `mem` 之和上限，两者都满足才启动。每毫秒按先序把 `PENDING` 节点扫
一遍，能启动的就启动，扫不动了结束这一毫秒；同刻释放出的资源当刻可用。节点启动时先记 `START`，紧接着
把额度为 0 的子节点记 `CANCEL`，再扫下一个。

## 4. 输入输出与文件格式

本节文件都是 UTF-8、`\n` 换行、末行也有换行。

### 4.1 调用树脚本 `samples/calls/<名>.json`

```json
{
  "name": "<文件名>",
  "config": {"deadline_ms": 300, "floor_ms": 60, "cap_ms": 300,
             "max_concurrency": 4, "memory_limit": 6},
  "tree": {"id": "root", "mem": 1, "children": [{"id": "a", "weight": 8, "mem": 2}]},
  "script": {"work": {"root": 30, "a": 500},
             "events": [{"at_ms": 200, "kind": "cancel", "by": "caller", "target": "a"}]}
}
```

`name` 等于文件名去 `.json`；`script.work` 要给全每个节点的收尾工作量；`script.events` 的 `at_ms` 非
降序，同刻按数组里的先后处理，`kind` 目前只有 `cancel`。

### 4.2 轨迹 `samples/expected/<名>.trace`

一行一条事件，共四种：

```
<毫秒> START <id>
<毫秒> END <id>[ partial]
<毫秒> TIMEOUT <id>
<毫秒> CANCEL <id> by=<发起者> reason=<budget|external>
```

`<毫秒>` 是十进制整数、不补零；时间非降序；同一毫秒内保持引擎产出的先后，不再排序。从未启动的节点没有
`START` 行，但会有 `CANCEL` 行；根节点结算即全程结束，轨迹到那里为止。

### 4.3 页面 `viewer.html`

原生 HTML/Canvas，不 `fetch`、不引 CDN、不起服务，双击即开且跑两遍逐字节相同。选入脚本与轨迹后：横轴
是轨迹里的毫秒；一个节点一行、行标签是 `id`；行上标出 `START` / `END` / `TIMEOUT`，`CANCEL` 标出发起
者与 `reason`。

## 5. 性能与验收口径

规模：单棵 ≤ 10^4 个节点、脚本 ≤ 10^4 条事件；单次运行 ≤ 2 秒，额外峰值内存 ≤ 64 MiB（`tracemalloc`
扣基线）。

1. 环境：Python 3.13、只用标准库、无构建无网络；运行期除 `var/`（缓存、临时文件）外不写别的路径，
   `var/` 不入库。
2. 正确性：`samples/calls/` 下每个脚本跑一次 `engine.py`，输出与 `samples/expected/<同名>.trace`
   逐字节相同（含行序与结尾换行）。
3. 确定性：同一脚本跑两遍 sha256 相同；不得依赖真实时间、随机数、哈希随机化、字典/集合迭代顺序，也不得
   靠线程调度。
4. 边界：六个脚本各盯一条口径、逐行可对（见第 6 节）。
5. 页面：4.3 的四项齐全，数字与轨迹一致。

## 6. 样例说明

`calls/<名>.json` 与 `expected/<名>.trace` 一一对应：

| 脚本 | 场景 | 关键结果 |
| --- | --- | --- |
| `01-single-branch-timeout` | 单支超时：两支同跑，一支吃满额度 | `300 TIMEOUT long`；`120 END short`；根 `360 END root partial` |
| `02-sibling-unaffected` | 兄弟支不受影响：失败支的额度不回收不重分配 | `200 TIMEOUT A` + `200 CANCEL A1 by=A reason=budget`；`B` 300 照常结束 |
| `03-parent-deadline-first` | 父截止先到：内存被占着，孙节点 120 才跑起来 | `G` 截止 1120，父 `B` 1000 先到：`1000 TIMEOUT B` + `1000 CANCEL G by=B reason=budget` |
| `04-budget-eaten-by-one-branch` | 预算被某一支吃光：先声明的支把余额吃到 0 | `c` 从未 `START`，当刻 `0 CANCEL c by=root reason=budget` |
| `05-cancel-vs-complete` | 取消与完成赛跑：同刻两个外部取消 | 200 上完成赢过取消；`q` 同刻到期记 `CANCEL ... by=supervisor` 而非 `TIMEOUT` |
| `06-three-simultaneous-timeouts` | 三个下游同刻到期 | 先序定归属：`X`、`Y`、`Z` 各记 `TIMEOUT`，同刻到期的 `Z1` 反被 `CANCEL by=Z` |

`samples/notes.md` 是这组样例的现场记录，不参与判定。

## 7. 待补的文档

隐藏验收用例不随仓库提供；非法脚本（重复 `id`、缺 `work`、权重非正、事件乱序）的逐条错误码与提示文案、
`viewer.html` 的配色与排版、更大规模下的性能基线，都由实现在本说明的口径内自定。
