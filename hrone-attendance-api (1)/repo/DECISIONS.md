# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique (emp_code, date) guarantees one record per day and serves per-employee queries; (date desc, emp_code) serves the list sort and month scans; (status, date, emp_code) serves status filters; (emp_code, punch_in desc) finds the record to punch out. `employees`: unique emp_code, and (department, joined_on). Rejected: a lone emp_code index on logs, since the unique one already covers it as a prefix.

2. **Punch-in race.** Both requests try `insert_one`. The unique index lets MongoDB accept exactly one; the other raises `DuplicateKeyError`, which I turn into 409. There is no read before the write, so nothing can slip between them.

3. **Ties.** I rank with `$rank` (1, 2, 2, 4), then keep rows with rank <= limit. Everyone tied at the cutoff is returned, so the list can be longer than `limit`. Rows are ordered by total, then emp_code.

4. **Headcount.** The pipeline starts from `employees`, not from the logs. `$lookup` plus `$unwind` with `preserveNullAndEmptyArrays` keeps employees with no logs, and each employee is counted once.

5. **100x the data.** I would stop scanning raw logs for analytics and keep a small per-employee, per-month summary collection, updated whenever a record is written.
