# 退出链路接口契约（草稿，供前端审阅）

日期：2026-09-19。状态：**草稿，尚未写任何代码**。按约定在实施前送审，因为部分成交、撤单、恢复
和可卖数量决定审批页的结构，不只是字段。前端审阅后的版本作为第二稿，实施以第二稿为准。

对应 `docs/UPGRADE_PLAN_2026-09-18.md` 第 5.3 节"统一授权并补齐全量退出"。本稿不修改前端、
`config/*.yaml`、交易权限或部署。样例中的数量、时间、id 都是示意值，不是账户读数。

---

## 0. 一句话说清这件事要解决什么

今天一个人可以批准卖出，链路是通的：提案带 `sell_quantity`，Hard Risk 的 HR14／HR14b 校验持仓
并把数量夹到账本持仓，执行时按券商真相重算，成交与部分成交经对账记录。缺的不是"能不能卖"，
而是三件事：

1. **审批页无法说清这一笔卖出会留下什么。** 没有一个地方回答"账本说 0.2、券商说多少、已经成交
   多少、这一笔之后还剩多少、这是不是清仓"。
2. **部分成交或在券商端被撤单后，剩下的部分没有出路。** 要卖掉余量只能重新做一次完整研究，而
   "把没卖完的卖完"不该是一次新研究。
3. **自主减仓可以逐步走到接近清仓。** `REDUCE` 每次至多半仓、每天至多一个持仓，但连续几天对同
   一持仓减半，数学上到不了零、实际上等于清仓。计划要求的"SELL 有独立授权，不能靠反复 REDUCE
   绕过清仓权限"，缺的正是这个累计上限。

---

## 1. 今天的实际行为（已核实，带位置）

| 事实 | 位置 |
| --- | --- |
| `SELL` 只在已有持仓时产生；`Underweight` 映射为 `REDUCE`，`Sell` 映射为 `SELL` | `policy.py::RATING_TO_ACTION` |
| `REDUCE` 按构造是半仓，`SELL` 是全部持仓；给了 `sell_quantity` 就用它 | `risk/engine.py` HR14 分支 |
| 数量被夹到账本持仓：`min(requested, position.quantity)` | 同上 |
| **减仓路径在 HR14／HR14b 之后直接返回**，所以置信度门、sizing、风险层级、事件黑窗、现金下限、集中度都不约束卖出 | 同上，早于 HR01／HR02 返回 |
| 熔断与梯子不在风控引擎里拦卖出，在执行时拦：`revalidation.invalidate_triggers` 含 `kill_switch_active` | `execution.py` |
| 梯子升级或市场状态变差**永不**使减仓失效 | `execution.py`，`reducing` 分支 |
| 执行时数量 = `min(券商真相算出的可卖量, 已授权量)`，向下取整到六位小数；不足精度则不下单 | `execution.py` 第 6 步 |
| **卖出不受 `min_reduction_ratio` 约束**；那条"不得少于批准的一半"只管买入 | 同上，SELL/REDUCE 提前返回 |
| 一个账户同时只允许一个活动订单（唯一索引） | `state_db.py` `one_active_order_per_account` |
| 真钱卖出走市价单："退出没成交就不算降了风险" | `config/risk_profile.yaml` `micro_live_execution.order_rules` |
| 风险层级永不约束卖出："填满的层级必须仍可减" | 同上 `buckets.micro_live.tiers` |
| `SELL` 永不自主；`autonomy.permits` 只认 `REDUCE` | `autonomy.py::AUTONOMOUS_ACTION` |
| 授权对象已把这件事作为数据给出：`SELL.autonomous.blocked_by = [never_autonomous]` | `GET /config/authority` |
| 批准时 `terms.sell_quantity` 必须与提案完全一致，不一致 409 | `service/approvals.py::_approvable` |
| 部分成交、未知状态、对账失败都已有记录与恢复；`/attention` 已有 `order_unknown` 与 `reconciliation_error` | `execution.py`、`service/notifications.py` |
| **控制台没有撤单入口**，任何路由都不撤单 | `api/app.py` |
| 公司行动（拆股、并股、特别分红）没有任何代码读取 | 全库无 |

结论：需要新增的是**可卖数量的事实、退出续做、自主减仓的累计上限**，以及把这些讲清楚的字段。
不需要新增下单入口，`service/approvals.py` 仍是唯一一条。

---

## 2. 要定的七个决定（每项给出我的建议与理由）

**决定一：一个人批准清仓时，到底授权了什么。**
建议：**授权一个数量，永不超卖。** 保留 `terms.sell_quantity` 必须完全一致这条规则，不放宽。
屏幕上显示的是"清仓（当前 0.2 股）"，批准授权的就是 0.2。执行时按 `min(授权, 当前可卖)` 下单，
所以持仓在这期间变少了就卖得更少；变多了就只卖 0.2，响应明确说**没有清完**并给出续做入口。
理由：超卖是不可逆的错，少卖是可以再来一次的事。

**决定二：可卖数量以账本还是券商为准。**
建议：**显示两者，按较小者下单。** `sellable.quantity = min(账本, 券商) - 活动订单已占用`。两者不
一致时不拒绝卖出（拒绝会在最需要退出时挡住退出），而是取小、标注不一致，并让差额照现有规则成为
对账事项。券商数字取已有的 kept reading，不为开屏多开一次握手。

**决定三：同账户已有活动订单时。**
建议：审批页**不提供批准按钮**，显示"该账户已有一笔订单在进行"。这是数据库唯一索引的既有事实，
提前说出来比让批准在下单时失败好。

**决定四：SELL 的"独立授权"是什么（计划 5.3 的核心）。**
建议：它是**自主一侧的累计上限**，不是对人的限制。人永远可以清仓，只受账户级的普适阻断（熔断、
真钱开关、预检、梯子停机），这些授权对象已经在报。新增
`buckets.<bucket>.autonomy.reduce.max_cumulative_reduction_pct`：滚动窗口内（建议 30 天）代码对同一
持仓累计移除的比例上限，从批准记录本身算出（`approved_by` 以 `autonomy:` 开头的那些，带 ticker 和
`sell_quantity`），与 `acted_today` 同样的做法，不另设计数器。**未声明则拒绝自主减仓**，代码
`no_cumulative_cap`，与"未声明的限额是拒绝而不是默认"这条既有规则一致（`tests/test_autonomy.py` 有
同名测试）。这一项是收紧，不需要冷静期。

**决定五：部分成交或券商端撤单后的余量。**
建议：新增 `POST /proposals/{id}/continue_exit`，创建一个**绑定原提案**的续做提案，数量只能是
"原授权减去已成交"，并再夹到当前可卖；沿用原 `run_id` 与原 `authority_version`，所以授权变化的确认
规则照常适用；它**不会自己执行**，仍需一次人工批准。所有续做的授权量之和永不超过原授权，所以这不是
绕过清仓权限的路，而是把一次被打断的退出做完。理由：要卖掉余量而必须重跑完整研究，是把"没卖完"
当成"新想法"。

**决定六：撤单要不要做成控制台动作。**
建议：**不做。** 撤单是一次 venue 写操作，而 Robinhood 自己的 App 就是撤单的地方（owner 的"不重造
Robinhood"规则）。本系统要做的是**正确处理别处发生的撤单**：对账已把 `canceled` 映射为 CANCELED 并
保留部分成交，屏幕说"已在券商端撤单，已成交 N"，并在仍想退出时提供续做入口。

**决定七：卖出要不要受事件黑窗、层级、置信度约束。**
建议：**都不受，并把这件事写进契约**，因为今天的代码已经如此，而"退出也照抄开仓的限制"是一个很容易
被好意加进来的错。退出仍要过的检查是：持仓存在、数量有效且不超卖、报价可读且不过期、账户可读、
熔断未启、券商预审通过。

---

## 3. 接口契约

### 3.1 `GET /proposals/{id}` 新增 `exit` 块（仅 `SELL`／`REDUCE` 提案有，其余为 `null`）

| 字段 | 说明 |
| --- | --- |
| `intent` | `close_position`（`SELL`）\| `reduce_position`（`REDUCE`）。由动作派生，不新增存储字段 |
| `authorised_quantity` | 批准会授权的股数，即 `terms.sell_quantity` 必须等于的那个数 |
| `sellable.quantity` | 现在实际可卖：`min(账本, 券商)` 减去活动订单占用 |
| `sellable.ledger_quantity` / `sellable.venue_quantity` | 两个来源各自的数，**任一不可读时为 `null`，不写 0** |
| `sellable.venue_as_of` | 券商读数的时刻（kept reading）；`null` 表示从未读到 |
| `sellable.agrees` | 两个来源是否一致（容差 1e-6）；`false` 时 `note` 说明差在哪 |
| `already_filled` | 本提案此前已成交的股数（部分成交、或被撤单前成交的部分） |
| `remaining_after_execution` | 这一笔按当前可卖全部成交后，该持仓还会剩下多少 |
| `closes_position` | `remaining_after_execution == 0`。**这是屏幕唯一可以据以说"清仓"的字段**，不要从 `intent` 推断 |
| `min_sellable_increment` | 券商支持的最小增量（现为 1e-6）；余量低于它就卖不掉 |
| `blocked_by[]` | 闭集，见下。空表示这一笔在**持仓层面**没有障碍 |
| `continuation_of` / `continued_by` | 续做链的两端，`null` 表示不在链上 |

`exit.blocked_by` 代码（闭集，前端本地化）：

| 代码 | 含义 |
| --- | --- |
| `active_order` | 该账户已有一笔活动订单（唯一索引），此时不提供批准 |
| `position_gone` | 账本与券商都不再持有该标的 |
| `quantity_unknown` | 券商读不到且账本未确认，数量无法确定；不猜，不卖 |
| `below_precision` | 可卖余量小于券商最小增量 |
| `reconciliation_pending` | 本提案上一次尝试状态未明，对账仍在进行；**绝不提供再卖一次** |

**分工要说清**：`exit.blocked_by` 只讲**这个持仓**；熔断、真钱开关、预检、梯子停机这些**账户级**
阻断在 `GET /config/authority.actions.SELL.human_approval.blocked_by`。审批页必须读两处，任一非空
就不提供批准。

### 3.2 `POST /proposals/{id}/approve` 的响应新增 `exit_result`

现有字段（`status`、`outcome`、`submitted`、`order`、`error`、`reasons`、`message`、
`authority_changed`）不变。减仓提案另带：

| 字段 | 说明 |
| --- | --- |
| `requested_quantity` | 授权量 |
| `submitted_quantity` | 实际下单量（`min(授权, 当前可卖)`，向下取整到精度） |
| `filled_quantity` | 已成交（同步读到的部分；未终态时可能为 0） |
| `remaining_quantity` | 该持仓仍剩余的股数 |
| `position_closed` | 是否已清仓 |
| `continuation_available` | 是否可以调用 `continue_exit` |

### 3.3 `POST /proposals/{id}/continue_exit`（新增）

| 请求字段 | 规则 |
| --- | --- |
| `request_id` | UUID，幂等；同 id 重放返回同一个续做提案，同 id 不同内容 409 |
| `confirm` | 必须 `true` |
| `authority_version` | 与批准一致的规则：与当前不符 409 要求重载 |
| `acknowledge_authority_change` | 放宽／混合／未知时必须为 `true`，且必须带 `authority_version` |

| 情形 | 响应 |
| --- | --- |
| 原提案是 `PARTIALLY_FILLED`，或 `CANCELED` 且有部分成交 | 200，返回新提案摘要，`continuation_of` 指向原提案；状态 `WAITING_FOR_APPROVAL`，**不执行** |
| 原提案未成交任何数量 | 400 `"nothing was filled; approve the original proposal again or request fresh research"` |
| 原提案仍在对账中 | 409 `"the previous attempt is unresolved; reconciliation continues"` |
| 续做量会超过原授权减已成交 | 服务端自行夹小，不报错；响应里的数量就是夹小后的 |
| 持仓已不存在 | 400 `"the position is gone; nothing to continue"` |

续做提案继承原 `run_id`、`authority_version`、`strategy`、`instrument_type`，`purpose` 记为
`exit_review`，并在历史里留一条 `exit_continued`。

### 3.4 `GET /config/authority` 的 `SELL` 部分（已存在，本轮只增一个代码）

`SELL.autonomous.blocked_by` 现有 `never_autonomous` 不变。`REDUCE.autonomous.blocked_by` 新增
`no_cumulative_cap`（未声明 `max_cumulative_reduction_pct`），并在 `REDUCE.limits` 增
`max_cumulative_reduction_pct` 与 `cumulative_used_pct`（该持仓在窗口内已被代码移除的比例）。
`consistency` 新增一项 `reduce_without_cumulative_cap`，说明"每次半仓且每天一个"不足以阻止代码把一
个持仓逐步减到接近零。

### 3.5 `/attention` 新增一个 kind

| kind | 出现 | 退出 | 严重性／优先级 |
| --- | --- | --- | --- |
| `exit_incomplete` | 减仓提案部分成交或被撤单且有成交，持仓仍有余量，72 小时内，且未创建续做 | 创建了续做提案；持仓清零；或满 72 小时（之后只在 Activity 里） | high／3，`requires_person: true`，`actions: ["continue_exit", "open"]` |

---

## 4. 四种响应样例

**正常**（清仓待批准，账本与券商一致，无障碍）：

```json
"exit": {
  "intent": "close_position", "authorised_quantity": 0.2,
  "sellable": {"quantity": 0.2, "ledger_quantity": 0.2, "venue_quantity": 0.2,
               "venue_as_of": "2026-09-19T15:04:02+00:00", "agrees": true, "note": null},
  "already_filled": 0.0, "remaining_after_execution": 0.0, "closes_position": true,
  "min_sellable_increment": 0.000001, "blocked_by": [],
  "continuation_of": null, "continued_by": null
}
```

**空数据**（从未读到券商，账本也未确认）：

```json
"exit": {
  "intent": "close_position", "authorised_quantity": 0.2,
  "sellable": {"quantity": null, "ledger_quantity": 0.2, "venue_quantity": null,
               "venue_as_of": null, "agrees": null,
               "note": "the venue has not been read; the ledger's own figure is unconfirmed"},
  "already_filled": 0.0, "remaining_after_execution": null, "closes_position": false,
  "min_sellable_increment": 0.000001,
  "blocked_by": [{"code": "quantity_unknown",
                  "detail": "no venue reading and no confirmed ledger; the quantity cannot be established"}],
  "continuation_of": null, "continued_by": null
}
```
屏幕显示"数量无法确认"，不提供批准，不显示 0 股。

**过期／被打断**（部分成交后余量未卖，可续做）：

```json
"exit": {
  "intent": "close_position", "authorised_quantity": 0.2,
  "sellable": {"quantity": 0.08, "ledger_quantity": 0.08, "venue_quantity": 0.08,
               "venue_as_of": "2026-09-19T15:31:40+00:00", "agrees": true, "note": null},
  "already_filled": 0.12, "remaining_after_execution": 0.0, "closes_position": true,
  "min_sellable_increment": 0.000001, "blocked_by": [],
  "continuation_of": null, "continued_by": null
}
```
配套 `exit_result`：`{"requested_quantity": 0.2, "submitted_quantity": 0.2, "filled_quantity": 0.12,
"remaining_quantity": 0.08, "position_closed": false, "continuation_available": true}`，
`/attention` 出现一条 `exit_incomplete`。

**失败**（该账户已有活动订单，且授权版本在放宽后未确认）：

```json
"exit": {
  "intent": "reduce_position", "authorised_quantity": 0.1,
  "sellable": {"quantity": 0.0, "ledger_quantity": 0.2, "venue_quantity": 0.2,
               "venue_as_of": "2026-09-19T15:04:02+00:00", "agrees": true,
               "note": "0.2 is committed to an order already working in this account"},
  "already_filled": 0.0, "remaining_after_execution": 0.2, "closes_position": false,
  "min_sellable_increment": 0.000001,
  "blocked_by": [{"code": "active_order",
                  "detail": "an order is already working in micro_live; one account holds one active order"}],
  "continuation_of": null, "continued_by": null
}
```
批准请求会以 409 被拒（授权变化未确认），并且即便确认了也仍被 `active_order` 挡住。

---

## 5. 审批页结构建议（这是前端要的那部分）

一屏内要能回答四个问题，顺序建议如下：

1. **这一笔会卖掉多少、之后还剩多少、是不是清仓。** 用 `authorised_quantity`、
   `sellable.quantity`、`remaining_after_execution`、`closes_position`。"清仓"只认
   `closes_position`，不要从动作名或 `intent` 推断。
2. **数量的来源是否一致、读于何时。** 两个来源各自显示，不一致时显示两者与 `note`；任一为 `null`
   显示"未读到"，不显示 0。
3. **有什么挡着。** `exit.blocked_by`（这个持仓）与 `authority.actions.SELL.human_approval.blocked_by`
   （这个账户）合并展示，任一非空就不提供批准按钮。
4. **上一次尝试留下了什么。** `already_filled` 非零时显示"已成交 N"，
   `continuation_available` 为真时提供"把剩下的卖完"，它会新建一个待批准的续做提案，而不是立即下单。

必须避免的三件事：对账未明时提供"再卖一次"；把 `intent` 当成结果；把账本与券商不一致做成拒绝退出。

---

## 6. 本轮不做

- **公司行动调整**：没有任何数据源。拆股会让账本与券商数量不一致，届时按决定二取小并作为对账事项
  浮现，不假装算得准。
- **控制台撤单**：见决定六，撤单留在券商端。
- **给卖出加事件黑窗、风险层级、置信度门**：见决定七，明确不做。
- **自主清仓**：`SELL` 永不自主，本轮不变。

---

## 7. 实施顺序与验收（第二稿确定后）

1. `exit` 块与 `sellable` 的计算（只读，用 kept reading，不新增握手），`ProposalDetail` 加严格模型。
2. `autonomy.reduce.max_cumulative_reduction_pct`：从批准记录算累计比例，未声明则拒绝；授权对象与
   `consistency` 同步。
3. `continue_exit`：续做提案、授权版本继承、累计不超原授权、幂等。
4. `exit_result` 与 `/attention` 的 `exit_incomplete`。
5. 隔离测试：超卖（授权 > 可卖、可卖 > 授权）、部分成交后续做、券商端撤单后续做、对账未明时拒绝
   再卖、活动订单占用、账本与券商不一致、精度不足、授权在研究后收紧与放宽、续做累计不超原授权、
   自主减仓累计触顶。全部离线，仍只有 `service/approvals.py` 一条下单入口。
6. `deploy/checks.sh` 通过后提交，并把提交号与字段说明发前端。

验收边界：本轮完成不等于可以启用自主减仓。`buckets.micro_live.autonomy.reduce.enabled` 仍是关的，
打开它是单独的配置决定，且届时会被新的累计上限约束。
