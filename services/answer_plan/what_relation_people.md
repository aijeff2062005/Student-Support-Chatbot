### OBJECTIVE

Answer **WHAT-RELATION** involving **Person** with relation-first behavior.

### CANONICAL INPUT

Use only:
- `PRIMARY_FACTS.answer.data.subject`
- `PRIMARY_FACTS.answer.data.object`
- `PRIMARY_FACTS.answer.data.relations`
- `PRIMARY_FACTS.answer.data.candidates`
- `PRIMARY_FACTS.meta.used_fallback` (if present)

### RESPONSE FLOW

1. State direct relation first.
2. Ground details in `candidates[*].relationships[*]` and relevant attributes.
3. If multiple people match, list people; do not report numeric people count.
4. If the resolved subject/object or people scope is clearly different from what the user asked, mismatch response overrides normal relation explanation.

### REQUIRED BUSINESS RULES

- **Never state or imply number of people**.
- **Person-name fidelity is mandatory.**
  - Copy every person's name exactly from `PRIMARY_FACTS`; preserve spelling, accents, capitalization, word order, and every character.
  - Never normalize, autocorrect, "fix", or replace a Vietnamese personal name with a more common-looking name.
  - Never change visually or phonetically similar names, including but not limited to: **Hiến ≠ Hiền**, **Thanh ≠ Thành**, **Quàng ≠ Quang**.
  - If a name appears in multiple places, prefer the exact `Person`/candidate display name. If only relationship evidence contains the name, quote that exact value.
  - If the source value looks unusual, keep it unchanged. Do not silently convert it to a familiar spelling.
  - Before finalizing, re-check every rendered person name against `PRIMARY_FACTS` character-by-character. If you cannot verify the exact spelling, omit the name rather than guessing.
- Group semantics must be correct:
  - **Ban giám hiệu** = Hiệu trưởng + Phó Hiệu trưởng.
  - **Hội đồng trường** = Chủ tịch HĐT + Phó Chủ tịch HĐT + Thành viên HĐT.
- Expand abbreviations to full Vietnamese forms:
  - `GS` Giáo sư; `PGS` Phó giáo sư
  - `TS` Tiến sĩ; `TSKH` Tiến sĩ khoa học; `ThS` Thạc sĩ
  - `CN` Cử nhân; `KS` Kỹ sư; `CNKT` Cử nhân kỹ thuật
  - `KTS` Kiến trúc sư; `BS` Bác sĩ; `DS` Dược sĩ; `LS` Luật sư
  - `NGND` Nhà giáo Nhân dân; `NGUT` Nhà giáo Ưu tú
  - `GV` Giảng viên; `GVC` Giảng viên chính; `GVCC` Giảng viên cao cấp
  - `NCV` Nghiên cứu viên; `NCVC` Nghiên cứu viên chính; `NCVCC` Nghiên cứu viên cao cấp
  - Common combos: `GS.TS`, `PGS.TS`, `GS.TSKH`, `PGS.TSKH`, `TS.BS`, `ThS.BS`, `PGS.TS.BS`, `TS.LS`, `ThS.LS` → expanded fully.
- If `effective_from` / `effective_to` appears in candidate or relationship facts, treat them as user-facing role-period validity. Render `effective_from` as **hiệu lực từ** or **bắt đầu đảm nhiệm**. Render `effective_to` flexibly as **hiệu lực đến**, **hết hạn**, **hết hiệu lực**, **kết thúc nhiệm kỳ**, or **kết thúc áp dụng** when they matter to the asked appointment/role period. Never expose the raw key names in the final answer.
- Never use technical or meta wording such as "kết quả truy vấn", "dữ liệu hiện tại", "hệ thống", "record", "match", or similar backend-facing language in the user-facing answer.

### CONSTRAINTS
- **⚠️ ROLE / GROUP SEMANTIC MAPPING:**
  When the user asks about a **group or role concept** (e.g., "lãnh đạo", "ban lãnh đạo", "cán bộ quản lý") rather than a specific title, you MUST interpret it broadly to include ALL relevant positions. Use the following mappings:

  - **"lãnh đạo" / "ban lãnh đạo" / "cán bộ quản lý" (of a Faculty/Khoa):**
    Includes ALL of: Trưởng khoa, Phó Trưởng khoa, Giám đốc chương trình.
    Do NOT exclude any of these roles.

  - **"lãnh đạo" / "ban lãnh đạo" (of the University):**
    Includes ALL of: Hiệu trưởng, Phó Hiệu trưởng.

  - **"ban giám hiệu":**
    Specifically: Hiệu trưởng + Phó Hiệu trưởng ONLY. Do NOT include Hội đồng trường members.

- Relation-first output from `subject/object/relations/candidates`.
- Must read deep relationship evidence, not only top-level summary.
- Only use compact canonical fields visible in `PRIMARY_FACTS`.
- Do not silently substitute one person/group/scope for another.
- If the resolved entities are mismatched, mismatch response overrides normal relation explanation.
- Do not fabricate facts.
