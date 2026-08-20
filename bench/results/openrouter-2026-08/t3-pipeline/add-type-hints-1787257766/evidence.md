# LazyCode pipeline evidence — `add-type-hints` over OpenRouter batch

- model: `anthropic/claude-haiku-4.5` · provider: `openrouter` (`/api/beta/batches`, `/v1/messages` skin)
- job status: **NEEDS_HUMAN** · waves: 0 · wall clock: 16.17s
- tokens (from `llm_calls`): 1065 in / 1025 out across 3 calls
- receipts: none found
- key-usage delta for the whole run (planner + waves): $0.028614

## The conversion, in the store's own events

| # | event | detail |
|---|---|---|
| 0 | JOB_CREATED |  |
| 1 | NODE_ADDED | node_id=explore_billing_structure |
| 2 | NODE_ADDED | node_id=explore_imports |
| 3 | NODE_ADDED | node_id=explore_usage |
| 4 | NODE_ADDED | node_id=add_hints_invoice |
| 5 | NODE_ADDED | node_id=add_hints_ledger |
| 6 | NODE_ADDED | node_id=add_hints_refunds |
| 7 | NODE_ADDED | node_id=verify_hints |
| 8 | PLAN_PROPOSED |  |
| 9 | PLAN_APPROVED |  |
| 10 | LEASE_ACQUIRED |  |
| 11 | LEASE_RENEWED |  |
| 12 | NODE_READY | node_id=explore_billing_structure |
| 13 | NODE_STATE_CHANGED | node_id=explore_billing_structure |
| 14 | NODE_DONE | node_id=explore_billing_structure |
| 15 | NODE_READY | node_id=explore_imports |
| 16 | NODE_STATE_CHANGED | node_id=explore_imports |
| 17 | NODE_DONE | node_id=explore_imports |
| 18 | NODE_READY | node_id=explore_usage |
| 19 | NODE_STATE_CHANGED | node_id=explore_usage |
| 20 | NODE_DONE | node_id=explore_usage |
| 21 | LEASE_RENEWED |  |
| 22 | NODE_READY | node_id=add_hints_invoice |
| 23 | NODE_READY | node_id=add_hints_ledger |
| 24 | NODE_READY | node_id=add_hints_refunds |
| 25 | WAVE_FORMED | wave_id=f47da32a-0, idempotency_key=f47da32af1ef53e9:0 |
| 26 | NODE_HARVESTED | node_id=add_hints_invoice |
| 27 | NODE_HARVESTED | node_id=add_hints_ledger |
| 28 | NODE_HARVESTED | node_id=add_hints_refunds |
| 29 | WAVE_SUBMITTED | wave_id=f47da32a-0, idempotency_key=f47da32af1ef53e9:0, item_count=3 |
| 30 | LEASE_ACQUIRED |  |
| 31 | LEASE_RENEWED |  |
| 32 | ITEM_RETURNED | wave_id=f47da32a-0, status=completed |
| 33 | CONTRACT_RESULT | node_id=add_hints_invoice, passed=True |
| 34 | ARTIFACT_APPLY_INTENT | node_id=add_hints_invoice |
| 35 | NODE_NEEDS_HUMAN | node_id=add_hints_invoice |
| 36 | ITEM_RETURNED | wave_id=f47da32a-0, status=completed |
| 37 | CONTRACT_RESULT | node_id=add_hints_ledger, passed=True |
| 38 | ARTIFACT_APPLY_INTENT | node_id=add_hints_ledger |
| 39 | NODE_NEEDS_HUMAN | node_id=add_hints_ledger |
| 40 | ITEM_RETURNED | wave_id=f47da32a-0, status=completed |
| 41 | CONTRACT_RESULT | node_id=add_hints_refunds, passed=True |
| 42 | ARTIFACT_APPLY_INTENT | node_id=add_hints_refunds |
| 43 | NODE_NEEDS_HUMAN | node_id=add_hints_refunds |
| 44 | WAVE_COMPLETED | wave_id=f47da32a-0 |
| 45 | LEASE_RENEWED |  |

## Waves (plan → provider batches)

```json
[
 {
  "id": "f47da32a-0",
  "batch_id": null,
  "idempotency_key": "f47da32af1ef53e9:0",
  "status": "COMPLETED",
  "provider": "openrouter"
 }
]
```

## Applied diffs

0 diff(s) applied to the repo after verify.