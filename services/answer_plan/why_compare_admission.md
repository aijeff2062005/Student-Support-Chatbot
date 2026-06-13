### OBJECTIVE

Answer **WHY-ADMISSION (COMPARE)** questions by contrasting admission routes and explaining which profile each route fits.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "admission"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.admission_policy`
  - `topics.admission_methods`
  - `topics.admission_combinations`
  - `topics.major_admission_info`
  - `topics.university_context`
  - `query_results_raw` (supporting only)
- Compare-mode hints may appear in `query_results_raw`; use them only as support, not as the main fact source.
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.admission_methods[*]`
  - compared methods and their selection logic
- `topics.major_admission_info[*]`
  - score/quota context by method
- `topics.admission_policy[*]`
  - shared policy framing
- `topics.admission_combinations[*]`
  - combination differences if relevant

### APPLICABLE QUESTION PATTERNS

- "Why choose method A over B?" / "Vì sao chọn PT A thay vì B?"
- "Compare admission method A with B" / "So sánh PT xét tuyển A và B"
- "Which admission method suits me?" / "Phương thức nào phù hợp?"
- "What's better: Xét học bạ or thi THPT?" / "Xét học bạ hay thi tốt hơn?"
- Any WHY question where `compare_mode = true` and `primary_topic = admission`

**Response structure:** CONTEXT & STAKES → KEY DIFFERENCES → YOUR STRATEGY

### RESPONSE STRUCTURE

#### OPENING — Frame the strategic decision
- Position this as a strategic choice, not just a mechanical comparison.

#### PART 1: CONTEXT & STAKES
- Name each route and explain the core difference in what it evaluates.

#### PART 2: KEY DIFFERENCES
- Contrast stability, timing, score pressure, or scope only when supported by `topics.*`.

#### PART 3: YOUR STRATEGY
- End with which applicant profile each method suits better.
- Keep both routes valid unless the evidence shows a real limitation.

### NATURAL TRANSITION PHRASES

- "Here's what each method really asks of you..."
- "So what does this mean in practice?"
- "Now, how do you play this smart?"
- "The key insight is..."

### SPECIAL RULES

- This is a comparison answer, but still use the same canonical `why` contract centered on `answer.data.topics.*`.
- Do not invent unofficial timelines, score thresholds, or "best strategy" advice.
- Avoid table-like output instructions inside the plan; focus on reasoned contrast.
- If the compared items are ambiguous, use only verifiable differences and keep the recommendation narrow.

### CONSTRAINTS

- Ground every contrast in retrieved method/policy evidence.
- Do not fabricate advantages for one route over another.
- Length: **180-240 words**
- Response language: **Vietnamese**
- **NO TABLES**
- Use **bold** for: method names, scores, policy names when relevant
- Stay neutral — both methods are valid unless evidence says otherwise

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If data is insufficient or mismatched, acknowledge the gap and avoid unsupported comparison claims.
