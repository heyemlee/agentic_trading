# 退出链路接口契约（第二稿）

日期：2026-09-19。状态：**第二稿，仍未写任何代码**。第一稿经前端审阅（见 git 历史 `4b98235`），本稿
按审阅意见改写：第一、二、三、六、七条原则保留，其中第一条的一个推论是错的、已纠正；第四、五条重写。
实施以本稿为准，仍待前端确认第四、五条。

对应 `docs/UPGRADE_PLAN_2026-09-18.md` 第 5.3 节。本稿不修改前端、`config/*.yaml`、交易权限或部署。
样例中的数量、时间、id 都是示意值，不是账户读数。

---

## 0. 第一稿的四处错误，先认下来

| 错在哪 | 为什么错 | 本稿怎么改 |
| --- | --- | --- |
| 用 `PARTIALLY_FILLED` 作为续做资格 | `PENDING_EXECUTION = (SUBMITTING, SUBMITTED, PARTIALLY_FILLED, UNKNOWN)`（`proposals.py:41`）。部分成交意味着**订单可能还在挂着**，此时创建续做就是重复卖出 | 资格改为"原订单已终结 + 对账完成 + 账户无活动订单 + 链上还有额度"，见 §2.1 |
| 额度按"已发出的授权量之和"计 | 零成交撤单的额度应当可以再用；按发出量计会把它永久作废 | 额度按**已成交**与**未完成占用**计，见 §2.3 |
| 持仓变多时给续做入口 | 原授权不覆盖后来新增的持仓，那是新想法 | 原授权已全部成交则链关闭，`allowance_spent`；新增持仓需要新提案 |
| 零成交撤单时建议"再批准原提案" | `_approvable` 要求状态为 `PROPOSED`，`CANCELED` 的提案无法再批准 | 零成交撤单同样走续做（额度全额返还），文案改正，见 §2.6 |

另有一处疏忽：账户级权限必须按**实际动作**读 `actions.SELL` 或 `actions.REDUCE`，不能一律写 `SELL`。
本稿所有字段都带 `authority_action` 明说该读哪一个。

---

## 1. 今天的实际行为（已核实，带位置）

| 事实 | 位置 |
| --- | --- |
| `SELL` 只在已有持仓时产生；`Underweight`→`REDUCE`，`Sell`→`SELL` | `policy.py::RATING_TO_ACTION` |
| `REDUCE` 按构造是半仓，`SELL` 是全部持仓；给了 `sell_quantity` 就用它 | `risk/engine.py` HR14 分支 |
| 数量被夹到账本持仓：`min(requested, position.quantity)` | 同上 |
| **减仓路径在 HR14／HR14b 之后直接返回**，置信度门、sizing、风险层级、事件黑窗、现金下限、集中度都不约束卖出 | 同上，早于 HR01／HR02 返回 |
| 熔断与梯子在执行时拦，不在风控引擎里 | `execution.py`，`revalidation.invalidate_triggers` 含 `kill_switch_active` |
| 梯子升级或市场状态变差**永不**使减仓失效 | `execution.py`，`reducing` 分支 |
| **过期提案一律不执行**，减仓也不例外；检查在 `revalidate` 第 1 步，在动作分支之前 | `execution.py:168` |
| 执行时数量 = `min(券商真相可卖量, 已授权量)`，向下取整到六位小数；不足精度则不下单 | `execution.py` 第 6 步 |
| **卖出不受 `min_reduction_ratio` 约束**；那条只管买入 | 同上，SELL/REDUCE 提前返回 |
| `PARTIALLY_FILLED` 是**未终结**状态 | `proposals.py:41` `PENDING_EXECUTION` |
| 订单预留在状态离开 `PENDING_EXECUTION` 时释放；一个账户同时只允许一个活动预留 | `proposals.py:315`、`state_db.py:28` |
| 券商终态映射：`filled`→EXECUTED，`canceled`／`expired`／`done_for_day`→CANCELED，`rejected`→REJECTED | `execution.py:728` |
| `filled_quantity` 是券商累计成交量，赋值而非累加 | `execution.py:725` |
| `CANCELED` 的提案无法再批准（`_approvable` 要求 `PROPOSED`） | `service/approvals.py` |
| 真钱卖出走市价单；风险层级永不约束卖出 | `config/risk_profile.yaml` `micro_live_execution.order_rules`、`buckets.micro_live.tiers` |
| `SELL` 永不自主；授权对象已把它作为数据给出 | `autonomy.py::AUTONOMOUS_ACTION`、`GET /config/authority` |
| 控制台没有撤单入口；公司行动无任何代码读取 | `api/app.py`；全库无 |

需要新增的是：**可卖数量的事实、退出续做、自主减仓的持仓下限**，以及把这些讲清楚的字段。
`service/approvals.py` 仍是唯一下单入口。

---

## 2. 七个决定（一、二、三、六、七已接受；四、五重写待确认）

### 2.1 决定一（已接受，含一处纠正）：授权一个数量，永不超卖

保留 `terms.sell_quantity` 必须完全一致这条规则。屏幕显示"清仓（当前 0.2 股）"，批准授权 0.2。
执行按 `min(授权, 当前可卖)` 下单，所以持仓变少就卖得少。

**纠正**：持仓变多时**不提供续做**。原授权不覆盖后来新增的份额，那是一个新想法，需要新提案。
链上额度用尽时 `continuable.blocked_by` 为 `allowance_spent`。

### 2.2 决定二（已接受）：两个来源都显示，按较小者下单

`sellable.quantity = max(0, min(可读来源) − 活动预留占用)`。不一致时不拒绝退出，取小、标注，差额照
现有规则成为对账事项。券商数字取已有 kept reading，不为开屏多开握手；读数过期按 §2.4 处理。

### 2.3 决定三（已接受，重写算法）：额度按整条链算

一条退出链由**根提案**（第一个 `SELL`／`REDUCE`）和它的续做组成。

| 量 | 定义 |
| --- | --- |
| `allowance` | 根提案的 `authorised_quantity`。这是这条链永久的上限 |
| `filled` | 链上所有成员的 `filled_quantity` 之和（券商累计成交，不含未成交） |
| `committed` | 链上仍未完成的成员的 `authorised_quantity` 之和：状态为 `PROPOSED`、`APPROVED` 或仍在 `PENDING_EXECUTION` 的那些 |
| `remaining_allowance` | `max(0, allowance − filled − committed)` |

所以零成交撤单会把那部分额度**还回**链上（它既没成交也不再占用），而已成交的部分永久扣减。

**重复分支的防护**（三条，缺一不可）：

1. **一条链同时只允许一个未完成续做。** 已存在时，`continue_exit` 返回那一个而不新建。
2. `request_id`（UUID）幂等：同 id 重放返回同一个续做；同 id 不同内容 409。
3. 创建在数据库事务内完成并再次检查前两条，所以双标签页、重启、并发重放都不会产生第二个可执行分支。

### 2.4 决定四（重写，待确认）：数量口径与"清仓"的判定

四个量，各自的口径写明：

| 字段 | 口径 |
| --- | --- |
| `sellable.quantity` | `min(可读来源) − committed_quantity`。**取小是为了不超卖，不能据此判断清仓** |
| `expected_submit_quantity` | `min(authorised_quantity, sellable.quantity)`，向下取整到 `min_sellable_increment` |
| `expected_remaining` | **`max(可读来源) − expected_submit_quantity`**，下限 0。用较大者，因为"是否还剩"必须悲观回答 |
| `expected_remaining_basis` | `both_sources_agree` \| `ledger_only` \| `venue_only` \| `larger_of_two` |

`expected_closes_position` 是三态：

| 值 | 条件 |
| --- | --- |
| `true` | 两个来源都可读、一致（容差 1e-6），且 `expected_remaining == 0` |
| `false` | 两个来源都可读，但 `expected_remaining > 0` |
| `null` | 任一来源不可读或过期。屏幕显示"无法确认"，**不填 0，不显示"预计清仓"** |

审批前屏幕只说"**预计清仓**"，依据 `expected_closes_position == true`。

成交后另一组字段，同样三态：

| 字段 | 口径 |
| --- | --- |
| `confirmed_remaining` | 成交后重新读到的持仓余量；任一来源不可读时为 `null` |
| `confirmed_remaining_basis` | 同上四值 |
| `position_closed` | `true` 仅当两个来源都**新读到**且都为 0；`false` 当确认仍有余量；`null` 当无法确认。**无法确认不填 0，屏幕不显示"已清仓"** |

**读数过期的规则**：`venue_as_of` 早于 `venue_max_age_seconds`（建议 180，与控制台既有阈值一致）时，
`venue_stale: true`，**该来源按不可读处理**：sizing 退回账本单一来源，`expected_closes_position` 与
`position_closed` 变为 `null`。过期的券商读数不能用来声称清仓。

### 2.5 决定五（重写，待确认）：自主减仓的持仓下限，不用滚动窗口

**撤回**第一稿的 `max_cumulative_reduction_pct`（滚动 30 天）。你们的反驳成立：窗口只约束窗口内，
不能阻止长期逐步清空。

改为一条持久下限 `buckets.<bucket>.autonomy.reduce.min_remaining_pct_of_opened`：

| 问题 | 答案 |
| --- | --- |
| 分母是什么 | **人最后一次开仓或加仓成交后该持仓的数量**（`opened_baseline_quantity`）。从批准与成交记录算出：`approved_by` **不以** `autonomy:` 开头、动作为 `BUY`／`ADD` 的最后一笔成交之后的持仓量 |
| 窗口起点 | 没有窗口。这是一条持久下限，所以"长期逐步清空"被堵死：代码把持仓减到下限后，再往下永远只能由人来做 |
| 加仓后如何处理 | 人加仓会抬高基线，代码因此重获空间。这是对的：人加仓是人表达了看法 |
| 如何计数 | 已离开的份额按**实际成交**计；活动预留按**占用**计，所以两次自主动作不会重复减同一份 |
| 没有基线时 | 拒绝，代码 `no_opened_baseline`（转入的持仓、种子账本都属此类），失败闭合 |
| 配置未声明时 | 拒绝自主减仓，代码 `no_min_remaining`，与"未声明的限额是拒绝而不是默认"一致 |

这是收紧，不需要冷静期。`SELL` 仍永不自主，本条只约束 `REDUCE`。

**接口位置的纠正**：`/config/authority` 没有标的，不能返回"该持仓"的用量。所以
`actions.REDUCE.limits` 只报**规则本身**（`min_remaining_pct_of_opened`），**按持仓的数字放在提案
详情的 `exit.autonomous` 块**，那里才有确定的标的。

### 2.6 决定六（已接受，文案纠正）：撤单留在券商端，恢复走续做

撤单不做成控制台动作，Robinhood App 就是撤单的地方。本系统负责正确处理别处发生的撤单。

零成交撤单的恢复：**同样走 `continue_exit`**。额度全额返还，所以续做量等于原授权，仍需一次人工批准。
第一稿写的"再批准原提案"是错的，代码不允许。

**新鲜度**：续做沿用**根提案的新鲜度锚点**（`created_at`），不重新起算，否则就是把陈旧研究洗成一个
看起来新鲜的提案。根提案已过期时 `continue_exit` 拒绝，代码 `research_expired`，文案指向重新研究。
这与现有行为一致（`execution.py:168`，过期提案一律不执行）。

> 一个反面论据，供你们否决：退出的论点变旧不构成继续持有的理由，所以也可以主张"允许过期后的退出并
> 标注"。我倾向保持与现有行为一致，但这条值得你们表态。

### 2.7 决定七（已接受）：卖出不受事件黑窗、层级、置信度约束

今天的代码已经如此，写进契约是为了防止有人出于好意把开仓的限制照抄给退出。退出仍要过的检查：
持仓存在、数量有效且不超卖、报价可读且不过期、账户可读、熔断未启、券商预审通过。

---

## 3. 接口契约

### 3.1 `GET /proposals/{id}` 的 `exit` 块（仅 `SELL`／`REDUCE`，其余 `null`）

| 字段 | 说明 |
| --- | --- |
| `action` / `authority_action` | `SELL` 或 `REDUCE`。**账户级权限读 `/config/authority.actions[authority_action]`**，不要写死 |
| `intent` | `close_position`（SELL）\| `reduce_position`（REDUCE），由动作派生 |
| `authorised_quantity` | 批准会授权的股数，`terms.sell_quantity` 必须等于它 |
| `sellable.*` | `quantity`、`basis`、`ledger_quantity`、`venue_quantity`、`venue_as_of`、`venue_stale`、`committed_quantity`、`agrees`、`note`。任一来源不可读时该项为 `null`，不写 0 |
| `expected_submit_quantity` | 见 §2.4 |
| `expected_remaining` / `expected_remaining_basis` | 见 §2.4，按较大来源 |
| `expected_closes_position` | 三态，见 §2.4。**屏幕只据此显示"预计清仓"** |
| `min_sellable_increment` | 券商最小增量（现为 1e-6） |
| `already_filled` | 本提案已成交股数 |
| `chain.*` | `root_proposal_id`、`allowance`、`filled`、`committed`、`remaining_allowance`、`members[]`（每项 `proposal_id`、`status`、`authorised_quantity`、`filled_quantity`） |
| `continuable.*` | `eligible`（bool）、`quantity`（可续做股数）、`blocked_by[]`。**在 GET 里，所以刷新页面或从待办进入都读得到** |
| `autonomous` | 仅 `REDUCE` 有，见 §3.3；`SELL` 为 `null` |
| `blocked_by[]` | 这一笔在**持仓层面**的阻断，闭集见下 |

`exit.blocked_by` 与 `continuable.blocked_by` 共用一个闭集：

| 代码 | 含义 |
| --- | --- |
| `order_working` | 本链有成员处于 `PENDING_EXECUTION`（含 `PARTIALLY_FILLED`）：券商可能还在成交，此时创建续做会重复卖出 |
| `reconciliation_unresolved` | 有 `reconciliation_error` 或状态 `UNKNOWN`：对账未完成，**绝不提供再卖一次** |
| `active_order` | 该账户已有活动预留（唯一索引），任何新订单都进不去 |
| `allowance_spent` | `remaining_allowance == 0`：原授权已用尽。后来新增的持仓不属于本链，需要新提案 |
| `position_gone` | 账本与券商都不再持有 |
| `quantity_unknown` | 券商不可读或过期，且账本未确认；不猜，不卖 |
| `below_precision` | 可续做量小于 `min_sellable_increment` |
| `research_expired` | 根提案已过其新鲜度窗口，见 §2.6 |

**分工**：`exit.blocked_by` 只讲这个持仓与这条链；熔断、真钱开关、预检、梯子停机在
`/config/authority.actions[authority_action].human_approval.blocked_by`。审批页两处都要读，
任一非空就不提供批准。

### 3.2 `POST /proposals/{id}/approve` 的响应新增 `exit_result`

现有字段不变。减仓提案另带：`requested_quantity`、`submitted_quantity`、`filled_quantity`、
`confirmed_remaining`、`confirmed_remaining_basis`、`position_closed`（三态）、
`continuable`（同 §3.1 的结构）。

### 3.3 `exit.autonomous`（仅 `REDUCE`）

```json
{
  "permitted": false,
  "min_remaining_pct_of_opened": 25,
  "opened_baseline_quantity": 0.4,
  "opened_baseline_at": "2026-09-12T14:02:11+00:00",
  "floor_quantity": 0.1,
  "current_quantity": 0.2,
  "headroom_quantity": 0.1,
  "blocked_by": [{"code": "not_enabled", "detail": "buckets.micro_live.autonomy.reduce.enabled is not true"}]
}
```

`headroom_quantity = max(0, current_quantity − floor_quantity)`；代码的减仓量不得超过它。
`opened_baseline_quantity` 为 `null` 时 `blocked_by` 含 `no_opened_baseline`。

### 3.4 `POST /proposals/{id}/continue_exit`

| 请求字段 | 规则 |
| --- | --- |
| `request_id` | UUID，幂等 |
| `confirm` | 必须 `true` |
| `authority_version` | 与批准同规则，不符 409 |
| `acknowledge_authority_change` | 放宽／混合／未知时必须 `true`，且必须带 `authority_version` |

| 情形 | 响应 |
| --- | --- |
| 有资格（终结 + 已对账 + 无活动预留 + 有剩余额度 + 可卖 > 0 + 研究未过期） | 200，新提案摘要，`continuation_of` 指向根提案，状态 `WAITING_FOR_APPROVAL`，**不执行** |
| 本链已有未完成续做 | 200，返回那一个，`deduplicated: true`（不新建第二个分支） |
| 订单仍在进行 | 409 `"an order from this exit is still working at the venue; reconciliation decides what remains"` |
| 对账未完成 | 409 `"the previous attempt is unresolved; reconciliation continues"` |
| 额度已用尽 | 400 `"the original authorisation is fully filled; a position acquired since is a new idea and needs its own proposal"` |
| 根提案已过期 | 400 `"the research behind this exit has expired; request fresh research"` |
| 持仓已不存在 | 400 `"the position is gone; nothing to continue"` |

续做提案继承根提案的 `run_id`、`authority_version`、`strategy`、`instrument_type` 与**新鲜度锚点**，
`purpose` 记为 `exit_review`，历史留一条 `exit_continued`。

### 3.5 `/attention` 新增一个 kind

| kind | 出现 | 退出 | 严重性／优先级 |
| --- | --- | --- | --- |
| `exit_incomplete` | 退出链已终结且对账完成、`remaining_allowance > 0`、持仓仍有余量、72 小时内、且本链无未完成续做 | 创建了续做；额度用尽；持仓清零；或满 72 小时 | high／3，`requires_person: true`，`actions: ["continue_exit", "open"]` |

**判定与接口、按钮同源**：`/attention` 这一项的出现条件就是 `exit.continuable.eligible`，三处不各算一遍。

---

## 4. 六种响应样例

**A. 正常**（清仓待批准，两来源一致，无阻断）：

```json
"exit": {
  "action": "SELL", "authority_action": "SELL", "intent": "close_position",
  "authorised_quantity": 0.2,
  "sellable": {"quantity": 0.2, "basis": "both_sources_agree", "ledger_quantity": 0.2,
               "venue_quantity": 0.2, "venue_as_of": "2026-09-19T15:04:02+00:00",
               "venue_stale": false, "committed_quantity": 0.0, "agrees": true, "note": null},
  "expected_submit_quantity": 0.2, "expected_remaining": 0.0,
  "expected_remaining_basis": "both_sources_agree", "expected_closes_position": true,
  "min_sellable_increment": 0.000001, "already_filled": 0.0,
  "chain": {"root_proposal_id": "p_41aa07", "allowance": 0.2, "filled": 0.0, "committed": 0.2,
            "remaining_allowance": 0.0,
            "members": [{"proposal_id": "p_41aa07", "status": "WAITING_FOR_APPROVAL",
                         "authorised_quantity": 0.2, "filled_quantity": 0.0}]},
  "continuable": {"eligible": false, "quantity": 0.0,
                  "blocked_by": [{"code": "order_working",
                                  "detail": "this exit has not been attempted yet"}]},
  "autonomous": null, "blocked_by": []
}
```

**B. 部分成交但订单仍挂着**（前端要求的第一个样例，**不可续做**）：

```json
"exit": {
  "action": "SELL", "authority_action": "SELL", "intent": "close_position",
  "authorised_quantity": 0.2,
  "sellable": {"quantity": 0.0, "basis": "both_sources_agree", "ledger_quantity": 0.08,
               "venue_quantity": 0.08, "venue_as_of": "2026-09-19T15:31:40+00:00",
               "venue_stale": false, "committed_quantity": 0.08, "agrees": true,
               "note": "0.08 is committed to an order still working in this account"},
  "expected_submit_quantity": 0.0, "expected_remaining": 0.08,
  "expected_remaining_basis": "both_sources_agree", "expected_closes_position": false,
  "min_sellable_increment": 0.000001, "already_filled": 0.12,
  "chain": {"root_proposal_id": "p_41aa07", "allowance": 0.2, "filled": 0.12, "committed": 0.08,
            "remaining_allowance": 0.0,
            "members": [{"proposal_id": "p_41aa07", "status": "PARTIALLY_FILLED",
                         "authorised_quantity": 0.2, "filled_quantity": 0.12}]},
  "continuable": {"eligible": false, "quantity": 0.0, "blocked_by": [
    {"code": "order_working",
     "detail": "the order is still working at the venue; it may yet fill the remaining 0.08"},
    {"code": "active_order", "detail": "an order is already working in micro_live"}]},
  "autonomous": null,
  "blocked_by": [{"code": "order_working", "detail": "the order is still working at the venue"}]
}
```
屏幕：显示"已成交 0.12，订单仍在进行"，**不提供续做按钮，也不提供批准**。`/attention` 不出
`exit_incomplete`（链未终结）。

**C. 部分成交后在券商端撤单且对账完成**（前端要求的第二个样例，**可续做**）：

```json
"exit": {
  "action": "SELL", "authority_action": "SELL", "intent": "close_position",
  "authorised_quantity": 0.2,
  "sellable": {"quantity": 0.08, "basis": "both_sources_agree", "ledger_quantity": 0.08,
               "venue_quantity": 0.08, "venue_as_of": "2026-09-19T15:46:10+00:00",
               "venue_stale": false, "committed_quantity": 0.0, "agrees": true, "note": null},
  "expected_submit_quantity": 0.0, "expected_remaining": 0.08,
  "expected_remaining_basis": "both_sources_agree", "expected_closes_position": false,
  "min_sellable_increment": 0.000001, "already_filled": 0.12,
  "chain": {"root_proposal_id": "p_41aa07", "allowance": 0.2, "filled": 0.12, "committed": 0.0,
            "remaining_allowance": 0.08,
            "members": [{"proposal_id": "p_41aa07", "status": "CANCELED",
                         "authorised_quantity": 0.2, "filled_quantity": 0.12}]},
  "continuable": {"eligible": true, "quantity": 0.08, "blocked_by": []},
  "autonomous": null, "blocked_by": []
}
```
配套 `exit_result`：`{"requested_quantity": 0.2, "submitted_quantity": 0.2, "filled_quantity": 0.12,
"confirmed_remaining": 0.08, "confirmed_remaining_basis": "both_sources_agree",
"position_closed": false, "continuable": {"eligible": true, "quantity": 0.08, "blocked_by": []}}`。
`/attention` 出一条 `exit_incomplete`。屏幕提供"**创建余量续做提案**"，旁注"创建后仍需批准"。

**D. 空数据**（从未读到券商，账本未确认）：

```json
"sellable": {"quantity": null, "basis": "ledger_only", "ledger_quantity": 0.2,
             "venue_quantity": null, "venue_as_of": null, "venue_stale": null,
             "committed_quantity": 0.0, "agrees": null,
             "note": "the venue has not been read; the ledger's own figure is unconfirmed"},
"expected_submit_quantity": null, "expected_remaining": null,
"expected_remaining_basis": "ledger_only", "expected_closes_position": null,
"continuable": {"eligible": false, "quantity": null, "blocked_by": [
  {"code": "quantity_unknown", "detail": "no venue reading and no confirmed ledger"}]}
```
屏幕："数量无法确认"，不提供批准，不显示 0 股，不显示"预计清仓"。

**E. 券商读数过期**（数量取小，但清仓判断为"无法确认"）：

```json
"sellable": {"quantity": 0.2, "basis": "ledger_only", "ledger_quantity": 0.2,
             "venue_quantity": 0.3, "venue_as_of": "2026-09-19T14:58:00+00:00",
             "venue_stale": true, "committed_quantity": 0.0, "agrees": false,
             "note": "the venue reading is older than 180s and is not used for sizing; the two sources disagreed when last read (ledger 0.2, venue 0.3)"},
"expected_submit_quantity": 0.2, "expected_remaining": null,
"expected_remaining_basis": "ledger_only", "expected_closes_position": null
```
这正是你们举的账本 0.2、券商 0.3 的例子：可以卖 0.2，但**不显示"预计清仓"**。

**F. 失败**（额度已用尽，后来又建了新仓）：

```json
"chain": {"root_proposal_id": "p_41aa07", "allowance": 0.2, "filled": 0.2, "committed": 0.0,
          "remaining_allowance": 0.0, "members": [{"proposal_id": "p_41aa07", "status": "EXECUTED",
                                                   "authorised_quantity": 0.2, "filled_quantity": 0.2}]},
"continuable": {"eligible": false, "quantity": 0.0, "blocked_by": [
  {"code": "allowance_spent",
   "detail": "the original authorisation of 0.2 is fully filled; the 0.15 held now was acquired since and needs its own proposal"}]}
```

---

## 5. 审批页结构（前端要的那部分）

一屏内按顺序回答四个问题：

1. **这一笔会卖多少、之后还剩多少、是不是清仓。** 用 `expected_submit_quantity`、
   `expected_remaining`、`expected_closes_position`。审批前最多说"**预计清仓**"，且仅当
   `expected_closes_position === true`；成交后说"已清仓"仅当 `position_closed === true`。
   两者为 `null` 时说"无法确认"，永不显示 0。
2. **数量来源与读取时间。** 两来源分别显示，`venue_stale` 为真时标注"券商读数已过期，未用于计算"。
3. **有什么挡着。** `exit.blocked_by` 与 `authority.actions[authority_action].human_approval.blocked_by`
   合并，任一非空不提供批准。
4. **上一次尝试留下了什么。** `already_filled` 非零显示"已成交 N"；`continuable.eligible` 为真时提供
   "创建余量续做提案"，旁注"创建后仍需批准"；为假时显示 `continuable.blocked_by` 的原因，
   **`order_working` 与 `reconciliation_unresolved` 这两种情况下按钮必须不存在**。

必须避免：对账未明或订单仍在进行时提供任何再卖入口；把 `intent` 当成结果；把两来源不一致做成拒绝
退出；把无法确认显示为 0 或"已清仓"。

---

## 6. 本轮不做

- **公司行动调整**：无数据源。拆股会使两来源不一致，按 §2.2 取小并作为对账事项浮现，不假装算得准。
- **控制台撤单**：见 §2.6。
- **给卖出加事件黑窗、风险层级、置信度门**：见 §2.7。
- **自主清仓**：`SELL` 永不自主。

---

## 7. 实施顺序与验收（第四、五条确认后）

1. `exit` 块：`sellable`、四个数量口径、三态判定、`chain`、`continuable`（只读，用 kept reading，
   不新增握手），`ProposalDetail` 加严格模型。
2. `autonomy.reduce.min_remaining_pct_of_opened`：基线从批准与成交记录算出，未声明或无基线则拒绝；
   规则进 `/config/authority.actions.REDUCE.limits`，按持仓的数字进 `exit.autonomous`。
3. `continue_exit`：链额度、一条链一个未完成续做、事务内二次检查、幂等、新鲜度锚点继承。
4. `exit_result` 与 `/attention` 的 `exit_incomplete`，判定与 `continuable.eligible` 同源。
5. 隔离测试：授权 > 可卖、可卖 > 授权、两来源不一致、券商读数过期、精度不足、部分成交仍挂单时拒绝
   续做、部分成交后撤单可续做、零成交撤单额度全额返还、额度用尽后新持仓不可续做、一条链只允许一个
   未完成续做（并发与双 `request_id`）、重启后不产生第二分支、根提案过期拒绝、自主减仓触及持仓下限、
   无基线时拒绝。全部离线，仍只有 `service/approvals.py` 一条下单入口。
6. `deploy/checks.sh` 通过后提交，并把提交号与字段说明发前端。

验收边界：本轮完成不等于可以启用自主减仓。`buckets.micro_live.autonomy.reduce.enabled` 仍是关的，
打开它是单独的配置决定，届时受新的持仓下限约束。
