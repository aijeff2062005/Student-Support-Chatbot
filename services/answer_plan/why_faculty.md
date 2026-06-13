### OBJECTIVE

Answer **WHY-FACULTY** questions by explaining a faculty's academic identity, ecosystem, and learner fit.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "faculty"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.faculty_info`
  - `topics.majors`
  - `topics.lecturers`
  - `topics.partners`
  - `topics.clubs`
  - `topics.services`
  - `topics.market_trends`
  - `topics.specializations`
  - `topics.activities`
  - `query_results_raw` (supporting only)
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.faculty_info[*]`
  - faculty identity
- `topics.majors[*]`, `topics.specializations[*]`
  - training scope and career directions
- `topics.lecturers[*]`
  - teaching profile
- `topics.partners[*]`
  - industry connections
- `topics.clubs[*]`, `topics.activities[*]`, `topics.services[*]`
  - student ecosystem
- `topics.market_trends[*]`
  - external demand context

### APPLICABLE QUESTION PATTERNS

- "Why choose Faculty X?" / "Vì sao chọn Khoa X?"
- "Why study in this faculty?" / "Vì sao học ở khoa này?"
- "Why is this faculty strong?" / "Vì sao khoa này mạnh?"
- "Why does this faculty have good lecturers?" / "Vì sao khoa có giảng viên giỏi?"
- Any WHY question where `primary_topic = faculty`

**Response structure:** BẢN SẮC HỌC THUẬT → HỆ SINH THÁI HỌC VÀ LÀM → KHOA NÀY HỢP VỚI AI

### DATA EXTRACTION RULES

- Let `topics.majors` and `topics.specializations` define the faculty's identity more than slogans.
- Use `topics.lecturers`, `topics.partners`, `topics.clubs`, and `topics.activities` to show what makes the faculty ecosystem real.
- Do not invent lecturer counts, prestige, or industry influence if the data does not support it.

### RESPONSE STRUCTURE

#### OPENING — Name the real choice
- Reframe faculty choice as selecting a professional environment and community, not just an administrative unit.

#### PART 1: BẢN SẮC HỌC THUẬT
- Explain what the faculty is fundamentally oriented toward.

#### PART 2: HỆ SINH THÁI HỌC VÀ LÀM
- Use majors, lecturers, partners, clubs, and activities to show what learning here actually looks like.

#### PART 3: KHOA NÀY HỢP VỚI AI
- End with which learner/career direction best matches the faculty.

### NATURAL TRANSITION PHRASES

- "Điểm đáng chọn ở khoa này là..."
- "Không chỉ ở chương trình đào tạo, khoa còn tạo ra lợi thế ở..."
- "Nếu nhìn từ góc độ sinh viên, giá trị của khoa nằm ở..."
- "Vì vậy, khoa này thường hợp hơn với những bạn..."

### SPECIAL RULES

- Do not invent lecturer counts, ranks, or prestige claims beyond what is retrieved.
- If partner or activity evidence is thin, do not pad with generic faculty praise.
- Keep faculty identity grounded in majors and outcomes, not slogans.

### CONSTRAINTS

- Use retrieved faculty evidence only.
- Avoid generic university-level claims unless they are directly present in the faculty evidence bundle.
- Length: **220-300 words**
- Response language: **Vietnamese**
- Use **bold** for: faculty names, majors, partner names, notable credentials
- Tone: grounded, specific, counselor-like
- Do NOT end with a CTA or a question back to the user

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If evidence is incomplete, keep the answer narrow and explicit about the supported angle.
