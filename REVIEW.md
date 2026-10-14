# REVIEW.md

| # | Where | What is wrong | How to notice | How fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Compares naive/ambiguous local timestamps and computes from the wrong date for overnight shifts. | Punch in after midnight on a 22:00–06:00 shift. | Normalize instants to UTC, compare in IST, and derive attendance date using shift boundaries. |
| 2 | `compute_late_minutes` | Grace period is implemented as `minutes > 10`, losing the strict-seconds edge (09:40:01 should be 10 late minutes). | Test 09:40:00 and 09:40:01. | Compare elapsed seconds to 600, then floor elapsed minutes. |
| 3 | `compute_work_hours` | Python's ordinary `round` uses bankers' rounding. | Duration whose hundredths are exactly halfway. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Always treats shift end as the same date and counts any positive minute. | Overnight shift or 10 minutes after shift end. | Put overnight shift end on the following day and require at least 30 whole minutes. |
| 5 | `/health` | Does not ping MongoDB, so a disconnected database still reports healthy. | Stop MongoDB and call health. | Issue `admin.command('ping')`, return 503 on failure. |
| 6 | `create_employee` | Check-then-insert is racy and `created_at` is naive. | Parallel duplicate create requests. | Unique index catches races; map duplicate-key to 409; use UTC-aware server time. |
| 7 | `create_employee` / request model | Does not validate employee code, email, shift formats, unequal shift boundaries, or joined date. | Submit malformed values. | Pydantic constraints and validators. |
| 8 | `list_employees` | `skip=page * page_size` skips page one; total ignores department filter; no stable sort. | Page 1 misses first records; filter a department. | Use `(page-1)*page_size`, filtered count, and `emp_code` ascending sort. |
| 9 | `punch_in` | Unknown employee causes a 500; status isn't constrained; timestamp is interpreted in server-local timezone; missing employee check is absent. | Unknown code, invalid status, or UTC server. | Return 404, validate presence statuses and epoch milliseconds, normalize to UTC. |
| 10 | `punch_in` | Check-then-insert permits duplicate attendance rows under concurrency; no unique natural-key guarantee. | Send parallel same-day punches. | Unique `(emp_code,date)` index and catch duplicate-key as 409. |
| 11 | `punch_in` | Attendance date ignores IST and overnight shifts; timestamps are not truncated to seconds. | Punch shortly after midnight on an overnight shift or pass milliseconds. | Calculate R1 attendance date and truncate/store UTC seconds. |
| 12 | `/attendance` | Loads every matching record into Python, which is slow and memory-heavy at 100k rows. | Benchmark a broad query. | Count and paginate in MongoDB with indexed filter/sort. |
| 13 | `/attendance` | Date range is not validated and sort lacks `emp_code` tie-breaker. | Invalid/reversed dates and multiple employees on one date. | Validate inclusive dates and sort date descending, employee code ascending. |
| 14 | All responses | Raw BSON datetimes/ObjectIds leak or epoch-millisecond fields are missing/inconsistent. | Inspect response types and history payload. | Strip `_id` and convert all API instants, including nested history changes, to milliseconds. |
| 15 | Punch-out (missing) | No implementation; no atomic update to prevent two punch-outs succeeding. | Call endpoint or send concurrent requests. | Select latest eligible punch-in, validate interval, update conditionally on `punch_out: null`. |
| 16 | Regularization (missing) | No validation/recalculation/audit append or concurrency protection. | Correct a punch and inspect history; race two corrections. | Validate final state, recompute derived values, record actual diffs and compare-and-set history. |
| 17 | Analytics (missing) | Required analytics endpoints absent. | Call each analytics route. | Add MongoDB aggregation pipelines for employee/month, department, leaderboard and trend. |
| 18 | Index setup (missing) | No startup indexes to support uniqueness and list/analytics access. | Inspect indexes or run explain. | Create idempotent unique and query-supporting indexes on startup. |

## Examined and not considered defects

- Using `emp_code` rather than MongoDB `_id` is intentional and follows the contract.
- Keeping calendar dates and shift times as strings is intentional; they are not instants.
- A missing attendance document is not automatically `ABSENT`; only an explicit `ABSENT` record means absent.
- Punch-in/out are original events and should not create audit-history entries; history is for manual corrections only.
