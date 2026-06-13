### OBJECTIVE

Answer **HOW-CAREER-POSITION** questions by clarifying role requirements, realistic expectations, and a practical growth direction.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.answer.kind = "roadmap"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `career_profile`
  - `requirements`
  - `growth_direction`

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- `career_info` -> `answer.data.career_profile`

### RESPONSE FLOW

1. ROLE BASELINE
- Use `career_profile.name`, `description`, `working_environment`.

2. REQUIREMENT CHECK
- Use `required_experience_years`, `seniority_level`.

3. GROWTH DIRECTION
- Use `potential_salary_min` carefully as baseline signal, not guaranteed outcome.

### SPECIAL RULES

- Be realistic and avoid overpromising career outcomes.
- If any requirement field is missing, state guidance conditionally.

### CONSTRAINTS

- Use structured role evidence only.
- Do not fabricate salary ranges or certification requirements.

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
