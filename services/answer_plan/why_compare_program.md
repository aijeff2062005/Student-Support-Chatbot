### OBJECTIVE

Answer **WHY-PROGRAM (COMPARE)** questions by contrasting training design, career direction, and learner fit across programs.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "program"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.programs`
  - `query_results_raw` (supporting only)
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.programs[*]`
  - compared program bundles, each may include:
    - `program_info`
    - `learning_outcomes`
    - `objectives`
    - `courses`
    - `assessment_methods`
    - `teaching_methods`
    - `parent_major`
    - `policies`
    - `market_trends`
    - `partners`

### APPLICABLE QUESTION PATTERNS

- "Why choose program A over B?" / "Vì sao chọn CT A thay vì B?"
- "Compare program A with B" / "So sánh chương trình A và B"
- "Which program is better for career X?" / "CT nào tốt cho nghề X?"
- "What's different between these curricula?" / "Các CTĐT khác nhau chỗ nào?"
- Any WHY question where `compare_mode = true` and `primary_topic = program`

**Response structure:** CONTEXT & STAKES → KEY DIFFERENCES → YOUR DECISION

### RESPONSE STRUCTURE

#### OPENING — Frame the decision
- Position this as a meaningful choice because each program leads to a different style of learning and different outcomes.

#### PART 1: CONTEXT & STAKES
- Start from `program_info`, `parent_major`, and market context if present.

#### PART 2: KEY DIFFERENCES
- Use outcomes, courses, teaching methods, and partner signals.

#### PART 3: YOUR DECISION
- Close with learner/profile fit and likely trajectory, grounded in the retrieved evidence.

### NATURAL TRANSITION PHRASES

- "Here's what each program is really about..."
- "Now let's look at where they actually differ..."
- "So how do you decide?"
- "The real distinction is..."

### SPECIAL RULES

- Treat each `topics.programs[*]` item as a program evidence bundle.
- Do not force one-to-one comparisons for fields that exist only on one side.
- Avoid generic career hype if `market_trends` or partner evidence is thin.

### CONSTRAINTS

- Keep the comparison grounded in program evidence.
- Do not invent rankings, salary claims, or superiority claims.
- Length: **220-280 words**
- Response language: **Vietnamese or natural bilingual handling if user asks that way**
- **NO TABLES**
- Use **bold** for: program names, market figures, key outcomes
- Stay neutral: both options may be valuable

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If comparison evidence is incomplete, keep the conclusion narrow and explicit about what is verifiable.
