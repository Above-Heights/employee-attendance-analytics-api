# Design decisions

1. **Indexes.** Unique indexes protect employee codes and `(emp_code, date)` attendance records. Compound indexes support listing, analytics, and punch-out queries. I skipped a separate employee-code index because the unique one already supports that lookup.

2. **Two punch-ins at once.** Both requests can read the employee and calculate the same IST date. A prior check cannot prevent this race. Both try to insert; MongoDB's unique index accepts one and rejects the other. The API returns 201 and 409 respectively.

3. **Ranking ties.** I use `$rank` on total late minutes. Equal totals share a rank, such as 1, 2, 2, 4. The limit filters ranks, not rows, so everyone tied at the cutoff appears. Employee code makes their display order stable.

4. **Zero-log headcount.** The department summary starts with employees who joined by month-end and looks up their logs. Someone with no logs still adds one to headcount, but zero to attendance totals.

5. **At 100 times the data.** I would inspect slow queries with `explain` and consider monthly summaries refreshed after punches or corrections. Original records would remain for audits.
