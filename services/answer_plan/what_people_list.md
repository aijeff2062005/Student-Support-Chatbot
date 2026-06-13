### OBJECTIVE

Compatibility-safe legacy plan for **WHAT-LIST Person**.

### CONTRACT ALIGNMENT

Treat this file as legacy alias of `what_list_people` and use canonical compact fields only.

Use only:
- `PRIMARY_FACTS.answer.data.formatted_answer`
- `PRIMARY_FACTS.answer.data.items`
- `PRIMARY_FACTS.answer.data.scope_entity`
- `PRIMARY_FACTS.answer.data.total`
- `PRIMARY_FACTS.pagination.next_start_index`
- `PRIMARY_FACTS.meta.used_fallback` (if present)

### REQUIRED BUSINESS RULES

- Never state or imply numeric people count.
- Group semantics:
  - Ban giám hiệu = Hiệu trưởng + Phó Hiệu trưởng.
  - Hội đồng trường = Chủ tịch HĐT + Phó Chủ tịch HĐT + Thành viên HĐT.
- Expand abbreviations to full Vietnamese forms:
  - `GS` Giáo sư; `PGS` Phó giáo sư
  - `TS` Tiến sĩ; `TSKH` Tiến sĩ khoa học; `ThS` Thạc sĩ
  - `CN` Cử nhân; `KS` Kỹ sư; `CNKT` Cử nhân kỹ thuật
  - `KTS` Kiến trúc sư; `BS` Bác sĩ; `DS` Dược sĩ; `LS` Luật sư
  - `NGND` Nhà giáo Nhân dân; `NGUT` Nhà giáo Ưu tú
  - `GV` Giảng viên; `GVC` Giảng viên chính; `GVCC` Giảng viên cao cấp
  - `NCV` Nghiên cứu viên; `NCVC` Nghiên cứu viên chính; `NCVCC` Nghiên cứu viên cao cấp
  - Common combos: `GS.TS`, `PGS.TS`, `GS.TSKH`, `PGS.TSKH`, `TS.BS`, `ThS.BS`, `PGS.TS.BS`, `TS.LS`, `ThS.LS` → expanded fully.
- If `effective_from` / `effective_to` appears in visible facts, treat them as user-facing role-period validity. Render `effective_from` as **hiệu lực từ** or **bắt đầu đảm nhiệm**. Render `effective_to` flexibly as **hiệu lực đến**, **hết hạn**, **hết hiệu lực**, **kết thúc nhiệm kỳ**, or **kết thúc áp dụng** when they matter to the asked appointment/role period. Never expose the raw key names in the final answer.

### RESPONSE FLOW

1. **HARD PRIORITY**: If `answer.data.formatted_answer` is non-empty, use it FIRST as factual backbone, then rewrite naturally for the final response (not verbatim copy).
2. Only add/adjust minimal details from `answer.data.items` and scope/pagination fields when needed for strict correctness.
3. If the resolved people scope/group is clearly different from what the user asked, mismatch response overrides normal people-list behavior.

### CONSTRAINTS

- Do not rely on legacy keys (`list_results`, `found_entity_list`, ...).
- Do not silently substitute one people group/scope for another.
- Never use technical or meta wording such as "kết quả truy vấn", "dữ liệu hiện tại", "hệ thống", "record", "match", or similar backend-facing language in the user-facing answer.
- Do not fabricate facts.
