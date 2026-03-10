# 后台活动状态契约（第一稿）

`/system/operations` 与 `ct-ops status` 现在只回答"逾期与否"，把两件不同的事说成同一件：**从未到期**
和**该跑没跑**。`ledger_confirm` 是第一个踩到的，以后每加一项后台活动都会再踩一次。

本稿只改监控状态的表述，不改任何活动的行为、时序或权限。

## 0. 今天的实际行为（已核实，带位置）

`service/system.py:171` 一行决定一切：

```python
"overdue": age is None or age > window * 2,
```

注释写的是"从未跑过也算逾期：一个备份作业从未加载的安装，看起来和健康的一模一样，直到需要它的
那天"。这对 `backup` 是对的,那是个装好就该跑的东西。对**刚加进来的**活动就不对：它从未跑过，而且
还没到第一次该跑的时候。

`ledger_confirm` 当前状态（2026-09-19 00:45 本地核实）：

| 事实 | 值 |
| --- | --- |
| 每日确认的代码进入线上版本 | 2026-09-18 13:27 本地（`f4e25bf`） |
| 维护作业触发时刻 | 每天 03:45 本地 |
| 第一次有机会运行 | 2026-09-19 03:45 本地 |
| 成功心跳 | 无 |
| 确认报告文档 | 无（非 dry-run 调用一定会写，连拒绝也写,所以是从未执行） |
| 因此 `overdue` | `true`,**误报** |

**一处更正**：我此前说它会以 `ledger_unconfirmed`、critical 出现在待办里，这是错的。逾期走的是
`notifications.py:424` 的 `backup_overdue`，RANKING 里是 normal／4。critical／1 的
`ledger_unconfirmed` 只在**确认报告存在且某个账本拒绝**时才出现（`notifications.py:405` 一带），
报告不存在，所以现在没有 critical 误报。要修的是"该跑没跑"这句话本身不成立，以及下面第 3 节那条
重复告知。

## 1. 四个状态（闭集）

后端为每个活动给出 `state`，前端按它显示，不再自己从 `overdue` 与 `last_success_at` 推断。

| state | 含义 | 判定 |
| --- | --- | --- |
| `current` | 在窗口内跑过 | 有成功，且 `age_minutes <= expected_every_minutes × 2` |
| `awaiting_first_run` | 从未跑过，且还没到期 | 无成功，且 `now − expected_since <= expected_every_minutes` |
| `overdue` | 该跑没跑 | 无成功且 `now − expected_since > expected_every_minutes`；或有成功但 `age_minutes > expected_every_minutes × 2` |
| `failed` | 跑了，但被拒绝 | 该活动留有报告，且报告说最近一次未通过（今天只有 `ledger_confirm` 留报告） |

`expected_since` 是"这项活动在本部署上从哪一刻起该跑了"。它在活动 id 第一次出现且没有心跳时写入一次，
之后不再改动，所以一次发布不会把一个已经逾期一周的活动重新说成"等待首次运行"。写入点是发布时
（`release.sh publish` 已经在调 `operations heartbeat`）与 API 启动，两处都是幂等的：只在该键不存在
时写。

`overdue` 布尔字段**保留不动**，等于 `state in ("overdue", "failed")`,旧读者不会因为本次改动而漏报。

## 2. 新增字段

`/system/operations.activities[]` 每项在现有字段之外增加：

| 字段 | 说明 |
| --- | --- |
| `state` | 上表四值之一 |
| `expected_since` | 这项活动从哪一刻起该跑了；老部署上可能为 `null`，此时 `awaiting_first_run` 不适用，行为与今天一致 |
| `due_by` | 下一次该跑到的时刻。`awaiting_first_run` 时是首次期限，`current` 时是下一次期限；`overdue`／`failed` 时为已过去的那个时刻 |
| `last_attempt_at` | 最近一次**运行**的时刻，不论成败。只有留报告的活动有，其余为 `null` |
| `last_failure` | `null`，或 `{"at": ..., "reason": ...}`。`reason` 是报告里的原话，不重写 |

现有字段 `id`、`last_success_at`、`age_minutes`、`expected_every_minutes`、`overdue`、`detail` 全部
保持原义。

## 3. 待办映射（含一条抑制规则）

| state | 是否进 `/attention` | kind | 严重性／优先级 |
| --- | --- | --- | --- |
| `current` | 否 | | |
| `awaiting_first_run` | **否** | | 没有人需要做什么 |
| `overdue` | 是 | `backup_overdue`（不变） | normal／4；`backup` 仍标 urgent |
| `failed` | 是 | `ledger_unconfirmed`（每个账本一条，不变） | critical／1 |

**抑制规则（今天缺的那条）**：`failed` 时不再另发 `backup_overdue`。账本连续拒绝三天，今天会同时出现
一条 critical 的"账本无法确认"和一条 normal 的"确认作业没按时跑"，说的是同一件事。拒绝本身就是运行
过的证据，所以 `failed` 吃掉 `overdue`。

## 4. 四种样例

**`current`**
```json
{"id": "backup", "state": "current", "last_success_at": "2026-09-19T07:15:43Z",
 "age_minutes": 3.7, "expected_every_minutes": 1440, "expected_since": "2026-09-04T22:21:00Z",
 "due_by": "2026-09-20T07:15:43Z", "last_attempt_at": null, "last_failure": null,
 "overdue": false, "detail": "trading_state_20260919T071543Z.sqlite3, integrity ok"}
```

**`awaiting_first_run`**（`ledger_confirm` 今天的真实状态）
```json
{"id": "ledger_confirm", "state": "awaiting_first_run", "last_success_at": null,
 "age_minutes": null, "expected_every_minutes": 1440, "expected_since": "2026-09-18T20:27:56Z",
 "due_by": "2026-09-19T20:27:56Z", "last_attempt_at": null, "last_failure": null,
 "overdue": false, "detail": ""}
```
屏幕应显示"首次运行待执行，期限 <due_by>"，**不要**显示"从未运行"或"逾期"。

**`overdue`**
```json
{"id": "ledger_confirm", "state": "overdue", "last_success_at": null, "age_minutes": null,
 "expected_every_minutes": 1440, "expected_since": "2026-09-18T20:27:56Z",
 "due_by": "2026-09-19T20:27:56Z", "last_attempt_at": null, "last_failure": null,
 "overdue": true, "detail": ""}
```

**`failed`**
```json
{"id": "ledger_confirm", "state": "failed", "last_success_at": "2026-09-11T18:02:00Z",
 "age_minutes": 11520.0, "expected_every_minutes": 1440,
 "expected_since": "2026-09-18T20:27:56Z", "due_by": "2026-09-13T18:02:00Z",
 "last_attempt_at": "2026-09-19T10:45:12Z",
 "last_failure": {"at": "2026-09-19T10:45:12Z",
   "reason": "CashMismatch: the venue holds 101.50 and the ledger 100.00 in micro_live; record the 1.50 difference as a deposit before confirming"},
 "overdue": true, "detail": "micro_live as_of 2026-09-11"}
```
此时 `/attention` 只有 critical 的 `ledger_unconfirmed`，没有 `backup_overdue`。

## 5. 前端要改的三处

1. System 页每行按 `state` 显示，不再从 `last_success_at == null` 推"从未运行"。四个状态各一句文案，
   两种语言。
2. `awaiting_first_run` 不进任何"需要处理"的计数或徽标。
3. `failed` 时把 `last_failure.reason` 原样显示,那是后端给的原话（可能是券商现金差额这类可操作信息），
   不要改写、不要截断到看不出金额。

## 6. 本稿不做

- 不改任何活动的时序、窗口或触发方式。`expected_every_minutes` 的数值不动。
- 不改 `backup` 的 urgent 判定。
- 不新增待办 kind,`failed` 复用已有的 `ledger_unconfirmed`，`overdue` 复用已有的 `backup_overdue`。
- 不动自主权限的任何部分。

## 7. 实施顺序（字段定稿后）

1. `operations.declare(db)` 写 `expected_since`，幂等；发布与 API 启动各调一次。
2. `service/system.py` 产出 `state` 与四个新字段，`overdue` 保持原义。
3. `service/notifications.py` 的 `operations()` 只在 `state == "overdue"` 时发 `backup_overdue`。
4. 测试：四个状态各一例，抑制规则一例，老部署 `expected_since` 缺失时行为不变一例。
5. `deploy/checks.sh` 通过后提交，提交号与字段说明发前端。
