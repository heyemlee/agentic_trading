# 后台活动状态契约（第二稿）

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

## 1. 前端四条意见，逐条落成规则

**一、`due_by` 是监控期限，不是下一次预计运行时刻。** 第一稿把它写成"下一次该跑到的时刻"，与状态翻转的
实际阈值不符：有成功记录的活动要过 `window × 2` 才算逾期，所以一个 1.5 倍窗口没跑的活动会显示一个已过去
的 `due_by` 却仍是 `current`。现在 `due_by` 就定义为**状态翻到 `overdue` 的那一刻**：

| 情形 | `due_by` |
| --- | --- |
| 有成功记录 | `last_success_at + expected_every_minutes × 2` |
| 无成功记录，`expected_since` 已知 | `expected_since + expected_every_minutes` |
| 无成功记录，`expected_since` 未知 | `null`,说不出来就不说 |

两个倍数不同是既有的容差，本次不动它，只是把 `due_by` 与它对齐。屏幕上 `due_by` 可以直接读作"再过多久
会报逾期"。

**二、`expected_since` 写一次，永不重写，且只在没有成功记录时被读。** 两条都要，缺一条就会出现你们说的
重置：

- **只写一次**：仅当该 id 既无成功记录也无既有 `expected_since` 时写入。任何后续发布、重启、迁移都不会
  改它。
- **只在无成功记录时被读**：有成功记录的活动，`due_by` 一律从 `last_success_at` 算。所以给一个上次成功
  在十天前的活动写入 `expected_since` 也不会把它的期限往后推,那个字段此时根本不参与判定。

迁移那一刻（本部署第一次跑这套声明）与之后新增一项活动，来源不同：

| 场合 | `expected_since` 取值 |
| --- | --- |
| 迁移（活动来源文档尚不存在） | 当前运行版本的 `RELEASE.json.published_at`,该活动的代码最早也是那时才到这台机器的 |
| 迁移之后新增的 id | 写入当刻 |
| 开发检出（没有 `RELEASE.json`） | `null`,不给宽限期，行为与今天一致 |

迁移只发生一次，之后文档存在，所以再发布多少次都不会有第二次宽限期。

**三、失败是"最近一次结果"，不是粘滞状态；连失败都停了要能看出来。**

- **恢复**：`failed` 由报告与成功记录的先后决定,报告比上一次成功更新且说未通过就是 `failed`；之后任何
  一次成功比报告更新，立刻回到 `current`。不需要人工清除。
- **停跑**：一直失败说明它还在跑，`failed` 覆盖 `overdue`（第 3 节那条抑制规则的由来）。但如果连尝试都
  停了,`last_attempt_at` 本身已超过监控期限,就不再是 `failed` 而是 `overdue`。一个只失败过一次、之后
  彻底不跑的活动，不会永远停在"失败"上假装自己还活着。

**四、`failed` 与期限无关，`due_by` 可以尚未到达。** 一小时前跑过并被拒绝，下一次期限在明天,这是
`failed` 且 `due_by` 在未来。因此：

- `overdue` 这个**布尔字段保持今天的公式不变**（`age is None or age > window × 2`），不再像第一稿那样
  重定义为 `state in ("overdue", "failed")`。新鲜的失败因此是 `overdue: false` 加 `state: "failed"`。
- 旧读者不会因此漏掉失败：失败的可操作信息本来就由报告产生的 critical `ledger_unconfirmed` 承载，从来
  不经过 `overdue`。

## 2. 状态判定（按此优先级）

```
window        = expected_every_minutes
success_at    = 成功心跳时刻，从未成功则 null
expected_since= 本活动从哪一刻起该跑了，说不出则 null
attempt_at    = 最近一次运行时刻，不论成败；只有留报告的活动有
refusal       = 报告说未通过时的 {at, reason}

due_by = success_at + window×2          若 success_at 非空
       = expected_since + window        否则若 expected_since 非空
       = null                           否则

1. failed              refusal 比 success_at 更新（或从未成功），且 attempt_at + window×2 >= now
2. overdue             due_by 非空且 now > due_by；或 due_by 为空且 success_at 为空
3. awaiting_first_run  success_at 为空，due_by 非空，且 now <= due_by
4. current             其余
```

没有报告的活动永远不会是 `failed`,它们没有"跑了但被拒绝"这种结果可读。今天只有 `ledger_confirm`
留报告。

## 3. 新增字段

`/system/operations.activities[]` 每项在现有字段之外增加：

| 字段 | 说明 |
| --- | --- |
| `state` | 上面四值之一 |
| `expected_since` | 见第 1 节第二条；可能为 `null` |
| `due_by` | 状态翻到 `overdue` 的时刻；`null` 表示说不出来 |
| `last_attempt_at` | 最近一次运行时刻，不论成败；无报告的活动为 `null` |
| `last_failure` | `null`，或 `{"at": ..., "reason": ...}`，`reason` 是报告原话 |

现有字段 `id`、`last_success_at`、`age_minutes`、`expected_every_minutes`、`overdue`、`detail` 全部
保持原义与原公式。

## 4. 待办映射（含一条抑制规则）

| state | 是否进 `/attention` | kind | 严重性／优先级 |
| --- | --- | --- | --- |
| `current` | 否 | | |
| `awaiting_first_run` | **否** | | 没有人需要做什么 |
| `overdue` | 是 | `backup_overdue`（不变） | normal／4；`backup` 仍标 urgent |
| `failed` | 是 | `ledger_unconfirmed`（每个账本一条，不变） | critical／1 |

**抑制规则**：只在 `state == "overdue"` 时发 `backup_overdue`。账本连续拒绝几天，今天会同时出现一条
critical 的"账本无法确认"和一条 normal 的"确认作业没按时跑"，说的是同一件事;拒绝本身就是运行过的证据。
而连尝试都停了的时候，按第 1 节第三条它已经是 `overdue`，`backup_overdue` 照常出现。

## 5. 五种样例

**`current`**
```json
{"id": "backup", "state": "current", "last_success_at": "2026-09-19T07:15:43Z",
 "age_minutes": 3.7, "expected_every_minutes": 1440, "expected_since": null,
 "due_by": "2026-09-21T07:15:43Z", "last_attempt_at": null, "last_failure": null,
 "overdue": false, "detail": "trading_state_20260919T071543Z.sqlite3, integrity ok"}
```
`expected_since` 为 `null` 而状态仍是 `current`：它有成功记录，那个字段此时不参与判定。`due_by` 是
成功时刻加两个窗口。

**`awaiting_first_run`**（`ledger_confirm` 在首次运行之前）
```json
{"id": "ledger_confirm", "state": "awaiting_first_run", "last_success_at": null,
 "age_minutes": null, "expected_every_minutes": 1440, "expected_since": "2026-09-18T20:27:56Z",
 "due_by": "2026-09-19T20:27:56Z", "last_attempt_at": null, "last_failure": null,
 "overdue": true, "detail": ""}
```
注意 `overdue` 是 `true`,那个布尔按旧公式算，从未成功即为真。**前端一律以 `state` 为准**，这一条正是
第一稿要修掉的误报。屏幕显示"首次运行待执行，期限 <due_by>"，不显示"从未运行"或"逾期"。

**`failed`，期限尚未到达**（第 1 节第四条）
```json
{"id": "ledger_confirm", "state": "failed", "last_success_at": "2026-09-18T10:45:02Z",
 "age_minutes": 1440.0, "expected_every_minutes": 1440,
 "expected_since": "2026-09-18T20:27:56Z", "due_by": "2026-09-20T10:45:02Z",
 "last_attempt_at": "2026-09-19T10:45:12Z",
 "last_failure": {"at": "2026-09-19T10:45:12Z",
   "reason": "CashMismatch: the venue holds 101.50 and the ledger 100.00 in micro_live; record the 1.50 difference as a deposit before confirming"},
 "overdue": false, "detail": "micro_live as_of 2026-09-18"}
```
`overdue: false`、`due_by` 在未来、`state: "failed"` 三者同时成立。`/attention` 只有 critical 的
`ledger_unconfirmed`，没有 `backup_overdue`。

**`overdue`，连尝试都停了**（第 1 节第三条）
```json
{"id": "ledger_confirm", "state": "overdue", "last_success_at": "2026-09-11T18:02:00Z",
 "age_minutes": 11520.0, "expected_every_minutes": 1440,
 "expected_since": "2026-09-18T20:27:56Z", "due_by": "2026-09-13T18:02:00Z",
 "last_attempt_at": "2026-09-13T10:45:12Z",
 "last_failure": {"at": "2026-09-13T10:45:12Z", "reason": "BrokerUnavailable: no venue session"},
 "overdue": true, "detail": "micro_live as_of 2026-09-11"}
```
上次尝试本身已超过监控期限，所以不再是 `failed`,它不在跑了，而不是在跑并被拒。`backup_overdue` 照常
出现。

**`overdue`，来源说不出来**（迁移前就存在、从未跑过的活动；开发检出同此）
```json
{"id": "schedule", "state": "overdue", "last_success_at": null, "age_minutes": null,
 "expected_every_minutes": 10080, "expected_since": null, "due_by": null,
 "last_attempt_at": null, "last_failure": null, "overdue": true, "detail": ""}
```
不给宽限期，行为与今天完全一致。

## 6. 状态转换（`ledger_confirm` 的完整一条线）

| 时刻 | 发生了什么 | state | `/attention` |
| --- | --- | --- | --- |
| 迁移写入 `expected_since` | 每日确认的代码随版本到机器上 | `awaiting_first_run` | 无 |
| 首次期限之前 | 还没到 03:45 | `awaiting_first_run` | 无 |
| 03:45 跑通 | 两个账本都确认，写成功心跳 | `current` | 无 |
| 次日 03:45 被拒 | 报告比成功更新，尝试是新鲜的 | `failed` | critical `ledger_unconfirmed` |
| 人在控制台确认掉差额 | 成功比报告更新 | `current` | 无（自动消失，无需人工清除） |
| 若改为连尝试都停了 | `last_attempt_at` 超过监控期限 | `overdue` | normal `backup_overdue` |
| 若首次期限过了仍未跑 | 从 `awaiting_first_run` 直接翻过去 | `overdue` | normal `backup_overdue` |

两条不会发生的转换，写在这里以免前端为它们留分支：`awaiting_first_run` 永远不会直接变成 `failed`
（没跑过就没有报告），`current` 也不会因为时间流逝变成 `awaiting_first_run`（有成功记录就永远不是首次）。

## 7. 前端要改的三处

1. System 页每行按 `state` 显示，**不要再读 `overdue` 布尔，也不要从 `last_success_at == null` 推
   "从未运行"**。四个状态各一句文案，两种语言。`overdue` 保留只为不破坏旧读者，它与 `state` 会在
   `awaiting_first_run` 和新鲜的 `failed` 两处故意不一致。
2. `awaiting_first_run` 不进任何"需要处理"的计数或徽标。
3. `failed` 时把 `last_failure.reason` 原样显示,那是后端给的原话（可能是券商现金差额这类可操作信息），
   不要改写、不要截断到看不出金额。

## 8. 本稿不做

- 不改任何活动的时序、窗口或触发方式。`expected_every_minutes` 的数值不动。
- 不改 `backup` 的 urgent 判定。
- 不新增待办 kind,`failed` 复用已有的 `ledger_unconfirmed`，`overdue` 复用已有的 `backup_overdue`。
- 不动自主权限的任何部分。

## 9. 实现记录

| 步骤 | 状态 |
| --- | --- |
| `operations.declare` 写 `expected_since`，幂等 | 已完成 |
| `service/system.py` 产出 `state` 与四个新字段 | 已完成 |
| `notifications.py` 只在 `state == "overdue"` 时发 `backup_overdue` | 已完成 |
| 测试 | `tests/test_operations.py` 新增 11 项、`tests/test_attention.py` 新增 3 项、`tests/test_api.py` 新增 1 项 |

写入点两处，都是幂等的：`ct-ops` 的任意一次调用（发布时的 `heartbeat`、每日作业的 `maintain`、
`status`），以及 API 启动。所以一次发布或一次重启就会补齐，不需要额外的运维动作。

`/system/operations` 的路由返回 `dict`，新字段直接透出，接口形状没有变化。

**一处与契约的偏差**：第 3 节说 `expected_since` 在"既无成功记录也无既有条目"时写入。实现里还多一条：
活动来源文档存在与否决定这次是迁移还是新增（迁移取版本发布时刻，新增取当刻）。契约第 1 节第二条的表格
已经这么写了，这里只是说明它在代码里是靠那份文档是否存在来判断的,所以文档一旦写下，就再没有第二次
迁移。

## 10. 原实施顺序

1. `operations.declare(db)` 写 `expected_since`，幂等；发布与 API 启动各调一次。
2. `service/system.py` 产出 `state` 与四个新字段，`overdue` 保持原义。
3. `service/notifications.py` 的 `operations()` 只在 `state == "overdue"` 时发 `backup_overdue`。
4. 测试：四个状态各一例，抑制规则一例，老部署 `expected_since` 缺失时行为不变一例。
5. `deploy/checks.sh` 通过后提交，提交号与字段说明发前端。
