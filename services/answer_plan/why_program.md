### OBJECTIVE

Answer **WHY-PROGRAM** questions by explaining why a program is worth studying, how it is designed, and who it suits.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "program"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.program_info`
  - `topics.learning_outcomes`
  - `topics.objectives`
  - `topics.courses`
  - `topics.assessment_methods`
  - `topics.teaching_methods`
  - `topics.parent_major`
  - `topics.policies`
  - `topics.market_trends`
  - `topics.partners`
  - `query_results_raw` (supporting only)
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.program_info[*]`
  - core program profile
- `topics.learning_outcomes[*]`, `topics.objectives[*]`
  - capability and design goals
- `topics.courses[*]`, `topics.assessment_methods[*]`, `topics.teaching_methods[*]`
  - training mechanics
- `topics.parent_major[*]`
  - broader major context
- `topics.policies[*]`, `topics.market_trends[*]`, `topics.partners[*]`
  - support, relevance, and credibility

### APPLICABLE QUESTION PATTERNS

- "Why choose program X?" / "Vì sao chọn chương trình X?"
- "Why is this program effective?" / "Vì sao CTĐT này hiệu quả?"
- "Why does this program have these courses?" / "Vì sao CT có những học phần này?"
- "Why study this curriculum?" / "Vì sao nên học chương trình này?"
- Any WHY question where `primary_topic = program`

**Response structure:** MARKET RELEVANCE → PROGRAM DISTINCTIVES → YOUR JOURNEY

### RESPONSE STRATEGY

**CASE 1: Generic program question**
→ Build the answer around market value, training distinctives, and learner journey.

**CASE 2: Personal-fit question**
→ Use outcomes, teaching methods, and career paths to infer who the program suits best.

**CASE 3: Curriculum-design question**
→ Increase weight on `topics.courses`, `topics.learning_outcomes`, `topics.teaching_methods`, and `topics.assessment_methods`.

### DATA EXTRACTION RULES

- Prioritize `topics.program_info` as the anchor before using supporting topics.
- If `topics.market_trends` exists, use it to justify "why now"; if not, rely on `career_opportunities` and parent-major context.
- Prefer 2-3 concrete differentiators from `topics.learning_outcomes`, `topics.courses`, `topics.teaching_methods`, and `topics.partners` instead of generic praise.
- Use `query_results_raw` only when needed to verify edge context, never as the first fact source.

### RESPONSE STRUCTURE

#### OPENING — Frame the real choice
Open from the idea that choosing a program means choosing a learning path and a future capability set, not just a program title.

#### PART 1: MARKET RELEVANCE
**Evidence mapping:**
- `topics.market_trends` → growth, demand, domain
- `topics.program_info` → objective, career opportunities
- `topics.parent_major` → broader field context, employment proof if available

**What this section should do:**
- Trả lời "vì sao chương trình này đáng học bây giờ".
- Nếu market data yếu, thay bằng nghề đầu ra và objective.

#### PART 2: PROGRAM DISTINCTIVES
**Evidence mapping:**
- `topics.learning_outcomes` / `topics.objectives` → năng lực cốt lõi
- `topics.courses` → curriculum highlights
- `topics.teaching_methods` / `topics.assessment_methods` → cách học và cách chứng minh năng lực
- `topics.partners` → kết nối thực tiễn

**Must-include when available:**
- 2-3 năng lực hoặc outcomes thật sự đáng học
- 2-4 course / method / partner signals cho thấy chương trình không chỉ là lý thuyết

**LLM reasoning allowed:**
- Suy ra điểm khác biệt thực chất của chương trình từ tổ hợp outcomes + methods + courses.
- Nếu user hỏi "vì sao có mấy học phần này", tăng trọng số giải thích logic curriculum.

#### PART 3: YOUR JOURNEY
**Evidence mapping:**
- `topics.program_info` → further study, graduation requirements, career opportunities
- `topics.policies` → support context nếu thật sự liên quan
- `topics.parent_major` → career direction

**What this section should do:**
- Khép lại bằng việc chương trình này hợp với ai và dẫn người học đi đến đâu.
- Cho người đọc thấy một hành trình hợp lý từ học đến làm hoặc học tiếp.

### NATURAL TRANSITION PHRASES

- "Điểm đáng học của chương trình này là..."
- "Quan trọng hơn, giá trị không nằm ở tên chương trình mà ở cách chương trình đào tạo..."
- "Nếu nhìn từ góc độ người học, điều này có ý nghĩa vì..."
- "Vì vậy, chương trình này hợp hơn với những bạn..."

### SPECIAL RULES

- Keep `topics.program_info[*]` as the anchor; do not drift into generic field talk.
- Do not invent accreditation, ranking, or employer outcomes.
- If market evidence is weak, rely on program design and outcomes instead of hype.

### CONSTRAINTS

- Use retrieved program evidence only.
- Avoid brochure-style wording unsupported by the data.
- Length: **220-300 words**
- Response language: **Vietnamese**
- Use **bold** for: program name, market figures, course names, partner names
- Skip market data gracefully if not available
- Tone: practical, forward-looking, grounded
- Do NOT end with a CTA or a question back to the user

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If some program dimensions are missing, keep the argument focused on the verified ones.
