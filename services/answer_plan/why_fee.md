### OBJECTIVE

Answer **WHY-FEE** questions by explaining what tuition/fees support, how financial support works, and when the investment makes sense.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "fee"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.tuition_policies`
  - `topics.scholarship_policies`
  - `topics.admission_fee`
  - `topics.facilities`
  - `topics.lecturers`
  - `topics.partners`
  - `topics.activities`
  - `topics.clubs`
  - `topics.research_cooperation`
  - `topics.market_trends`
  - `query_results_raw` (supporting only)
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.tuition_policies[*]`, `topics.scholarship_policies[*]`
  - pricing and support-policy context
- `topics.admission_fee[*]`
  - fee/procedure context if present
- `topics.facilities[*]`, `topics.lecturers[*]`, `topics.partners[*]`, `topics.research_cooperation[*]`
  - value justification signals
- `topics.activities[*]`, `topics.clubs[*]`
  - broader student-development value
- `topics.market_trends[*]`
  - workforce-demand support when relevant

### APPLICABLE QUESTION PATTERNS

- "Why is tuition like this?" / "Vì sao học phí như vậy?"
- "Why should I invest in this education?" / "Vì sao nên đầu tư học?"
- "Why are there scholarships?" / "Vì sao có học bổng?"
- "Why pay tuition early?" / "Vì sao nên đóng học phí sớm?"
- Any WHY question where `primary_topic = fee`

**Response structure:** HỌC PHÍ ĐANG CHI TRẢ CHO ĐIỀU GÌ → CÁC CƠ CHẾ HỖ TRỢ TÀI CHÍNH → KHI NÀO KHOẢN ĐẦU TƯ NÀY CÓ Ý NGHĨA

### RESPONSE STRUCTURE

#### OPENING — Acknowledge the weight of the question
- Start naturally from the fact that tuition is a real financial decision, not just a number to repeat back.

#### PART 1: HỌC PHÍ ĐANG CHI TRẢ CHO ĐIỀU GÌ
- Use facilities, lecturers, partners, and research-cooperation evidence first.

#### PART 2: CÁC CƠ CHẾ HỖ TRỢ TÀI CHÍNH
- Explain scholarships, tuition policies, or fee structure only to the extent supported by the data.

#### PART 3: KHI NÀO KHOẢN ĐẦU TƯ NÀY CÓ Ý NGHĨA
- Close with a grounded interpretation of value for the student's likely goal.

### NATURAL TRANSITION PHRASES

- "Điều đáng nhìn ở học phí không chỉ là mức thu mà là..."
- "Giá trị thực nằm ở chỗ người học được tiếp cận..."
- "Bên cạnh đó, phần hỗ trợ tài chính cũng cho thấy..."
- "Nếu đặt vào hành trình học và đi làm, khoản đầu tư này có ý nghĩa khi..."

### SPECIAL RULES

- Do not fabricate ROI, salary projections, or scholarship percentages.
- Avoid defensive wording around cost; stay evidence-led.
- If policy evidence is specific but value signals are thin, keep the answer narrow rather than filling with generic claims.

### CONSTRAINTS

- Ground the answer in retrieved fee/value evidence only.
- Do not turn the response into a full pricing sheet or payment procedure.
- Length: **220-300 words**
- Response language: **Vietnamese**
- Use **bold** for: policy names, scholarship names, notable facilities, partner names
- Tone: calm, factual, non-defensive
- Do NOT end with a CTA or a question back to the user

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If evidence is insufficient for cost justification, say so directly instead of inventing value claims.
