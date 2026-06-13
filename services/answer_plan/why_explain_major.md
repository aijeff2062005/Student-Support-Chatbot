### OBJECTIVE

Answer **WHY-EXPLAIN-MAJOR** questions by explaining why a major/specialization is worth studying, how GDU trains for it, and who it fits.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "explain_major"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.main_entity`
  - `topics.specializations`
  - `topics.academic_programs`
  - `topics.learning_outcomes`
  - `topics.market_trends`
  - `topics.faculty_info`
  - `topics.partners`
  - `query_results_raw` (supporting only)
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.main_entity[*]`
  - the main major/specialization being explained
- `topics.specializations[*]`
  - deeper tracks under the main major when available
- `topics.academic_programs[*]`
  - training-program context
- `topics.learning_outcomes[*]`
  - capabilities students are expected to develop
- `topics.market_trends[*]`
  - demand/trend context
- `topics.faculty_info[*]`, `topics.partners[*]`
  - support and credibility context when available

### APPLICABLE QUESTION PATTERNS

- "Why study major X?" / "Vì sao nên học ngành X?"
- "Why choose specialization Y?" / "Vì sao chọn chuyên ngành Y?"
- "Why is this major trending?" / "Vì sao ngành này hot?"
- "Why does this major have good job prospects?" / "Vì sao ngành này dễ xin việc?"
- Any WHY question where `primary_topic = major`

**Response structure:** MARKET OPPORTUNITY → UNIQUE VALUE → YOUR PATH

### ENTITY HIERARCHY

- `Major`: main major, may have faculty and partner evidence.
- `Specialization`: narrower track under a major, may not have direct faculty/partner evidence.

### DATA EXTRACTION RULES

- Treat `topics.main_entity` as the anchor for the answer.
- Use `topics.market_trends` to justify "why this field matters" when available.
- Use `topics.learning_outcomes` and `topics.academic_programs` to explain how GDU turns field value into a real training path.
- If the resolved entity is a specialization, do not invent faculty or partner context when absent.

### RESPONSE STRUCTURE

#### OPENING — Start from the real decision
Frame major choice as choosing a type of problem to solve and a kind of work to grow into.

#### PART 1: MARKET OPPORTUNITY
**Evidence mapping:**
- `topics.main_entity` → description, objective, career directions, key points
- `topics.market_trends` → demand, growth, gap, domains
- `topics.learning_outcomes` → capability relevance

**What this section should do:**
- Trả lời vì sao ngành/chuyên ngành này đáng học trong bối cảnh hiện tại.
- Nếu không có market data mạnh, dùng career direction + key points để thay thế.

#### PART 2: UNIQUE VALUE
**Evidence mapping:**
- `topics.academic_programs` → training duration, method, path
- `topics.learning_outcomes` → năng lực cụ thể
- `topics.faculty_info` / `topics.partners` → support và credibility nếu có
- `topics.specializations` → chiều sâu/nhánh học thêm nếu là major

**Must-include when available:**
- 2-3 learning outcomes thực sự mô tả được giá trị đào tạo
- 1-2 signals cho thấy GDU đào tạo ngành này theo cách có điểm khác biệt

**Specialization rule:**
- Nếu là specialization, không được tự suy ra faculty/partner nếu bundle không có.

#### PART 3: YOUR PATH
**Evidence mapping:**
- `topics.main_entity` career opportunities
- `topics.academic_programs` further-study direction
- `topics.specializations` nếu relevant

**LLM reasoning allowed:**
- Suy ra learner fit từ objective + outcomes + career direction.
- Gợi ra trajectory nghề nghiệp ở mức hợp lý, không bịa số liệu hay lương.

### NATURAL TRANSITION PHRASES

- "Điều khiến ngành này đáng học là..."
- "Ở GDU, điểm đáng chú ý không chỉ là tên ngành mà là cách đào tạo..."
- "Nếu đặt từ góc nhìn người học, ngành này hợp hơn với những bạn..."
- "Vì vậy, giá trị của ngành không nằm ở xu hướng ngắn hạn mà ở..."

### SPECIAL RULES

- Treat `topics.main_entity[*]` as the anchor for the answer.
- If the resolved entity is a specialization, do not automatically inherit absent faculty/partner claims from the parent major.
- Do not rely on legacy top-level keys outside `answer.data.topics.*`.

### CONSTRAINTS

- Keep the answer grounded in retrieved major/specialization evidence only.
- Do not fabricate trend numbers, salary claims, or prestige labels.
- Length: **230-300 words**
- Response language: **Vietnamese**
- Use **bold** for: major/specialization names, key figures, partner names, key outcomes
- Tone: informed, grounded, persuasive
- Do NOT end with a CTA or a question back to the user

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If data is partial, keep claims narrow and explicit about what is verifiable.
