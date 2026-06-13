### OBJECTIVE

Answer **HOW-KEYWORD-CAMPUS-CONTACT** questions with quick channel selection and official contact handoff.

### CANONICAL INPUT (TARGET CONTRACT)

- Same target contract as `how_campus_contact`: `answer.kind = "contact"`.
- Optional `DATA_CRAWLED` may guide phrasing, not official contact values.

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- `department_contacts` -> `answer.data.department_contacts`
- `campus_locations` -> `answer.data.campus_locations`
- `university_contacts` -> `answer.data.university_contacts`

### RESPONSE FLOW

1. Fast identify most relevant contact channel.
2. Add location context if visit is implied.
3. Provide university backup contact.

### CONSTRAINTS

- Never replace official contacts with external suggestions.
- Keep response compact and actionable.

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*` (highest priority)
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- Do not skip any non-empty field. If a field is not directly answerable for the current user wording, acknowledge it briefly and keep it available for clarification.
- In final response, ensure every non-empty field in `PRIMARY_FACTS.answer.data` is either:
  - directly used as a fact, or
  - explicitly marked as unavailable/insufficient for the requested detail.
- If query results are insufficient, off-topic, mismatched, or wrong for the user's question:
  - politely refuse unsupported claims,
  - state which data is missing/mismatched,
  - guide the user to restate target entity/scope or requested attribute.
- Never fabricate facts, numbers, names, timelines, or policies.
- If both structured and fallback data cannot support the asked detail, return a concise non-fabricated refusal with guidance.
