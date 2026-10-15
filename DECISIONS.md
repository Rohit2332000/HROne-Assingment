
# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

   I created a unique index on employees.emp_code to prevent duplicate employee codes, along with indexes on department and joining date for employee filtering and headcount queries. In `attendance_logs`, I created a unique compound index on `(emp_code, date)` to guarantee one attendance record per employee per day. Additional indexes support date-based sorting, employee attendance history, status filtering, and punch-in lookups. I considered a separate index on `status` alone but rejected it because the compound status-date index better supports the required query patterns.
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

If two identical punch-in requests arrive simultaneously, both may initially find no existing record. However, the unique `(emp_code, date)` index allows only one insert to succeed. The other insert encounters a duplicate-key error and returns a conflict response, preventing duplicate attendance records.

1. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

The late-arrival leaderboard uses competition ranking. If employees tie at the limit, all employees sharing that rank are returned, even if the result exceeds the requested limit. This avoids arbitrarily excluding employees with equal late minutes.

1. **Headcount.** How does your department summary make sure employees with zero logs are counted?

The department summary starts with employees who joined on or before the month's end, then combines attendance aggregates. This ensures employees without attendance logs are still included in headcount.

1. **One thing you would change** if this had to serve 100x the data.

For 100 times more data, I would review query execution statistics, optimize aggregation pipelines, and introduce pagination or caching where appropriate. I would also evaluate MongoDB sharding if a single database became a bottleneck.
