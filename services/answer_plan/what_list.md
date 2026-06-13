### OBJECTIVE

Answer **WHAT-LIST** using canonical compact data that LLM sees in `PRIMARY_FACTS`.

### CANONICAL INPUT

- `PRIMARY_FACTS.intent = "list"`
- `PRIMARY_FACTS.answer.kind = "collection"`
- Use only:
  - `PRIMARY_FACTS.answer.data.formatted_answer`
  - `PRIMARY_FACTS.answer.data.items`
  - `PRIMARY_FACTS.answer.data.item_type`
  - `PRIMARY_FACTS.answer.data.scope_entity`
  - `PRIMARY_FACTS.answer.data.total`
  - `PRIMARY_FACTS.pagination.total|display_limit|next_start_index`
  - `PRIMARY_FACTS.meta.used_fallback` (if present)
  - `PRIMARY_FACTS.evidence.fallback` (if present)

### RESPONSE FLOW

1. **HARD PRIORITY**: If `answer.data.formatted_answer` is non-empty, use it FIRST as factual backbone, then rewrite naturally for the final response (not verbatim copy).
2. Only add/adjust minimal details from `answer.data` when needed for strict correctness (scope, item naming, pagination note).
3. Keep any adjustment consistent with `answer.data.items` (returned order) and `scope_entity`.
4. Use `answer.data.total` + `pagination` only as supplementary context; do not replace the core wording from `formatted_answer`.
5. If `meta.used_fallback = true`, keep fallback-supported wording.
6. If the resolved scope/entity is clearly different from what the user asked, mismatch response overrides `formatted_answer` and all normal list behavior.

### BUSINESS RULES

- If item/value is **Y khoa**, always render exactly **"ngành Y khoa"**.
- If entity type is `Course`, always use term **"học phần"**.
- Never use technical or meta wording such as "kết quả truy vấn", "dữ liệu hiện tại", "hệ thống", "record", "match", or similar backend-facing language in the user-facing answer.
- In a retrieval mismatch case, stop after the short mismatch notice. Do not continue with list bullets for the wrong scope/entity by default.
- If `effective_from` / `effective_to` appears in visible facts, treat them as user-facing time validity. Render `effective_from` as **hiệu lực từ** or **bắt đầu áp dụng**. Render `effective_to` flexibly as **hiệu lực đến**, **hết hạn**, **hết hiệu lực**, or **kết thúc áp dụng** when they help clarify the listed period or applicability. Never expose the raw key names in the final answer.

### CONSTRAINTS

- Only use compact canonical fields visible in `PRIMARY_FACTS`.
- Do not rely on legacy keys (`list_results`, `found_entity_list`, ...).
- Do not silently substitute one target scope/entity for another.
- If the resolved scope/entity is mismatched, mismatch response overrides normal list explanation.
- Do not fabricate facts.
- If data is insufficient/mismatched, answer briefly with what is verifiable and ask user to clarify target scope/filter.
- Map `code` labels by node type if code is shown: `Major` → Mã ngành, `Faculty` → Mã khoa, `AdmissionMethod` → Mã phương thức, `Course` → Mã học phần, `Campus` → Mã cơ sở. NEVER display or mention `code` for `Specialization`.
