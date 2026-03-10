# P2a 隔离测试矩阵（审计）

审计日期：2026-09-20。文件名沿用 P2a 的批次日期。

对应 `docs/UPGRADE_PLAN_2026-09-18.md` §10 的 P2a 停止条件：

> 隔离测试覆盖账户串用、授权撤销、部分成交、失联、配额耗尽、重复任务和恢复；保持唯一 live 下单入口

本稿是**审计**，不是计划。七项各列出：现有测试的精确名字（pytest node id，可直接跑）、它**实际断言**
了什么、以及**缺口**。只补了四处跨模块缺口（§9），实现型单测一律不复制——一个域内部的规则由该域自己
的测试持有，本稿不在它旁边再写一份。

本次审计全程离线（`tests/conftest.py` 的 `no_network_guard` 与 `real_state_guard` 照常生效），未改
`config/`、`state/`、`runs/`，未改任何生产代码，新增的四个测试都落在已有的域文件里。

---

## 0. 结论

| # | 维度 | 现有覆盖 | 本次补 | 剩余缺口 |
| --- | --- | --- | --- | --- |
| 1 | 账户串用 | 强（五个模块各自持有） | 2 | 见 §1 |
| 2 | 授权撤销 | 强 | 0 | 见 §2，判断为不补 |
| 3 | 部分成交 | 强（含真实券商适配器路径） | 0 | 无 |
| 4 | 服务失联 | 强 | 0 | 一项只能靠实机演练，见 §4 |
| 5 | 配额耗尽 | 中——三段各自成立，接缝未测 | 1 | 见 §5 |
| 6 | 重复任务 | 强 | （与 §1 的两个重合） | 无 |
| 7 | 重启恢复 | 强 | 0 | 无 |
| — | 唯一 live 下单入口 | **有一个真实漏洞** | 1 | 见 §8，已补 |

最重要的一条：§8 的静态证明原先只扫 `service/`，一个在 `service/` 之外直接拿 broker 调
`submit_order` 的模块两条断言都抓不到。已改为全包扫描，并做了反向验证（塞一个探针模块，测试变红；
旧写法不会）。

---

## 1. 账户串用

现有覆盖。Agentic（`micro_live`）、Demo（`core` / `sandbox`）与个人观察账户三者的边界，由五个模块各自
持有：绑定在服务层写死，记录按绑定过滤，证据检查读报告正文，券商适配器按账号过滤，调度按账户分周期。

| 测试 | 实际断言 |
| --- | --- |
| `tests/test_account_scope.py::test_a_personal_task_is_bound_to_one_observed_account_or_to_all_of_them` | 任务的 `account_scope_kind` 由服务器从快照写入（`single` / `all_personal`），不是模型说了算；未观察到的账号直接 `unknown personal account` |
| `tests/test_account_scope.py::test_a_conversation_keeps_the_scope_it_was_opened_under` | 同一 `conversation_id` 换账号、换 scope、或不带账号续聊，三种都抛 `bound to another account scope` |
| `tests/test_account_scope.py::test_each_scope_sees_its_own_records_and_nothing_of_another` | A 看不到 B 的记录也看不到旧的无归属记录；Agentic 视图的 `snapshot.accounts == []` 且只含 `micro_live` 记录 |
| `tests/test_account_scope.py::test_the_agentic_conversation_is_told_the_record_and_the_authority_and_nothing_personal` | 进模型的 context 里 `"snapshot" not in captured`，且 `fetch_snapshot` 被换成 `pytest.fail` —— 不是"没用到"，是"调了就红" |
| `tests/test_account_scope.py::test_a_single_account_conversation_is_told_that_account_alone` | 单账户会话的 context 只含该账号，且 `"risk" not in captured and "authority" not in captured` |
| `tests/test_research_records.py::test_personal_records_are_bound_to_their_account_and_never_to_another` | 研究记录按 `account_id` / `account_ids` 过滤，缺字段的旧记录不被归入任何账户 |
| `tests/test_research_records.py::test_the_scope_must_hold_the_bucket_and_a_holding_purpose_needs_the_holding` | `scope` 与 `bucket` 必须相符（`agentic` 只配 `micro_live`）；持仓类 purpose 对本账本不持有的标的直接拒绝 |
| `tests/test_bucket_context.py::test_a_report_about_the_wrong_account_cannot_support_an_opening` | 报告正文里提到未展示给该次运行的账户时，`check_evidence` 返回 `False`，理由含 `MICRO_LIVE` 与 `not in the snapshot` |
| `tests/test_bucket_context.py::test_core_as_an_ordinary_english_word_is_not_an_account_claim` | 三条把 core 当普通英文词用的句子不算账户声明——这条决定了上一条不是子串匹配 |
| `tests/test_robinhood.py::test_positions_from_other_accounts_are_excluded` | 适配器只返回 Agentic 账号的持仓 |
| `tests/test_robinhood.py::test_ambiguous_account_refuses_to_guess` | 账号认不准时拒绝，不挑一个 |
| `tests/test_robinhood.py::test_only_the_micro_live_bucket_reaches_the_real_money_venue` | `bucket_is_executable` 四种组合：只有 `micro_live` × `robinhood` 为真 |
| `tests/test_robinhood.py::test_micro_live_uses_its_own_ledger` | 两个 bucket 解析到两个账本文件 |
| `tests/test_autonomy.py::test_another_buckets_reduction_does_not_count` | 自主日额度按 bucket 分别计数 |
| `tests/test_api.py::test_accounts_are_reported_separately_and_never_summed_into_one_drawdown` | 回撤按账户分别记录，不并成一个 |
| `tests/test_evaluation.py::test_each_scope_counts_its_own_buckets_and_an_unlabelled_row_is_the_paper_books` | 评估按 scope 归集，无标注的行归纸面账本，不归 Agentic |
| `tests/test_schedule.py::test_holdings_come_before_candidates_and_each_account_keeps_its_own_period` | 三个任务算出三个不同的 period key —— 一个账户无法把另一个账户的周期标记为已跑 |
| `tests/test_advisor.py::test_readonly_analysis_reuses_upstream_graph_and_cannot_create_proposals` | 个人路径跑完不产生任何可批准提案 |

**缺口一（已补）**：`execution.execute` 里挡住"上一笔未了结"的那段写的是

```python
same_venue = (other.bucket == "micro_live") == (proposal.bucket == "micro_live")
```

`same_venue` 这一项**没有任何测试**。`tests/test_execution_recovery.py::test_unresolved_order_blocks_another_proposal`
用的是两笔 core 提案，删掉 `same_venue` 它照样绿。删掉的后果不是多下一单，而是反过来：纸面账本上一笔
没人对账的挂单，会挡住实盘的一次卖出。补法见 §9。

**缺口二（已补）**：`start_research` 的重放比对是 `(ticker, bucket, purpose)`，`start_advisor` 的是
`(kind, language, account_id, scope, conversation_id, message)`。两处现有的冲突测试都只改**问题本身**
（`BND` 对 `VTI`、换一句话），**账户那一项没被测**。去掉 `bucket` / `account_id`，一个账户的重放会拿到
另一个账户的运行，并把它的结论当成本账户的读——这是"重复任务"与"账户串用"的交叉点。补法见 §9。

**已核实不是缺口**：per-account 的执行锁（`approvals.py` 的 `file_lock(... f"{account_key}.execution")`）
与上面同源，由 §9 的第一个测试一并覆盖；跨账户的并发本身由
`tests/test_execution_recovery.py::test_concurrent_approval_retries_submit_once` 与
`tests/test_continue_exit.py::test_two_requests_at_once_still_make_one_continuation` 持有。

---

## 2. 授权撤销

现有覆盖。四条不同的"撤销"各有主：权限版本（authority version）、自主开关（autonomy）、配置收紧
（config change）、会话吊销（session revocation）。

| 测试 | 实际断言 |
| --- | --- |
| `tests/test_authority.py::test_a_screen_that_showed_an_older_authority_is_sent_back_to_reload` | 批准时带的 `authority_version` 与当前不符即 `ServiceConflict`，且**在开券商会话之前** |
| `tests/test_authority.py::test_a_tightened_authority_needs_no_acknowledgement_but_a_loosened_one_does` | 收紧（`autonomy.trip`）直接放行并在应答里标 `TIGHTENED`；放松（`autonomy.reset`）抛 `loosened`，提案留在 `PROPOSED`，补上确认后才过 |
| `tests/test_authority.py::test_an_acknowledgement_is_of_a_particular_version` | 确认必须针对某个版本，不带版本的确认被拒——否则"读的时候刚落地的变更"会被这次确认盖掉 |
| `tests/test_authority.py::test_a_proposal_that_predates_the_record_asks_a_person_to_look` | 早于权限记录的提案归入需确认，不按"没变过"处理 |
| `tests/test_authority.py::test_an_undeclared_position_floor_withholds_autonomous_reduction` | 下限没声明就不给自主减仓，不取默认值 |
| `tests/test_autonomy.py::test_a_trip_withholds_autonomy_until_a_person_clears_it` | 触发后自主权一直withheld 到人工清除 |
| `tests/test_autonomy.py::test_the_first_reason_is_the_one_kept_and_the_history_survives_a_reset` | 撤销理由与历史跨 reset 保留 |
| `tests/test_autonomy.py::test_a_withdrawal_stops_opening_too` | 撤销同时停掉开仓，不只停减仓 |
| `tests/test_autonomous_reductions.py::test_a_withdrawn_autonomy_says_why_and_does_nothing` | 整趟 pass 不做任何事并说明原因 |
| `tests/test_autonomous_reductions.py::test_a_proposal_born_under_another_authority_waits_for_a_person` | 在另一版权限下诞生的提案转人工 |
| `tests/test_autonomous_reductions.py::test_an_unaccountable_outcome_withdraws_autonomy_and_stops_the_pass` | 出现说不清的结果就撤销自主权并停住 |
| `tests/test_config_changes.py::test_classification_by_direction` | 14 组参数化：收紧=`IMMEDIATE`、放松=`COOLING`；没有规则的键 fail closed 按放松处理 |
| `tests/test_config_changes.py::test_immediate_change_is_written_recorded_and_committed` | 收紧写盘后**同一个 Service 实例**的 `profiles()` 立即读到新值，无需重启；并落审计与本地 commit |
| `tests/test_config_changes.py::test_loosening_change_needs_confirmation_then_waits` | 放松要密码 + 冷静期，到期前 `apply_config_changes()` 返回空，文件一字未写 |
| `tests/test_config_changes.py::test_pending_change_can_be_withdrawn_and_refuses_moved_ground` | 待生效的放松在同一项被收紧后失败，不从旧前提把上限抬回去 |
| `tests/test_execution_recovery.py::test_approving_while_real_money_is_switched_off_leaves_the_proposal_waiting` | `approve()` 读 `self.profiles()`，开关关着时抛 `real-money execution is disabled`，提案留 `PROPOSED` |
| `tests/test_execution_recovery.py::test_disabled_micro_switch_does_not_disable_order_reads` | 开关关着时对账**照常**开 `RobinhoodBroker` 去读——撤销权限不等于放弃一笔已在券商手里的单 |
| `tests/test_security_auth.py::test_password_rotation_revokes_sessions` | 改密码吊销全部会话 |
| `tests/test_security_auth.py::test_a_lost_phone_is_signed_out_without_touching_the_other_devices` | 单设备吊销不波及其他设备 |
| `tests/test_security_auth.py::test_identity_is_from_session_not_payload` | 身份取自会话，不取自请求体 |
| `tests/test_continue_exit.py::test_an_authority_that_moved_under_the_screen_refuses` | 续做退出同样查权限版本 |

**缺口（判断为不补）**：没有一个测试把"控制台写收紧 → 下一次批准被拒"整条串起来。这条接缝由上表两个
测试从两头夹住：`test_immediate_change_is_written_recorded_and_committed` 证明同一实例立即读到新值
（`profile.load_profiles` 无缓存，`ServiceBase.profiles()` 每次都读盘），
`test_approving_while_real_money_is_switched_off_leaves_the_proposal_waiting` 证明 `approve()` 读的就是
`self.profiles()`。再写一个端到端，只会是这两个的第三份副本，不增加任何新的失败模式——按"不复制实现型
单测"的口径，不补。

---

## 3. 部分成交

现有覆盖，且**包含真实券商适配器路径**，不只是模拟盘。

| 测试 | 实际断言 |
| --- | --- |
| `tests/test_robinhood.py::test_partial_fill_reconciles_only_what_filled` | 走 `execution.execute` + FakeMCP 券商 + `micro_live` 账本：`partially_filled`（授权 0.0929 / 已成 0.0400）只把 0.04 记进账本，现金按 `0.04 × 378.39` 扣，历史里留 `pending` 事件 |
| `tests/test_robinhood.py::test_every_order_state_maps_and_terminality_is_correct` | 券商全部订单状态到本系统生命周期状态的映射与终态判定 |
| `tests/test_execution_recovery.py::test_pending_then_filled_recovery_is_incremental_and_idempotent` | 参数化 partial/full：成交记录条数（partial 为 2）、总量等于订单量、二次对账后账本与 fills **逐字节不变**、预留被释放 |
| `tests/test_execution_recovery.py::test_partial_then_canceled_preserves_fill_and_terminal_state` | 部分成交后取消：状态 `CANCELED`、`filled_quantity` 保留、账本不动 |
| `tests/test_exits.py::test_the_allowance_counts_fills_and_only_the_unfilled_part_of_what_is_working` | 样例 B：授权 0.2、已成 0.12、仍挂单 ⇒ `committed == 0.08`、剩余额度 0.0（同时扣两次会算成负数） |
| `tests/test_exits.py::test_a_cancellation_after_a_partial_fill_returns_only_what_did_not_fill` | 样例 C：取消后归还 0.08，不是 0.2 |
| `tests/test_exits.py::test_the_chain_is_walked_from_any_member_and_the_allowance_is_the_roots` | 从链上任一成员出发结果相同，额度以根为准 |
| `tests/test_exits.py::test_a_remainder_below_the_venues_precision_cannot_be_continued` | 余量低于券商最小增量时不允许续做 |
| `tests/test_exit_result.py::test_an_exit_that_stopped_part_way_asks_to_be_finished` | 半途停下的退出产生待办；`test_the_to_do_goes_quiet_once_a_continuation_exists` 管它何时消失 |
| `tests/test_exit_result.py::test_a_partly_filled_exit_can_be_continued_and_approved` | 续做提案按未成交部分生成并可被批准 |
| `tests/test_continue_exit.py::test_the_book_binds_when_it_holds_less_than_the_allowance` | 续做量取"剩余额度"与"实际持仓"的较小者 |
| `tests/test_continue_exit.py::test_a_position_acquired_since_is_not_part_of_the_old_authorisation` | 期间新买入的份额不属于旧授权 |
| `tests/test_activity.py::test_a_fill_is_journalled_once_however_often_it_is_replayed` | 同一笔成交重放多次只入一次流水 |
| `tests/test_ledger_confirm.py::test_ct_state_confirm_refuses_while_the_broker_still_owns_an_order` | 券商还持有订单时拒绝给账本盖日期 |

**缺口**：无。这一项是七项里最密的。

---

## 4. 服务失联

现有覆盖。分四类：券商读不到、券商写到一半失联、状态/配置读不到、通知通道断。

| 测试 | 实际断言 |
| --- | --- |
| `tests/test_api.py::test_micro_live_answers_with_a_declared_shape_when_the_venue_is_unreachable` | 断线时 `/micro-live` 仍 200：`connected is False`、`account is None`（绝不半读）、`set(body) == set(MicroLive.model_fields)`、`checks`/`blocking`/`permissions` 都是可迭代的空壳而非缺键 |
| `tests/test_api.py::test_health_still_answers_when_the_configuration_is_what_broke` | 配置坏掉时 `/health` 用 `CONFIG_ERROR` 形状作答，其余字段是缺席而非置零 |
| `tests/test_api.py::test_a_calendar_that_cannot_be_read_reports_the_session_closed` | 日历读不到按休市处理 |
| `tests/test_robinhood.py::test_a_venue_that_cannot_be_reached_still_reads_as_disconnected` | 连不上时状态是 disconnected，不是"空账户" |
| `tests/test_robinhood.py::test_a_read_that_errors_is_unavailable_never_a_rejection` | 读失败是 unavailable，不会被当成券商拒单 |
| `tests/test_robinhood.py::test_a_venue_check_that_fails_before_placement_leaves_no_order_intent` | 下单前的检查失败 ⇒ 不留任何订单意图 |
| `tests/test_robinhood.py::test_a_transport_failure_retires_the_session_and_a_tool_error_does_not` | 传输层失败退掉会话、工具层错误不退——两种"失联"不混为一谈 |
| `tests/test_execution_recovery.py::test_unknown_submit_recovers_without_second_submission` | 券商已接单但应答丢失 ⇒ `SUBMIT_UNCERTAIN` + `UNKNOWN`，恢复后 `RECONCILED`，券商处始终只有 1 笔 |
| `tests/test_execution_recovery.py::test_an_exception_after_the_order_intent_is_left_for_reconciliation` | 记下订单意图之后抛异常 ⇒ 留给对账，不当成"没下单" |
| `tests/test_execution_scenarios.py::test_execution_scenario_case` | T11：券商状态不可用 ⇒ `INVALIDATED`，券商处 0 笔（fail closed） |
| `tests/test_exits.py::test_a_stale_venue_reading_is_reported_and_used_for_nothing` | 过期的券商读数会被报告出来，但不参与任何计算 |
| `tests/test_exits.py::test_positions_that_could_not_be_read_are_not_a_book_holding_nothing` | 读不到的持仓 ≠ 空持仓 |
| `tests/test_exit_result.py::test_an_unreadable_venue_after_the_order_cannot_confirm_anything` | 下单后读不到券商就不能声称"已清仓" |
| `tests/test_attention.py::test_a_broken_broker_connection_is_a_to_do_under_its_old_key` | 断线产生 `credential_invalid:robinhood` 待办，priority 1，key 沿用旧的 |
| `tests/test_attention.py::test_a_source_that_could_not_be_read_makes_the_answer_unconfirmable` | 有源读不到时不回答"无需处理" |
| `tests/test_notifications.py::test_a_condition_outlives_the_outage_that_stopped_it_being_delivered` | 重试预算耗尽转 `THROTTLED` 而非终态：通道修好后同一 key 仍能送出（2026-09-15 九条事件的真实回归） |
| `tests/test_notifications.py::test_state_that_cannot_be_read_is_the_loudest_blocker` | 状态读不到是最高优先级的阻塞项 |
| `tests/test_notifications.py::test_a_degraded_service_withholds_the_ping_it_would_otherwise_send` | 服务降级时不给外部看门狗发"我还好" |
| `tests/test_kept.py::test_a_failed_renewal_keeps_the_last_result_to_be_judged_by_its_age` | 刷新失败保留上一份结果，由其年龄判断可用与否 |
| `tests/test_ledger_confirm.py::test_ct_state_confirm_reports_a_venue_it_cannot_read_instead_of_dating_the_book` | 读不到券商就报告读不到，不给账本盖日期 |

**剩余缺口（离线补不了）**：`TODO.md` §11 其一记的那条——`credential_invalid` 事件链到真实设备推送，
从未做过一次实机演练。9-14 到 9-15 断了 15 小时无人知晓，事件一直在产生，只是当时 webpush 没有任何
设备订阅。演练要故意让实盘券商 token 失效，属于动实盘凭据，不在本审计范围内，也不该由测试代替。

---

## 5. 配额耗尽

两种额度，互不相干：**订阅额度**（Claude Code CLI 的会话上限，由 CLI 的 stderr 文本识别）与**研究额度**
（`research_schedule.max_analyses_per_day`，本系统自己数的）。

| 测试 | 实际断言 |
| --- | --- |
| `tests/test_claude_code_adapter.py::test_usage_limit_raises_and_never_falls_back` | 撞上限直接抛，绝不回落到 API key 计费 |
| `tests/test_claude_code_adapter.py::test_real_limit_wordings_are_classified` | 真实的几种上限措辞都能识别 |
| `tests/test_claude_code_adapter.py::test_a_partial_answer_that_mentions_limits_is_not_a_limit` | 正文里提到 limit 的长回答不算撞上限（400 字符阈值） |
| `tests/test_state_and_research_jobs.py::test_a_subscription_limit_is_recoverable_and_bounded` | `CLAUDE_LIMIT_REACHED` 的任务等满 `claude_limit_retry_after_hours` 才重新排队，且 `max_limit_recoveries` 用完就不再试 |
| `tests/test_schedule.py::test_the_daily_budget_counts_every_entry_point` | 手动与定时共用一个池子；只剩 1 个额度时当次只研究 1 个标的 |
| `tests/test_schedule.py::test_a_candidate_study_may_not_spend_the_capacity_reserved_for_exits` | `reserve_for_exits` 对候选研究生效，对持仓复查不生效——找买入不能吃掉找卖出的额度 |
| `tests/test_schedule.py::test_an_unusable_system_state_skips_with_the_reason` | `HALTED` / `CLAUDE_LIMIT_REACHED` / `CONFIG_ERROR` 三种状态都跳过并把状态名写进原因 |
| `tests/test_schedule.py::test_every_configured_task_runs_and_the_two_share_one_days_allowance` | 多账户多任务共享同一天的额度，逐个任务重读用量 |
| `tests/test_advisor.py::test_refresh_does_not_spend_research_allowance` | 刷新快照不算研究额度 |
| `tests/test_api.py::test_a_halt_that_needs_a_person_is_not_hidden_behind_a_quota_wait` | 同时存在回撤 halt 与订阅上限时，`status == HALTED`（要人的事优先），`claude.state` 仍带 `LIMIT_REACHED` |
| `tests/test_api.py::test_calls_today_are_counted_by_utc_date_from_the_end_of_the_audit` | 调用计数按 UTC 日期，且只读审计文件的尾部 |
| `tests/test_autonomy.py::test_the_two_budgets_are_separate` | 自主开仓与自主减仓各有各的日额度 |
| `tests/test_execution_scenarios.py::test_interrupted_claude_session_leaves_no_proposal_and_no_order` | T16：研究中途撞上限 ⇒ 提案库空、券商 0 笔 |

**缺口（已补）**：三段各自成立，接缝没测。

1. 任务状态 `CLAUDE_LIMIT_REACHED` → `system.claude_status()` 读 `store.list(limit=1)` → `health().status`
   —— 这一段**没有测试**。`test_an_unusable_system_state_skips_with_the_reason` 是直接把状态字符串传给
   `schedule.decide`，并不证明一个真的撞了上限的任务会让 health 报出这个状态。
2. 更要紧的另一半：**订阅额度耗尽不得挡住卖出**。批准路径根本不读 health（已核实：`approvals.py` 只
   import 了 `SystemStatus` 用于 pause 的返回值，`approve()` 全程不调 `health()`），这是有意的——一个
   到点自动回满的额度，不该成为"持仓卖不掉"的理由。但没有任何测试钉住这件事，而"没人测的意图"正是
   后来某个好心的检查会插进退出与券商之间的地方。

补法见 §9。

**已核实不是缺口**：`analyses_today` 数的是**当天创建的任务**，撞上限而没跑成的那次照样占额度；
`resume_after_limit` 复用同一个 `run_key` 重新排队，不会二次计数。这是有意的设计（订阅是一个池子，
尝试过就是尝试过），由 `test_a_subscription_limit_is_recoverable_and_bounded` 的 run_key 不变间接持有。

---

## 6. 重复任务

现有覆盖。四层去重，每层都有主：券商的 `ref_id`、本系统的 `client_order_id`、提案的
`approval_request_id`、任务队列的 `request_id` / `dedupe_key`。

| 测试 | 实际断言 |
| --- | --- |
| `tests/test_execution_scenarios.py::test_duplicate_submission_creates_exactly_one_order` | T15：同一提案执行两次，第二次 `ALREADY_EXECUTED` + `duplicate_prevented`，券商处 1 笔 |
| `tests/test_execution_scenarios.py::test_client_order_id_is_deterministic_per_attempt` | 同一提案同一 attempt 算出同一个 id |
| `tests/test_execution_scenarios.py::test_broker_refuses_a_duplicate_client_order_id` | 券商按 id 拒重 |
| `tests/test_robinhood.py::test_order_submission_maps_and_prevents_duplicates` | 重试带同一个 `ref_id`（36 位 UUID），券商以此拒绝时抛 `DuplicateOrder` 而不是"拒单" |
| `tests/test_execution_recovery.py::test_concurrent_approval_retries_submit_once` | 两个线程同时批准：两个应答**完全相等**，券商处 1 笔 |
| `tests/test_execution_recovery.py::test_approval_version_terms_identity_and_retries` | 版本、条款、身份、request id 四项任一不符即冲突；重放读回同一份应答 |
| `tests/test_state_and_research_jobs.py::test_identical_active_research_is_deduplicated` | 相同 `dedupe_key` 的活跃研究只有一份 |
| `tests/test_state_and_research_jobs.py::test_research_concurrency_claim_is_atomic` | 并发抢占名额是原子的 |
| `tests/test_continue_exit.py::test_a_repeated_tap_returns_the_same_continuation` | 重复点击返回同一个续做提案 |
| `tests/test_continue_exit.py::test_a_second_tab_with_its_own_id_gets_the_one_that_exists` | 第二个标签页用自己的 id 也拿到已存在的那个 |
| `tests/test_continue_exit.py::test_two_requests_at_once_still_make_one_continuation` | 两线程用 `Barrier` 同时发起，只生成一个续做 |
| `tests/test_continue_exit.py::test_one_request_id_cannot_be_pointed_at_another_exit` | 一个 request id 不能被指向另一笔退出 |
| `tests/test_advisor.py::test_chat_request_is_idempotent_and_conversations_are_serial` | 同 id 同内容返回同一 run；同 id 换内容冲突；同一会话串行 |
| `tests/test_research_records.py::test_a_repeated_request_returns_the_run_it_started_and_a_different_one_conflicts` | 同 id 同请求去重；同 id 换 **ticker** 冲突 |
| `tests/test_schedule.py::test_a_repeat_trigger_inside_an_executed_period_does_not_research_twice` | 同一周期内重复触发不二次研究 |
| `tests/test_evaluation.py::test_marking_the_same_day_twice_does_not_double_count_it` | 同一天标记两次不重复计入 |

**缺口**：只有账户那一项（见 §1 缺口二），已补。

---

## 7. 重启恢复

现有覆盖。

| 测试 | 实际断言 |
| --- | --- |
| `tests/test_execution_recovery.py::test_simulated_venue_recovers_after_broker_and_service_restart` | 券商与服务**都**换新对象，只凭持久化的提案与券商侧订单状态恢复；`submit_order` 被换成 `pytest.fail`；成交流水恰好 1 条 |
| `tests/test_execution_recovery.py::test_crash_between_ledger_and_audit_is_replayed_once` | 写流水失败时账本、提案、流水一起回滚；修好后重放一次，账本有仓、流水 1 条、状态 `EXECUTED` |
| `tests/test_execution_recovery.py::test_service_recovery_does_not_submit` | 恢复流程绝不下新单 |
| `tests/test_execution_recovery.py::test_recovery_still_runs_when_trading_is_paused` | 急停不阻断对账 |
| `tests/test_execution_recovery.py::test_a_fill_read_after_midnight_is_booked_to_the_session_it_happened_in` | 跨午夜读到的成交记在**下单那天**，不落到第二天的开仓额度里 |
| `tests/test_state_and_research_jobs.py::test_orphaned_research_requeues_same_run_id` | worker 没了的 `RUNNING` 任务回到 `QUEUED`，`run_key` 不变，`attempts` +1 |
| `tests/test_state_and_research_jobs.py::test_a_probe_that_cannot_run_never_starts_a_second_worker` | "问不到 worker 还在不在"≠"知道它死了"，不因此起第二个 |
| `tests/test_state_and_research_jobs.py::test_restart_accepts_only_the_matching_decision_record` | 重启后只认对得上的决策记录 |
| `tests/test_schedule.py::test_a_catch_up_after_a_restart_resumes_the_same_period` | 重启后的补跑续上同一个周期，不重研究 |
| `tests/test_continue_exit.py::test_a_restart_does_not_grow_a_second_branch` | 新进程读同一个 store，找到已存在的续做，`deduplicated is True`，链上仍只有一个 |
| `tests/test_security_auth.py::test_session_survives_auth_object_restart` | 会话跨 auth 对象重建存活 |
| `tests/test_backup_restore.py::test_state_keys_survive_a_move_to_another_directory` | 状态键跨目录迁移后仍可读 |
| `tests/test_backup_restore.py::test_restoring_revokes_sessions_and_names_the_orders_to_check` | 恢复后吊销会话并点名需要核对的订单 |
| `tests/test_backup_restore.py::test_the_release_gate_sees_every_order_the_broker_still_owns` | 发布闸门看见券商仍持有的每一笔 |
| `tests/test_autonomy.py::test_the_daily_count_comes_from_the_approvals_themselves` | 自主日额度从批准记录本身数出来——按构造就不怕重启，没有需要另外持久化的计数器 |
| `tests/test_operations.py::test_an_activity_that_never_ran_is_overdue_not_healthy` | 从未跑过的活动不报健康（配合 `docs/ACTIVITY_STATE_CONTRACT_2026-09-19.md` 的四态） |
| `tests/test_kept.py::test_polls_are_answered_from_one_read_while_it_is_young` | 冷启动（内存缓存为空）时第一次请求自己去读，不返回零值 |

**缺口**：无。

---

## 8. 唯一 live 下单入口的证明

规则（`CLAUDE.md`）：`service/approvals.py` 是唯一会下单的 live 路径。原有三条静态断言在
`tests/test_money_paths.py`：

| 测试 | 它证明了什么 |
| --- | --- |
| `test_only_the_approvals_module_may_place_an_order` | 全包 AST 扫描：从 `execution` import `execute` 的模块集合**恰好**等于 `{service/approvals.py}` ∪ `EXEMPT`（`cli.py`、`execution_scenarios.py`） |
| `test_the_cli_exemption_lasts_exactly_as_long_as_the_stage_gate` | `cli.py` 的豁免只在 `execution_policy.stage > 2` 时成立——读的是**本部署的真实 config**，stage 掉回 2 以下这条就红 |
| `test_ct_approve_refuses_before_it_ever_reaches_a_venue` | `ct-approve` 在 `make_broker` 之前就拒，不会先开一个真实会话再被赶走 |
| `test_the_convention_is_still_about_the_function_that_places_the_order` | `approvals.execute_proposal is execution.execute`，且 `execution.py` 里确实有 `submit_order`——防止改名后上面几条空转 |

**发现的漏洞（已补）**：第五条 `test_no_module_in_service_asks_a_venue_to_do_something` 原先只扫
`agentic_trading/service/*.py`。一个在 `service/` 之外的模块——`api/app.py`、`cli.py`、`triggers.py`、
`micro_live.py` 都在某个时刻手里有 broker——直接写 `broker.submit_order(request)`，**两条断言都抓不到**：
它不 import `execution.execute`（第一条扫不到），也不在 `service/` 下（第五条不看）。

已改为全包扫描（`test_no_module_but_execution_asks_a_venue_to_do_something`），允许名单只有
`execution.py`。反向验证做过：临时放一个

```python
def sneaky(broker, request):
    return broker.submit_order(request)
```

到 `agentic_trading/_scan_probe.py`，新写法报

```
only execution.py may ask a venue to place, change or cancel an order, and only
service/approvals.py may ask it to; found ['_scan_probe.py:2 .submit_order()']
```

旧写法照样绿。探针已删除。

**当前实测事实**（2026-09-20，全包 AST 扫描）：`{submit_order, cancel_order, review_order,
pre_submit_check}` 四个方法在整个 `agentic_trading/` 包里只有 `execution.py` 调用（三处：
`submit_order:568`、`pre_submit_check:504`、`review_order:514`）；`broker/` 下的是**定义**不是调用。
`execution.execute` 只被 `service/approvals.py` 与两个已记录理由的豁免模块 import。

结论：**`service/approvals.py` 仍是唯一 live 下单入口**，且这次之后这个结论被两条独立的断言看住——
一条看 import，一条看方法调用。

---

## 9. 本次补的四个测试

都落在已有的域文件里，不另起并行结构；`tests/test_money_paths.py` 是例外，它本来就是持有这条跨切面
规则的地方。

| 测试 | 位置 | 补的是哪个缺口 |
| --- | --- | --- |
| `test_an_unresolved_order_blocks_its_own_venue_and_never_the_other` | `tests/test_execution_recovery.py` | §1 缺口一：`same_venue` 这一项没人测 |
| `test_a_replayed_request_aimed_at_another_account_conflicts` | `tests/test_research_records.py` | §1 缺口二（研究队列侧） |
| `test_a_replayed_assistant_request_aimed_at_another_account_conflicts` | `tests/test_account_scope.py` | §1 缺口二（助手侧） |
| `test_a_subscription_limit_stops_research_and_never_an_exit` | `tests/test_execution_recovery.py` | §5 的两段接缝 |
| （改写）`test_no_module_but_execution_asks_a_venue_to_do_something` | `tests/test_money_paths.py` | §8 的扫描范围漏洞 |

**1. 跨账本的未了结订单**（参数化 core / sandbox / micro_live）

手工造一笔"券商还持有、本系统还说不清"的提案（`status=SUBMITTED` + `client_order_id`）放进 store，
再执行一笔正常的 core 提案：

- 未了结的在 `core` 或 `sandbox` ⇒ `RECONCILIATION_REQUIRED`，券商处 0 笔，错误里点名那笔的 id；
- 未了结的在 `micro_live` ⇒ 正常提交，券商处 1 笔。

`same_venue` 是一个对称表达式，这里从"不需要真实券商"的那一侧验证；实盘那一侧由
`tests/test_robinhood.py::test_end_to_end_micro_live_order_through_the_full_pipeline` 覆盖。

**2 / 3. 跨账户的重放**

同一个 `request_id`：对本账户重放拿回同一个 run 且 `deduplicated is True`；指向另一个账本
（`core` → `micro_live` / `sandbox`，A → B、A → 全部个人账户、个人 → Agentic）一律
`ServiceConflict: ... different request`。并断言**没有**为另一个账户起新任务——队列里仍只有第一个
`run_key`。

**4. 订阅上限不挡卖出**

在一个 config 复制到 tmp 的沙箱里（新加的 `sandboxed()` 辅助函数，理由写在它的 docstring 里：场景用的
profiles 的 `project_dir` 指向 checkout，而研究队列会往 `project_dir/runs` 下建目录）：

- 造一个 `CLAUDE_LIMIT_REACHED` 的研究任务 ⇒ `health().claude.state == "LIMIT_REACHED"` 且
  `health().status == CLAUDE_LIMIT_REACHED`（这一段之前没人测）；
- `schedule.decide(health_status=...)` ⇒ `run is False`，原因里带状态名；
- **同一时刻**批准一笔 SELL ⇒ `submitted is True`，券商处 1 笔，账本里 VTI 的仓位消失；
- 事后 health 仍然是 `CLAUDE_LIMIT_REACHED`。

---

## 10. 未补的缺口，及理由

| 缺口 | 为什么不补 |
| --- | --- |
| "控制台写收紧 → 下一次批准被拒"的端到端 | §2：已被两个测试从两头夹住，第三份是副本，不增加失败模式 |
| `credential_invalid` → 真实设备推送的实机演练 | §4：要故意弄失效实盘券商 token，属于动实盘凭据，测试代替不了，`TODO.md` §11 其一已记在案 |
| 撞上限而没跑成的运行仍占当天研究额度 | §5：这是有意的设计（订阅是一个池子），且 `run_key` 复用使其不会二次计数 |
| `micro_live` 侧的"纸面未了结不挡实盘"直接验证 | §9-1：`same_venue` 是一个对称表达式，另一侧要真实券商会话；实盘管线另有端到端测试 |

---

## 11. 检查记录

2026-09-20，`deploy/checks.sh`：**前端 155 项通过、秘密扫描通过；Ruff 与 Python 套件各有失败，全部来自
另一个 agent 同时在这个 checkout 上做的 P1 thesis-tracker 工作，与本次改动无关**。

- Ruff：`agentic_trading/thesis_trial.py`（I001、TRY004）与 `tests/test_thesis_trial.py`（I001）。
  排除这两个文件后 `ruff check .` 全绿；单独扫本次改的四个测试文件也全绿。
- Python：`tests/test_thesis_trial.py::test_the_runner_uses_the_subscription_adapter_without_tools_and_writes_only_the_requested_trial_dir`，
  以及 `tests/test_env.py::test_every_console_script_loads_it` —— 后者是既有的守卫被新入口踩到：
  `pyproject.toml` 新增的 `ct-thesis-trial = agentic_trading.thesis_trial_cli:main` 的 `main()` 里没有
  `load_env()`。不是本次改动引入的，也没有在本次改动里顺手改掉：那是对方正在写的文件。
- 去掉对方在途的两处后：`pytest -q --ignore=tests/test_thesis_trial.py
  --deselect tests/test_env.py::test_every_console_script_loads_it` ⇒ **973 passed**。

本次改动本身：四个新测试 + 一个扫描范围的改写，全部通过；`git status` 里属于本次的只有
`docs/P2A_ISOLATION_MATRIX_2026-09-19.md` 与四个 `tests/*.py`，未碰 `config/`、`state/`、`runs/`、
`pyproject.toml`，也未碰对方的文件。
