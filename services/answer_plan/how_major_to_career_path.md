### OBJECTIVE

Answer **HOW-MAJOR-TO-CAREER-PATH** questions by mapping role options and realistic progression edges.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.answer.kind = "roadmap"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `career_options`
  - `progression_edges`

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- `career_positions` -> `answer.data.career_options`
- `career_progression` -> `answer.data.progression_edges`

### RESPONSE FLOW

1. ROLE LANDSCAPE
- Use `career_options` to show reachable roles.

2. PROGRESSION PATHS
- Use `progression_edges` to show movement from one role to another.

3. PRACTICAL TAKEAWAY
- Suggest a staged path based on available transitions.

### SPECIAL RULES

- If progression edges are sparse, avoid forcing a full ladder.
- Mention salary fields only as indicative signals.

### CONSTRAINTS

- No invented role transitions.
- Keep mapping explicit and evidence-based.

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
