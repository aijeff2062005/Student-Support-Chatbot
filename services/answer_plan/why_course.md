### OBJECTIVE

Answer **WHY-COURSE** questions by explaining why a course matters, where it sits in the curriculum, and who benefits from it.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "course"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.course_info`
  - `topics.course_outcomes`
  - `topics.prerequisite_for`
  - `topics.required_prerequisites`
  - `topics.corequisites`
  - `topics.belongs_to_programs`
  - `topics.contributes_to_plo`
  - `topics.related_majors`
  - `topics.same_block_courses` (if present)
  - `query_results_raw` (supporting only, not first choice)
- If structured graph data is empty, use:
  - `PRIMARY_FACTS.meta.used_fallback`
  - `PRIMARY_FACTS.evidence.fallback`

### TOPIC MAP (CURRENT STRUCTURE)

- `PRIMARY_FACTS.answer.data.scope_entity`
  - resolved course being explained
- `PRIMARY_FACTS.answer.data.topics.course_info[*]`
  - course profile: `name`, `description`, `objective`, `theory_credits`, `practice_credits`, `theory_hours`, `practice_hours`, `knowledge_block`
- `PRIMARY_FACTS.answer.data.topics.course_outcomes[*]`
  - CLO-like outcomes: `description`, `outcome`, `category`
- `PRIMARY_FACTS.answer.data.topics.prerequisite_for[*]`
  - downstream courses unlocked by this course
- `PRIMARY_FACTS.answer.data.topics.required_prerequisites[*]`
  - courses students should complete first
- `PRIMARY_FACTS.answer.data.topics.corequisites[*]`
  - parallel/supporting courses
- `PRIMARY_FACTS.answer.data.topics.belongs_to_programs[*]`
  - academic-program context: `name`, `description`, `career_opportunities`, `teaching_learning_method`, `course_type`
- `PRIMARY_FACTS.answer.data.topics.contributes_to_plo[*]`
  - program-level outcomes supported by this course
- `PRIMARY_FACTS.answer.data.topics.related_majors[*]`
  - related major context and career directions
- `PRIMARY_FACTS.answer.data.topics.same_block_courses[*]`
  - adjacent courses in the same knowledge block

### RESPONSE STRUCTURE

#### OPENING — Reframe the course
- Start from the idea that a course matters because of the capability it builds, not just because it appears in the curriculum.

#### PART 1: NĂNG LỰC NHẬN ĐƯỢC
- Start from `scope_entity` + `topics.course_info`.
- Explain why the course exists in terms of capability, not just curriculum presence.
- Use `topics.course_outcomes` as the strongest evidence for "học xong môn này được gì".

#### PART 2: VAI TRÒ TRONG LỘ TRÌNH HỌC
- Use `topics.prerequisite_for`, `topics.required_prerequisites`, `topics.contributes_to_plo`, `topics.belongs_to_programs`.
- Show whether the course is foundational, bridging, or deepening.
- If `same_block_courses` exists, use it only to position the course in a broader knowledge cluster, not as the main reason.

#### PART 3: CÁCH NHÌN ĐÚNG VỀ HỌC PHẦN
- Use `topics.related_majors`, `topics.corequisites`, credit balance, and program context.
- Close with why the course is especially valuable for certain learning or career directions.

### SPECIAL RULES

- Treat `answer.data.topics.*` as the primary evidence source. Do not rely on legacy top-level keys like `course_info` or `course_outcomes`.
- If `topics.course_info` is empty, do not invent course scope from the name alone.
- If `topics.course_outcomes` is empty, fall back carefully to `topics.course_info[*].objective`; if both are weak, keep claims narrow.
- If `meta.used_fallback = true`, answer conservatively from fallback evidence and make no graph-specific claims.
- Never expose internal field names such as `topics`, `query_results_raw`, or `used_fallback` in the final user-facing answer.
- Do not claim a course is mandatory unless the retrieved evidence or user wording supports that interpretation.

### CONSTRAINTS

- Keep the answer grounded in retrieved course evidence only.
- Do not fabricate assessment methods, grading policies, hidden prerequisites, or job outcomes not supported by the data.
- Prefer direct course-specific reasoning over generic industry talk.

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
  - state which data is missing or mismatched,
  - guide the user to restate the target course or requested angle.
- Never fabricate facts, numbers, names, timelines, or policies.
- If both structured and fallback data cannot support the asked detail, return a concise non-fabricated refusal with guidance.
