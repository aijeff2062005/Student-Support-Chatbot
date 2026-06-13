### OBJECTIVE

Answer **WHAT-MAJOR-CONSTRAINT-LIST** questions by listing majors and specializations that satisfy academic attributes, properties, values, or semantic conditions.

### CANONICAL INPUT

- `PRIMARY_FACTS.intent = "constraint-list"`
- `PRIMARY_FACTS.answer.kind = "collection"`
- Use only:
  - `PRIMARY_FACTS.answer.data.items`
  - `PRIMARY_FACTS.answer.data.total`
  - `PRIMARY_FACTS.answer.data.selection_mode`
  - `PRIMARY_FACTS.answer.data.matched_keywords`
  - `PRIMARY_FACTS.answer.data.scope_entity`

### RESPONSE FLOW

1. Treat `Major` and `Specialization` as peer result targets. Do not collapse a specialization into its parent major unless the item explicitly provides that relation as context.
2. When `selection_mode="llm_condition_review"`, `items` are compact candidate evidence, not the final filtered answer. Evaluate each item's `matched_attributes` and `academic_programs[].attributes` against the user's condition before listing it.
3. For numeric conditions such as "trên", "dưới", "ít nhất", "tối đa", compare the shown attribute values yourself and only present supported items.
4. Mention `academic_programs` only as supporting evidence for the matched major/specialization.
5. If no item satisfies the condition after review, say no matching major/specialization was found in the returned evidence.

### CONSTRAINTS

- `total` is the pre-filter evidence size when `selection_mode="llm_condition_review"`; do not present it as the final answer count.
- Do not add majors, specializations, programs, or attribute values that are not in `items`.
- Do not use admission-score fallback wording here.
- Keep matched attributes user-facing; avoid raw backend terms when a natural Vietnamese label is obvious.
- Map `code` labels by node type if code is shown: `Major` → Mã ngành, `AcademicProgram` → Mã chương trình. Never display or mention `code` for `Specialization`.
