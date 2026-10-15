# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | MongoDB setup | Connection details had hard-coded fallbacks, contrary to the environment contract. | Start without `MONGO_URI` or `MONGO_DB`; the app silently uses an unintended local database. | Require both settings, while keeping `load_dotenv()` for local development. |
| 2 | MongoDB setup | PyMongo returned naive datetimes by default. | A stored UTC date can be treated as local time during IST calculations. | Created the client with `tz_aware=True` and normalized instants to UTC. |
| 3 | Application startup | No indexes were created. Uniqueness and query performance were not guaranteed. | Parallel creates can duplicate records; list queries scan/sort large collections. | Added idempotent startup indexes, including unique natural-key indexes. |
| 4 | `health` | It returned 200 even when MongoDB was unavailable. | Stop MongoDB and `/health` still says `ok`. | Ping MongoDB and return 503 when the ping fails. |
| 5 | `compute_late_minutes` | It compared truncated minutes to 10, so 09:40:01 for a 09:30 shift incorrectly returned 0. | Test 09:40:01; R2 requires 10 late minutes. | Compare elapsed seconds to 600, then floor total elapsed minutes. |
| 6 | `compute_late_minutes` | It built the shift start by replacing fields on a possibly naive/local datetime. | UTC input or an overnight attendance day gives the wrong boundary. | Build an aware IST boundary from the attendance date and shift time. |
| 7 | `compute_work_hours` | Python `round(float, 2)` uses binary floats and half-even behavior, not R4 half-up. | A value exactly at a half-centesimal-hour boundary can round the wrong way. | Use `Decimal.quantize(..., ROUND_HALF_UP)`. |
| 8 | `compute_overtime` | Any positive overtime was counted; R3 has a 30-minute minimum. | A punch-out 5 minutes after shift end returned 5 instead of 0. | Check elapsed seconds against 30 minutes before flooring. |
| 9 | `compute_overtime` | Shift end was always placed on the attendance date. | A 22:00-06:00 shift treats the 06:00 end as occurring before punch-in. | Put the end boundary on the next day for overnight shifts. |
| 10 | `EmployeeIn` | Employee fields had none of the contract's patterns or length validation. | Invalid codes, emails, dates and shift strings were accepted. | Added Pydantic field constraints and calendar-date validation. |
| 11 | `EmployeeIn` | Equal shift start and end values were accepted. | Submit `09:30` for both values; the contract requires 422. | Added a model-level validation rule. |
| 12 | `create_employee` | Check-then-insert was race-prone. | Two parallel requests could both see no employee and both insert. | Rely on a unique database index and translate `DuplicateKeyError` to 409. |
| 13 | `create_employee` | `created_at` used a naive server-local datetime and was returned as an ISO string. | The response did not contain epoch milliseconds and varied by server timezone. | Store an aware UTC value truncated to seconds and serialize it to epoch milliseconds. |
| 14 | `list_employees` | `skip = page * page_size` skipped the first page. | Page 1 started with item 21 at the default size. | Use `(page - 1) * page_size`. |
| 15 | `list_employees` | `total` counted the whole collection instead of the department filter. | A department page reported the company-wide count. | Pass the applied filter to `count_documents`. |
| 16 | `list_employees` | Results were not sorted by `emp_code`. | Insertion order leaked into the response. | Sort by `emp_code` ascending in MongoDB. |
| 17 | `list_employees` | Page numbers and sizes were unconstrained. | Page 0, negative sizes or more than 100 items were accepted. | Added FastAPI query constraints from R10. |
| 18 | `punch_in` | An unknown employee was dereferenced as `None`. | An unknown `emp_code` caused a 500 at `emp["shift_start"]`. | Return 404 before using the employee document. |
| 19 | `PunchInIn` | Timestamps accepted the wrong type and had no epoch-millisecond range. | Seconds-looking values, numeric strings, floats or explicit null could be accepted. | Added a strict, non-null integer type with the OpenAPI minimum and maximum; omission still uses the server clock. |
| 20 | `punch_in` | `fromtimestamp()` and `now()` used the machine's local timezone. | The same request produced a different date on a non-IST server. | Parse instants in UTC and convert to IST only for business boundaries. |
| 21 | `punch_in` | Fractional seconds were neither truncated before calculations nor before storage. | `09:40:00.900` could be treated as late. | Normalize every incoming or server-generated instant to whole seconds first. |
| 22 | `punch_in` | Attendance date was simply `ts.date()`, ignoring IST and overnight R1. | A 05:59 IST punch for a 22:00-06:00 shift went onto the wrong day. | Added an IST attendance-date helper with the overnight exception. |
| 23 | `PunchInIn` | `status` allowed arbitrary strings, including `ABSENT` and `LEAVE`. | Punch-in could create a non-presence record with a punch time. | Restricted it to `PRESENT`, `WFH`, and `ON_DUTY`. |
| 24 | `punch_in` | Duplicate detection used check-then-insert and was not concurrency-safe. | Simultaneous punches could both return 201. | Insert directly under the unique `(emp_code, date)` index and map the loser to 409. |
| 25 | `punch_in` and attendance responses | The API added an `id` based on MongoDB `_id`, although attendance records have no API id. | Successful responses contained an undocumented `id`. | Serialize only the contract's public fields. |
| 26 | `list_attendance` | It loaded every matching document, then sorted and paginated in Python. | Memory and response time grew with the entire collection. | Apply sort, skip and limit in MongoDB and count separately. |
| 27 | `list_attendance` | Sorting used only `date`, omitting the required `emp_code` tie-breaker. | Employees on the same date appeared in unstable order. | Sort by `date` descending, then `emp_code` ascending. |
| 28 | `list_attendance` | Dates, status, page bounds and reversed ranges were not validated. | A datetime string, invalid status, or `date_from > date_to` did not return 422. | Added strict `YYYY-MM-DD` parsing, enums, bounds and a range-order check. |
| 29 | API serialization | BSON datetimes were left for FastAPI to emit as ISO strings. | `created_at`, punches and history instants violated the epoch-millisecond convention. | Added explicit recursive API-boundary conversion. |
| 30 | `list_attendance` | Legacy documents missing `history` or `half_day` returned incomplete response objects. | The provided legacy sample lacked required response fields. | Default missing `history` to `[]` and `half_day` to `false` while serializing. |

## Examined and kept

- `load_dotenv()` was kept: its default `override=False` means real environment variables still take priority.
- Calendar dates and shift times remain strings in MongoDB because that is intentional in `DATA_MODEL.md`.
- Stored derived attendance fields remain authoritative when records are listed; they are not recomputed for reads.
- The starter's initial values for an open presence record (`punch_out`/`work_hours` null, overtime 0, `half_day` false, empty history) already matched the data model.

