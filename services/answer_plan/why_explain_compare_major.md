### OBJECTIVE

Answer **WHY-EXPLAIN-MAJOR (COMPARE)** questions by contrasting majors/specializations in terms of value, training direction, and learner fit.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "explain_major"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.entities`
  - `topics.comparison_type` (if present)
  - `query_results_raw` (supporting only)
- Comparison-type hints may only appear inside `query_results_raw`; use them as support, not as the main fact source.
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.entities[*]`
  - compared entity bundles, each may include:
    - `main_entity`
    - `specializations`
    - `academic_programs`
    - `learning_outcomes`
    - `market_trends`
    - `faculty_info`
    - `partners`
- `topics.comparison_type[*]` or `query_results_raw`
  - optional support for Major-vs-Major / Specialization-vs-Specialization framing

### APPLICABLE QUESTION PATTERNS

- "Why choose A over B?" / "Vì sao chọn ngành A thay vì B?"
- "Compare major A with B" / "So sánh ngành A với B"
- "Why is major A better than B for me?" / "Vì sao A phù hợp hơn B?"
- "What's different between specialization X and Y?" / "Chuyên ngành X khác Y chỗ nào?"
- Any WHY question where `compare_mode = true` and entities are Major/Specialization

**Response structure:** CONTEXT & STAKES → KEY DIFFERENCES → YOUR DECISION

### RESPONSE STRUCTURE

#### OPENING — Frame the decision
- Acknowledge that both directions may be viable, but the real issue is which one aligns better with the student's path.

#### PART 1: CONTEXT & STAKES
- Start from `main_entity`, objective, and career direction.

#### PART 2: KEY DIFFERENCES
- Use learning outcomes, programs, specializations, market trends, and partners when available.

#### PART 3: YOUR DECISION
- Close with decision guidance grounded in retrieved differences, not stereotypes.

### NATURAL TRANSITION PHRASES

- "To understand the choice, let's look at what each offers..."
- "Now, where do they actually diverge?"
- "So how do you decide?"
- "The real distinction is..."

### SPECIAL RULES

- Respect entity type boundaries: do not invent faculty/partner data for a specialization if the evidence bundle does not contain it.
- If comparison-type metadata is missing, infer only the minimum safe framing from the retrieved entity bundles.
- Avoid blanket claims like "ngành A tốt hơn ngành B".

### CONSTRAINTS

- Keep the comparison evidence-led and balanced.
- Do not fabricate rankings, salaries, or employment rates.
- Length: **200-260 words**
- Response language: **Vietnamese**
- **NO TABLES**
- Use **bold** for: major/specialization names, key figures, partner names
- Stay neutral — no bias unless evidence clearly supports a limitation

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If comparison evidence is asymmetric, say so and keep conclusions narrow.
