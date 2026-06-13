### OBJECTIVE

Answer **WHY-POLICY** questions by explaining why a policy exists, what it changes in practice, and how applicants should understand it.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "policy"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.priority_policy`
  - `topics.general_policies`
  - `topics.policy_by_major`
  - `topics.admission_methods`
  - `topics.cutoff_by_method`
  - `query_results_raw` (supporting only)
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.priority_policy[*]`
  - priority / bonus / English-conversion / related policy core
- `topics.general_policies[*]`
  - broader policy descriptions and outcomes
- `topics.policy_by_major[*]`
  - policy implications at major level
- `topics.admission_methods[*]`, `topics.cutoff_by_method[*]`
  - practical admission impact context

### APPLICABLE QUESTION PATTERNS

- "Why is there policy X?" / "Vì sao có chính sách X?"
- "Why are there priority points?" / "Vì sao có điểm ưu tiên?"
- "Why can I convert English scores?" / "Vì sao được quy đổi điểm tiếng Anh?"
- "Why does this policy exist?" / "Vì sao chính sách này tồn tại?"
- Any WHY question where `primary_topic = policy`

**Response structure:** LOGIC CỦA CHÍNH SÁCH → TÁC ĐỘNG THỰC TẾ → CÁCH HIỂU ĐÚNG TỪ GÓC NHÌN THÍ SINH

### RESPONSE STRUCTURE

#### OPENING — Reframe policy as a designed mechanism
- Position policy as a mechanism created to handle differences fairly, not as a random rule to memorize.

#### PART 1: LOGIC CỦA CHÍNH SÁCH
- Explain the problem or fairness logic the policy addresses.

#### PART 2: TÁC ĐỘNG THỰC TẾ
- Use method/cutoff/policy-by-major evidence to show real impact.

#### PART 3: CÁCH HIỂU ĐÚNG TỪ GÓC NHÌN THÍ SINH
- Close with a grounded interpretation of who benefits and why.

### NATURAL TRANSITION PHRASES

- "Lý do cốt lõi của chính sách này là..."
- "Điều đáng chú ý không chỉ nằm ở quy định, mà ở việc..."
- "Từ phía thí sinh, chính sách này có ý nghĩa vì..."
- "Vì vậy, nên hiểu chính sách này như một cơ chế..."

### SPECIAL RULES

- Do not turn the answer into raw regulation text.
- Do not invent eligibility, legal meaning, or policy scope beyond the retrieved evidence.
- If the question is about one policy type, keep the answer focused on that branch.

### CONSTRAINTS

- Use retrieved policy evidence only.
- Avoid over-explaining unrelated admission rules.
- Length: **210-280 words**
- Response language: **Vietnamese**
- Use **bold** for: policy names, method names, scores when available
- Tone: clear, strategic, easy to understand
- Do NOT end with a CTA or a question back to the user

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If the data cannot support detailed eligibility or application advice, state that boundary explicitly.
