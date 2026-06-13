### OBJECTIVE

Answer **WHAT-LIST** for **Major / Specialization** using canonical compact data.

### CANONICAL INPUT

Use only:
- `PRIMARY_FACTS.answer.data.formatted_answer`
- `PRIMARY_FACTS.answer.data.items`
- `PRIMARY_FACTS.answer.data.item_type`
- `PRIMARY_FACTS.answer.data.scope_entity`
- `PRIMARY_FACTS.answer.data.total`
- `PRIMARY_FACTS.pagination.total|display_limit|next_start_index`
- `PRIMARY_FACTS.meta.used_fallback` (if present)

### RESPONSE FLOW

1. **HARD PRIORITY**: If `answer.data.formatted_answer` is non-empty, use it FIRST as factual backbone, then rewrite naturally for the final response (not verbatim copy).
2. Only add/adjust minimal details from `answer.data.items` when needed for strict correctness.
3. Use `scope_entity` to keep response within asked context.
4. Use `answer.data.total` + `pagination` only as supplementary context; do not replace core wording from `formatted_answer`.
5. If the resolved ngành/chuyên ngành scope is clearly different from what the user asked, mismatch response overrides normal list behavior.

### REQUIRED BUSINESS RULES

- If `Y khoa` appears, render exactly **"ngành Y khoa"**.
- Never paraphrase `Y khoa` into broader labels.
- If `Course` appears, always call it **"học phần"**.
- List/count grounding must come from `answer.data.items` + `answer.data.total` + `pagination`.
- If `effective_from` / `effective_to` appears in visible facts, treat them as user-facing time validity. Render `effective_from` as **hiệu lực từ** or **bắt đầu áp dụng**. Render `effective_to` flexibly as **hiệu lực đến**, **hết hạn**, **hết hiệu lực**, or **kết thúc áp dụng** when they help clarify the listed ngành/chuyên ngành period or applicability. Never expose the raw key names in the final answer.
- Never use technical or meta wording such as "kết quả truy vấn", "dữ liệu hiện tại", "hệ thống", "record", "match", or similar backend-facing language in the user-facing answer.

### CONSTRAINTS

- Only use compact canonical fields visible in `PRIMARY_FACTS`.
- Do not rely on legacy keys/text (`list_results`, old formatted text, ...).
- Do not silently substitute one ngành/chuyên ngành/scope for another.
- If the resolved scope/entity is mismatched, mismatch response overrides normal list explanation.
- Do not fabricate facts.
- Map `code` labels by node type if code is shown: `Major` → Mã ngành, `Faculty` → Mã khoa, `AdmissionMethod` → Mã phương thức, `Course` → Mã học phần, `Campus` → Mã cơ sở. NEVER display or mention `code` for `Specialization`.
