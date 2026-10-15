
# REVIEW.md

## 1. Employee creation serialized dates incorrectly

- **Where:** `create_employee`
- **Problem:** `model_dump(mode="json")` serializes `joined_on` as a string, so calling `.isoformat()` on it raises `AttributeError`.
- **Notice:** Create an employee with `joined_on: "2026-01-01"`.
- **Fix:** Use `model_dump()` and serialize the date explicitly.

## 2. Month boundaries could produce server errors

- **Where:** `month_bounds`
- **Problem:** Valid-looking boundary inputs such as `9999-12` can cause an uncaught `ValueError` while calculating the next month.
- **Notice:** Request monthly analytics for `month=9999-12`.
- **Fix:** Catch calendar-boundary errors and return HTTP 422.

## 3. Punch-out rejected an earlier timestamp incorrectly

- **Where:** `punch_out`
- **Problem:** Searching only for `punch_in <= punched_at` hides a later punch-in and incorrectly returns 404.
- **Notice:** Punch in at 09:30 and punch out at 09:00 on the same date.
- **Fix:** Find the relevant open record first, then validate the timestamp and return 422.

## 4. Regularization mishandled records without history

- **Where:** `regularize`
- **Problem:** `old.setdefault("history", [])` runs before checking whether history existed, making the subsequent existence check ineffective.
- **Notice:** Regularize a legacy attendance record without a `history` field.
- **Fix:** Preserve the original field-presence state when building the compare-and-swap selector.

## 5. Trend attendance rates used the wrong scale

- **Where:** `build_trend_pipeline`
- **Problem:** The calculation returns a fraction such as `0.8` instead of a percentage such as `80.0`.
- **Notice:** A department has 8 present employees out of 10.
- **Fix:** Multiply the attendance ratio by 100 before rounding to four decimal places.

## 6. Whitespace-only employee fields were accepted

- **Where:** `EmployeeCreate`
- **Problem:** Minimum-length validation accepts strings containing only spaces.
- **Notice:** Submit `"name": "   "` or `"department": "   "`.
- **Fix:** Strip surrounding whitespace and reject empty results.

## 7. Audit reasons could contain only spaces

- **Where:** `RegularizeRequest`
- **Problem:** A minimum string length does not guarantee a meaningful audit reason.
- **Notice:** Submit `"reason": "     "`.
- **Fix:** Validate the stripped reason and reject blank values.

## 8. Unknown request fields were silently ignored

- **Where:** Request models
- **Problem:** `extra="ignore"` can hide misspelled optional fields and make requests appear successful.
- **Notice:** Send `shift_strat` instead of `shift_start`.
- **Fix:** Use `extra="forbid"` where the API contract requires rejecting unsupported fields.

## 9. Pagination totals can change during concurrent writes

- **Where:** `page_result`
- **Problem:** `count_documents()` and the subsequent page query are separate operations, so concurrent writes can make `total` inconsistent with the returned page.
- **Notice:** Insert or delete records while repeatedly requesting the same page.
- **Fix:** Use snapshot-consistent reads when a consistent total is required; otherwise document the behavior.

## 10. Punch-out selection needed to distinguish closed and open records

- **Where:** `punch_out`
- **Problem:** Selecting a record without explicitly considering its open/closed state can return a conflict even when another eligible open record exists.
- **Notice:** Create multiple attendance records for one employee, including an already-closed record and an older open record.
- **Fix:** Select the correct open record and retain a conditional update to prevent concurrent double punch-outs.

## 11. Explain output could misidentify the collection

- **Where:** `explain_endpoint`, `department_trend`
- **Problem:** The trend pipeline starts with `$documents` and runs as a database-level aggregation, so labeling it as an attendance collection query is misleading.
- **Notice:** Inspect the `department_trend` explain response.
- **Fix:** Report the database-level aggregation accurately and inspect the plans for its `$lookup` stages.

## 12. Reviewed and retained

- **Where:** `create_indexes`, `punch_in`, `regularize`, and leaderboard pipeline
- **Finding:** The unique employee-code and employee/date indexes protect uniqueness. Punch-in handles duplicate-key errors, regularization uses compare-and-swap to avoid lost updates, and competition ranking preserves ties at the limit.
- **Decision:** Retained these safeguards and the deterministic employee-code ordering for tied leaderboard results.
