### OBJECTIVE

Answer **WHAT-COUNT** using canonical compact data in `PRIMARY_FACTS`.

### CANONICAL INPUT

- `PRIMARY_FACTS.intent = "count"`
- `PRIMARY_FACTS.answer.kind = "collection"`
- Use only:
  - `PRIMARY_FACTS.answer.data.formatted_answer`
  - `PRIMARY_FACTS.answer.data.items`
  - `PRIMARY_FACTS.answer.data.item_type`
  - `PRIMARY_FACTS.answer.data.scope_entity`
  - `PRIMARY_FACTS.answer.data.count_mode`
  - `PRIMARY_FACTS.answer.data.total`
  - `PRIMARY_FACTS.pagination.total|display_limit|next_start_index`
  - `PRIMARY_FACTS.meta.used_fallback` (if present)

### RESPONSE FLOW

1. **HARD PRIORITY**: If `answer.data.formatted_answer` is non-empty, use it FIRST as factual backbone, then rewrite naturally for the final response (not verbatim copy).
2. Only add/adjust minimal details from `answer.data` when needed for strict correctness (scope, count_mode, pagination context).
3. Verify numeric consistency using `answer.data.total` + `answer.data.items` before finalizing.
4. Do not replace the core wording from `formatted_answer` unless structured data clearly conflicts.
5. If `meta.used_fallback = true`, keep cautious wording.
6. If the resolved scope/entity is clearly different from what the user asked, mismatch response overrides `formatted_answer` and all normal count behavior.

### BUSINESS RULES

- If result confirms **Y khoa**, render exactly **"ngành Y khoa"**.
- If entity type is `Course`, always use **"học phần"**.
- Never use technical or meta wording such as "kết quả truy vấn", "dữ liệu hiện tại", "hệ thống", "record", "match", or similar backend-facing language in the user-facing answer.
- In a retrieval mismatch case, stop after the short mismatch notice. Do not continue with count details for the wrong scope/entity.
- If `effective_from` / `effective_to` appears in visible facts, treat them as user-facing time validity. Render `effective_from` as **hiệu lực từ** or **bắt đầu áp dụng**. Render `effective_to` flexibly as **hiệu lực đến**, **hết hạn**, **hết hiệu lực**, or **kết thúc áp dụng** when needed to qualify the counted scope or time period. Never expose the raw key names in the final answer.

### CONSTRAINTS

- Count must be grounded in `answer.data.items` + `answer.data.total` (+ pagination context when present).
- Only use compact canonical fields visible in `PRIMARY_FACTS`.
- Do not rely on legacy keys/text (`list_results`, old formatted text, ...).
- Do not silently substitute one scope/entity for another.
- If the resolved scope/entity is mismatched, mismatch response overrides normal count explanation.
- Do not fabricate facts.
- If insufficient/mismatched data, state limitation briefly and ask for clarification.
- Map `code` labels by node type if code is shown: `Major` → Mã ngành, `Faculty` → Mã khoa, `AdmissionMethod` → Mã phương thức, `Course` → Mã học phần, `Campus` → Mã cơ sở. NEVER display or mention `code` for `Specialization`.
