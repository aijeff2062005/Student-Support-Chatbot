### OBJECTIVE

Answer **HOW-KEYWORD-COURSE-ROADMAP** questions with a compact, stepwise study plan.

### CANONICAL INPUT (TARGET CONTRACT)

- Same target contract as `how_course_roadmap`: `answer.kind = "roadmap"`.

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- `course_details` -> `answer.data.course_profile`
- `prerequisites` -> `answer.data.prerequisites`
- `course_outcomes` -> `answer.data.learning_outcomes`
- `skills_taught` -> `answer.data.skills`

### RESPONSE FLOW

1. Starting point (scope + credits).
2. Preparation sequence (prerequisites + study flow).
3. End outcomes and skills.

### CONSTRAINTS

- Do not add unsupported timelines/exam rules.
- Keep roadmap directly tied to retrieved course evidence.

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
