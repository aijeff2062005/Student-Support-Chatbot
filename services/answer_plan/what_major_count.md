### OBJECTIVE

Compatibility-safe legacy plan for **WHAT-COUNT Major/Specialization**.

### CONTRACT ALIGNMENT

Treat this file as legacy alias of `what_count_major` and use canonical compact fields only.

Use only:
- `PRIMARY_FACTS.answer.data.formatted_answer`
- `PRIMARY_FACTS.answer.data.items`
- `PRIMARY_FACTS.answer.data.item_type`
- `PRIMARY_FACTS.answer.data.scope_entity`
- `PRIMARY_FACTS.answer.data.total`
- `PRIMARY_FACTS.answer.data.count_mode`
- `PRIMARY_FACTS.pagination.total|display_limit|next_start_index`
- `PRIMARY_FACTS.meta.used_fallback` (if present)

### REQUIRED BUSINESS RULES

- If scope/result confirms Y khoa, render exactly **"ngành Y khoa"**.
- Use **"học phần"** for `Course`.
- Ground count in `items + total + pagination`.
- If `effective_from` / `effective_to` appears in visible facts, treat them as user-facing time validity. Render `effective_from` as **hiệu lực từ** or **bắt đầu áp dụng**. Render `effective_to` flexibly as **hiệu lực đến**, **hết hạn**, **hết hiệu lực**, or **kết thúc áp dụng** when they qualify the counted period or scope. Never expose the raw key names in the final answer.

### RESPONSE FLOW

1. **HARD PRIORITY**: If `answer.data.formatted_answer` is non-empty, use it FIRST as factual backbone, then rewrite naturally for the final response (not verbatim copy).
2. Only add/adjust minimal details from `answer.data.items` with total/count_mode/pagination when needed for strict correctness.
3. If the resolved ngành/chuyên ngành scope is clearly different from what the user asked, mismatch response overrides normal count behavior.

### CONSTRAINTS

- Do not rely on legacy keys/text (`list_results`, ...).
- Do not silently substitute one ngành/chuyên ngành/scope for another.
- Never use technical or meta wording such as "kết quả truy vấn", "dữ liệu hiện tại", "hệ thống", "record", "match", or similar backend-facing language in the user-facing answer.
- Do not fabricate facts.
- Map `code` labels by node type if code is shown: `Major` → Mã ngành, `Faculty` → Mã khoa, `AdmissionMethod` → Mã phương thức, `Course` → Mã học phần, `Campus` → Mã cơ sở. NEVER display or mention `code` for `Specialization`.
