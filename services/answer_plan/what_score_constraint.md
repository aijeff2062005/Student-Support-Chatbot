### OBJECTIVE

Answer **WHAT-SCORE-CONSTRAINT-LIST** questions for admission score/quota filtered lists.

### CANONICAL INPUT

- `PRIMARY_FACTS.intent = "constraint-list"`
- `PRIMARY_FACTS.answer.kind = "collection"`
- Use only:
  - `PRIMARY_FACTS.answer.data.formatted_answer`
  - `PRIMARY_FACTS.answer.data.items`
  - `PRIMARY_FACTS.answer.data.total`
  - `PRIMARY_FACTS.answer.data.current_primary_entity`
  - `PRIMARY_FACTS.answer.data.matched_condition`
  - `PRIMARY_FACTS.meta.used_fallback` when present

### RESPONSE FLOW

1. If `answer.data.formatted_answer` is non-empty, use it as the factual backbone and rewrite naturally without adding unsupported rows.
2. For cutoff-score or quota fallback, keep the fallback wording compact. Only say a year is fallback/reference when `formatted_answer` explicitly contains `current_year` and `fallback_year`.
3. If the user directly asks for the same year shown in the rows, answer as that year's score/quota data. Do not add "dữ liệu quá khứ" or "chưa có dữ liệu năm khác".
4. If `formatted_answer` is absent, build the answer from `items`, `matched_condition`, and `total`.
5. If the resolved scope/entity is clearly different from what the user asked, stop after a short mismatch notice.

### CONSTRAINTS

- Do not add majors, methods, scores, or quotas that are not in `formatted_answer` or `items`.
- For score/quota lists, show only the matched score/quota attributes relevant to the user's condition.
- If `effective_from` / `effective_to` appears, render them as user-facing validity time, never raw key names.
- Do not expose backend terms such as "record", "match", "query result", or "hệ thống".
- If result confirms **Y khoa**, render exactly **"ngành Y khoa"**.
