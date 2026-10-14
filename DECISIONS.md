# DECISIONS.md

1. **Indexes.** I create a unique `employees.emp_code` index so duplicate employee codes cannot race, and a unique `(emp_code, date)` attendance index so only one attendance record exists per employee per attendance day. The date/employee index supports the attendance list's sort and date range. The employee/date/status index helps employee analytics. The department/join-date index supports department filtering, while a separate `joined_on` index supports the department summary when no department filter is supplied. I would avoid a large compound index for every possible filter because it increases write cost and storage without helping every query.

2. **Punch-in race.** Both requests may pass application-level work, but MongoDB enforces the unique `(emp_code, date)` index. One insert wins; the other receives `DuplicateKeyError`, which the API translates to 409. I do not depend on a preliminary read to guarantee uniqueness.

3. **Ties.** Ranking uses MongoDB's competition `$rank`; if rank 10 is shared by three employees, all three are returned for `limit=10`. Tied rows are then ordered by employee code.

4. **Headcount.** Department summary starts from employees, filters by `joined_on <= month end`, then looks up that employee's logs. Employees with no logs remain in the aggregation and count toward headcount.

5. **100x data.** I would measure query plans and latency, consider pre-aggregated daily summaries, and review compound indexes against real filter patterns before adding infrastructure.
