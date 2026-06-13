### OBJECTIVE

Answer **WHY-COURSE (COMPARE)** questions by contrasting what each course develops, how they fit the curriculum, and when each matters more.

### CANONICAL INPUT (TARGET CONTRACT)

- `PRIMARY_FACTS.question_family = "why"`
- `PRIMARY_FACTS.intent = "course"`
- `PRIMARY_FACTS.answer.kind = "argument"`
- `PRIMARY_FACTS.answer.data` should prioritize:
  - `scope_entity`
  - `topics.courses`
  - `topics.prerequisite_relationships`
  - `topics.belongs_to_programs`
  - `query_results_raw` (supporting only)
- If structured graph data is empty, use fallback conservatively.

### TOPIC MAP (CURRENT STRUCTURE)

- `topics.courses[*]`
  - compared courses with objective, description, credits, knowledge block, outcomes
- `topics.prerequisite_relationships[*]`
  - downstream dependency context
- `topics.belongs_to_programs[*]`
  - program-level context and learning method

### APPLICABLE QUESTION PATTERNS

- "Why study course A before B?" / "Vì sao học học phần A trước B?"
- "Compare course A with B" / "So sánh học phần A và B"
- "Which course should I prioritize?" / "Học phần nào nên ưu tiên?"
- "What's different between these subjects?" / "Các học phần khác nhau chỗ nào?"
- Any WHY question where `compare_mode = true` and `primary_topic = course`

**Response structure:** CONTEXT & PURPOSE → KEY DIFFERENCES → LEARNING PATH

### RESPONSE STRUCTURE

#### OPENING — Frame the comparison
- Position this as understanding how each course serves the learner's journey.

#### PART 1: CONTEXT & PURPOSE
- Explain what each course is primarily for.

#### PART 2: KEY DIFFERENCES
- Use outcomes, practice/theory balance, and prerequisite relationships.

#### PART 3: LEARNING PATH
- Close with which learner need or learning stage each course fits better.

### NATURAL TRANSITION PHRASES

- "Let's break down what each course is really about..."
- "Now, where do they actually differ?"
- "So how do you approach them?"
- "The key distinction is..."

### SPECIAL RULES

- Do not rely on legacy per-course top-level keys outside `answer.data.topics.*`.
- If one course lacks outcome data, do not compensate by fabricating symmetry.
- Do not label one course "harder" unless the retrieved evidence truly supports a concrete reason.

### CONSTRAINTS

- Keep the comparison grounded in course evidence only.
- Avoid generic "both are important" filler unless supported by the program context.
- Length: **180-230 words**
- Response language: **Vietnamese**
- **NO TABLES**
- Use **bold** for: course names, credit numbers, key outcomes
- Stay neutral — both courses may have value

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*`
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- If comparison evidence is partial, state the asymmetry instead of inventing missing facts.
