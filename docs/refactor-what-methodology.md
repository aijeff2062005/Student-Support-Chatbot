# Refactor WHAT: Phương pháp đã làm và cách tái sử dụng cho HOW / WHY

## Mục tiêu của đợt refactor WHAT

Phần refactor `what` ở nhánh này không chỉ là chỉnh từng flow riêng lẻ, mà là chuẩn hoá toàn bộ pipeline theo cùng một tư duy:

1. Chuẩn hoá `query plan` để flow dễ đọc, dễ debug, và có chiến lược fallback rõ ràng.
2. Chuẩn hoá `query_results` thành một contract canonical, để downstream không phải hiểu state nội bộ của từng flow.
3. Tách logic “lấy dữ liệu” ra khỏi logic “trình bày cho response agent”.
4. Giảm noise trong prompt để response agent chỉ nhìn thấy dữ liệu thật sự cần dùng.
5. Bổ sung test ở mức contract để lần refactor tiếp theo an toàn hơn.

Các thay đổi này tập trung chủ yếu ở:

- [services/query_plan/what_attributes.yaml](/Users/vanlinhtruongdang/Work/GDU-admission-agent/services/query_plan/what_attributes.yaml)
- [services/query_plan/what_compare.yaml](/Users/vanlinhtruongdang/Work/GDU-admission-agent/services/query_plan/what_compare.yaml)
- [services/query_plan/what_relation.yaml](/Users/vanlinhtruongdang/Work/GDU-admission-agent/services/query_plan/what_relation.yaml)
- [services/query_plan/what_list.yaml](/Users/vanlinhtruongdang/Work/GDU-admission-agent/services/query_plan/what_list.yaml)
- [services/query_plan/what_constraint_list.yaml](/Users/vanlinhtruongdang/Work/GDU-admission-agent/services/query_plan/what_constraint_list.yaml)
- [schemas/what_query_results.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/schemas/what_query_results.py)
- [services/query_plan_execute.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/services/query_plan_execute.py)
- [tools/response_agent_prompt_builder.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/tools/response_agent_prompt_builder.py)
- [tools/response_agent_prompt_rules.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/tools/response_agent_prompt_rules.py)
- [tests/test_reasoning_processing.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/tests/test_reasoning_processing.py)

---

## 1. Refactor theo contract trước, flow sau

Đây là thay đổi quan trọng nhất.

Trước khi refactor sâu từng flow `what_*`, phần output của mỗi flow vẫn mang nhiều dấu vết implementation nội bộ như:

- `candidate_pool`
- `matched_attr_results`
- `subgraph`
- `compare_results`
- các key fallback ad-hoc

Vấn đề là downstream prompt builder và response agent phải hiểu quá nhiều biến tạm của query layer. Điều này làm:

- prompt phình to
- coupling cao
- refactor một flow rất dễ làm vỡ flow khác

### Cách đã làm

Tạo schema canonical riêng cho WHAT tại [schemas/what_query_results.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/schemas/what_query_results.py), với các thành phần ổn định:

- `question_family`
- `intent`
- `status`
- `entities`
- `answer`
- `evidence`
- `pagination`
- `meta`

`services/query_plan_execute.py` sau đó nhận trách nhiệm normalize state cuối của từng flow sang contract này qua các hàm:

- `_normalize_what_attributes_result`
- `_normalize_what_compare_result`
- `_normalize_what_relation_result`
- `_normalize_what_list_like_result`
- `_normalize_what_query_results`

### Giá trị thực tế

- Query plan được phép thay đổi cách triển khai bên trong mà không làm vỡ response layer.
- Prompt builder chỉ cần biết canonical shape.
- Đây là mẫu nên áp dụng lại nguyên vẹn cho `how` và `why`.

### Bài học tái sử dụng cho HOW / WHY

Với `how` và `why`, nên bắt đầu bằng việc định nghĩa:

- `HowQueryResult`
- `WhyQueryResult`

trước khi refactor từng flow con. Nếu chưa có canonical contract, refactor từng file YAML sẽ nhanh bị sa vào sửa cục bộ mà không giảm coupling hệ thống.

---

## 2. Chia flow thành phase rõ ràng và mô tả chiến lược ngay trong YAML

Một đặc điểm nổi bật của đợt refactor WHAT là biến các file YAML từ “danh sách step” thành “tài liệu thiết kế có thể đọc được”.

Ví dụ trong các file `what_*` mới đều có:

- `description` mô tả mục tiêu flow
- `Strategy` mô tả thứ tự xử lý
- comment chia `PHASE 1`, `PHASE 2`, `PHASE 3`, ...
- comment giải thích lý do của nhánh fallback hoặc special case

### Cách đã làm

Các pattern đã áp dụng:

1. Phase hoá rõ ràng.
2. Đặt tên step theo ý nghĩa nghiệp vụ, không đặt theo kỹ thuật mơ hồ.
3. Comment lý do tồn tại của branch, không chỉ mô tả nó làm gì.
4. Viết rõ precedence giữa các branch.

Ví dụ:

- `what_attributes`: `resolve primary -> direct attributes -> AcademicProgram fallback -> relation attrs -> global fallback -> media -> crawled`
- `what_relation`: `resolve context/primary riêng -> ID-based path -> keyword recovery path -> compact output -> crawled`
- `what_constraint_list`: `context-only resolve -> branch A attr -> branch B relation -> merge -> temporal fallback -> candidate pool fallback -> format`
- `what_list`: `resolve context -> deterministic PATH_REGISTRY -> format -> crawled`

### Giá trị thực tế

- Người mới đọc flow hiểu ngay ý đồ hệ thống.
- Khi debug production, có thể khoanh vùng đang fail ở phase nào.
- Khi copy pattern sang flow mới, chỉ cần thay branch nghiệp vụ thay vì viết lại từ đầu.

### Bài học tái sử dụng cho HOW / WHY

Mỗi flow `how_*` và `why_*` nên được viết theo format giống nhau:

1. `description` ngắn gọn nhưng nói rõ answer shape và fallback policy.
2. `Strategy` theo phase.
3. Comment giải thích “vì sao có branch này”.
4. Nếu có special case, ghi luôn trong YAML thay vì để logic ẩn trong Python.

---

## 3. Tách “resolve đầu vào” khỏi “retrieve dữ liệu”

Một pattern được làm khá nhất quán trong WHAT là không trộn resolve entity với retrieval.

### Cách đã làm

Ở các flow mới:

- `context_entities` và `primary_entities` được resolve riêng.
- Khi cần compare thì `primary` và `compare_targets` cũng resolve riêng.
- Một số flow có bước `prepare_entities_for_search` trước khi resolve, để mở rộng type hoặc chuẩn hoá input.

Ví dụ rõ nhất là [services/query_plan/what_relation.yaml](/Users/vanlinhtruongdang/Work/GDU-admission-agent/services/query_plan/what_relation.yaml):

- context luôn là anchor traversal
- primary có thể resolve thành entity thật hoặc chỉ là keyword-like
- nếu primary không resolve được thì chuyển sang keyword recovery path

### Giá trị thực tế

- Hành vi flow rõ hơn nhiều.
- Tránh nhập nhằng giữa entity thật và keyword gợi ý.
- Là nền tảng để fallback thông minh hơn, thay vì fail toàn flow ngay từ đầu.

### Bài học tái sử dụng cho HOW / WHY

`how` và `why` hiện cũng nên giữ nguyên nguyên tắc này:

- resolve entity/topic trước
- sau đó mới retrieval evidence
- cuối cùng mới compose answer payload

Đặc biệt với `why`, cần tách rõ:

- entity đang được hỏi “vì sao nên/chọn...”
- context hỗ trợ lập luận
- evidence để chứng minh

---

## 4. Ưu tiên retrieval có cấu trúc trước, fallback sau

WHAT đã được refactor theo một nguyên lý rất rõ: ưu tiên dữ liệu có cấu trúc, chỉ fallback khi thật sự cần.

### Cách đã làm

Trong hầu hết các flow:

1. Structured graph path chạy trước.
2. Nếu structured path không đủ, dùng hybrid fallback có kiểm soát.
3. Chỉ khi các nhánh trên trống mới dùng `crawled_data`.

Ví dụ:

- `what_attributes`: direct entity attributes trước, rồi AcademicProgram fallback, rồi relation/global fallback, cuối cùng mới crawled.
- `what_compare`: compare trực tiếp bằng Neo4j trước, rồi AcademicProgram fallback.
- `what_list`: ưu tiên `PATH_REGISTRY` deterministic traversal.
- `what_constraint_list`: thử attr branch và relation branch trước khi rơi sang temporal/candidate pool/crawled fallback.

### Giá trị thực tế

- Chất lượng dữ liệu upstream ổn định hơn.
- Response agent không phải đoán nguồn nào đáng tin hơn.
- Có thể encode mức độ tin cậy vào `meta.source` và `meta.used_fallback`.

### Bài học tái sử dụng cho HOW / WHY

`how` và `why` cũng nên dùng cùng policy:

1. Evidence có cấu trúc từ graph / curated source trước.
2. Fallback trung gian nếu có.
3. `crawled_data` chỉ là phương án cuối.

Nên tránh để prompt của `how`/`why` phải tự suy luận “nguồn nào là nguồn chính” khi orchestration có thể quyết định sẵn.

---

## 5. Thiết kế fallback theo tình huống nghiệp vụ, không chỉ fallback generic

Điểm mạnh của refactor WHAT là fallback không còn quá generic. Một số fallback được thiết kế đúng theo failure mode của domain.

### Các pattern đã làm

#### 5.1 AcademicProgram fallback cho Major / Specialization

Áp dụng ở `what_attributes` và `what_compare`.

Lý do:

- Có những thuộc tính nghiệp vụ thật ra nằm trên `AcademicProgram`, không nằm trực tiếp trên `Major` hoặc `Specialization`.

Ý nghĩa cho `how` / `why`:

- Nếu flow `how_major_guidance`, `how_course`, `why_program`, `why_explain_major` gặp dữ liệu bị phân tán giữa `Major` và `AcademicProgram`, nên chuẩn hoá branch fallback tương tự thay vì xử lý ad-hoc trong prompt.

#### 5.2 Temporal fallback

Áp dụng ở `what_constraint_list` qua `get_cutoff_temporal_fallback`.

Lý do:

- Một số câu hỏi current-year không có dữ liệu, nhưng năm gần nhất trước đó có.
- Nếu không có branch này, hệ thống dễ trả lời như thể dữ liệu không tồn tại.

Ý nghĩa cho `how` / `why`:

- Với `how_admission`, `how_fee`, `why_admission`, `why_fee`, nhiều khả năng cũng cần pattern “chưa có dữ liệu năm hiện tại nhưng có dữ liệu tham chiếu gần nhất”.

#### 5.3 Candidate pool fallback

Áp dụng ở `what_constraint_list`.

Lý do:

- Có khi entity scope là đúng, candidate pool cũng đúng, chỉ thiếu đúng attribute constraint ở năm hiện tại.
- Nếu rơi thẳng xuống crawled data sẽ tạo thông điệp sai kiểu “không có ngành”, trong khi thực tế là “có ngành nhưng chưa có dữ liệu thuộc tính”.

Ý nghĩa cho `how` / `why`:

- Đây là pattern rất đáng tái dùng cho các câu hỏi procedural/explanatory khi đã xác định đúng entity nhưng thiếu chi tiết triển khai.

#### 5.4 Keyword recovery path

Áp dụng ở `what_relation`.

Lý do:

- Primary side không phải lúc nào cũng resolve được thành node thật.
- Có khi user đang hỏi vai trò, thuộc tính quan hệ, hoặc mô tả gián tiếp.

Ý nghĩa cho `how` / `why`:

- `why` đặc biệt dễ gặp loại input trừu tượng như “vì sao ngành này đáng học”, “vì sao nên chọn”.
- Cần có nhánh recovery cho các cue khái niệm, không ép mọi thứ phải resolve thành entity cứng.

---

## 6. Dùng deterministic traversal khi bài toán cho phép

Đây là quyết định rất tốt ở `what_list`.

### Cách đã làm

Thay vì generic shortest-path hoặc traversal rộng, flow `what_list` dùng:

- `services.path_registry.list_via_path_registry`

để enumerate theo rule đã định nghĩa sẵn.

### Giá trị thực tế

- Ít noise hơn.
- Dễ kiểm soát output hơn.
- Dễ giải thích hơn khi debug.

### Bài học tái sử dụng cho HOW / WHY

Với những flow `how` hoặc `why` mà answer structure khá cố định theo topic, nên cân nhắc:

- registry / mapping có kiểm soát
- evidence selection theo whitelist

thay vì để retrieval chạy quá mở ngay từ đầu.

Ví dụ:

- `how_course` có thể chọn evidence theo lộ trình học, điều kiện, outcome, hỗ trợ.
- `why_university` có thể chọn evidence theo campus, faculty, facility, partner, event theo khung cố định.

---

## 7. Compact output trước khi đưa sang prompt

Một refactor rất đáng giá là xử lý sạch output ở tầng Python trước khi build prompt.

### Cách đã làm

Trong [tools/response_agent_prompt_builder.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/tools/response_agent_prompt_builder.py):

- loại bỏ internal keys như `id`, `node_id`, `relation_id`, `score`, `branch`, `subgraph`, ...
- collapse map có key nội bộ
- dedupe list giữ nguyên thứ tự
- compact theo từng answer kind:
  - attributes
  - compare
  - relation
  - list/count/constraint-list

Các hàm chính:

- `_sanitize_prompt_data`
- `_compact_what_answer_data`
- `_compact_what_query_results_for_prompt`
- `_compact_prompt_block_content`

### Giá trị thực tế

- Prompt ngắn hơn đáng kể.
- LLM ít bị phân tâm bởi technical metadata.
- Output của response agent ổn định hơn.

### Bài học tái sử dụng cho HOW / WHY

Không nên đưa raw final_state của `how`/`why` thẳng vào prompt.

Nên có bước:

1. normalize thành canonical contract
2. compact contract cho prompt

Hai bước này tách biệt nhau là tốt nhất:

- canonical để ổn định interface
- compact để tối ưu prompt

---

## 8. Tách prompt rules thành shared blocks thay vì hardcode nguyên prompt lớn

WHAT refactor không chỉ sửa query layer, mà còn làm sạch response prompt layer.

### Cách đã làm

Trong [tools/response_agent_prompt_rules.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/tools/response_agent_prompt_rules.py), các rule được tách thành các block có thể tái dùng:

- `_build_output_compliance_block`
- `_build_priority_order_block`
- `_build_role_identity_block`
- `_build_shared_rules_block`
- `_build_answer_query_task_instructions`
- `_build_answer_query_response_structure`
- `_build_primary_facts_*`
- `_build_data_crawled_*`
- `_build_follow_up_hint_rules`
- `_build_relation_attribute_rules`
- `_build_related_nodes_rules`

### Giá trị thực tế

- Tránh prompt builder thành một file monolith rất khó sửa.
- Rule thay đổi ở một chỗ sẽ áp dụng cho nhiều flow.
- Dễ chuẩn hoá behavior giữa các agent.

### Bài học tái sử dụng cho HOW / WHY

Khi refactor `how` và `why`, nên giữ tư duy:

- shared rules block
- family-specific rules block
- answer-shape-specific rules block

Không nên viết một prompt dài riêng cho từng topic nếu chúng vẫn dùng chung cùng answer policy.

Riêng `why`, có thể tách:

- argument-map rules
- evidence-grounding rules
- comparison rules
- direction rules

thành các block tái dùng cho mọi `why_*`.

---

## 9. Chuẩn hoá answer plan theo canonical schema, không bám key cũ

Một phần refactor quan trọng nữa là cập nhật lại các file `services/answer_plan/what_*.md` để bám theo canonical output mới.

### Cách đã làm

Các answer plan của WHAT được chỉnh để:

- đọc từ `answer.kind` và `answer.data`
- không phụ thuộc trực tiếp vào các key state cũ
- không giả định raw retrieval payload

Ví dụ `what_compare.md` đã nhấn mạnh không dựa vào các key cũ như `compare_results`.

### Giá trị thực tế

- Query layer thay đổi mà answer plan không cần sửa nhiều.
- Tăng độ bền của prompt contract.

### Bài học tái sử dụng cho HOW / WHY

Khi bắt đầu refactor `how` và `why`, nếu answer plan còn đang bám trực tiếp vào raw state thì nên sửa sớm.

Thứ tự nên là:

1. canonical schema
2. query plan normalization
3. answer plan migrate sang canonical schema
4. compact prompt

---

## 10. Viết test ở mức contract, không chỉ test branch nội bộ

Đợt refactor WHAT đã bổ sung test có giá trị cao ở [tests/test_reasoning_processing.py](/Users/vanlinhtruongdang/Work/GDU-admission-agent/tests/test_reasoning_processing.py).

### Các kiểu test đã làm

1. Test normalize output sang canonical shape.
2. Test canonical empty result được prompt builder hiểu đúng là empty.
3. Test prompt compaction loại bỏ noise.
4. Test pagination/count giữ đúng semantics.
5. Test fallback shape như `academic_program_fallback`.

### Giá trị thực tế

- Refactor tiếp mà không quá sợ vỡ behavior downstream.
- Test mô tả contract mong muốn, không buộc implementation phải giữ nguyên cấu trúc cũ.

### Bài học tái sử dụng cho HOW / WHY

Khi làm `how` và `why`, nên viết ít nhất các nhóm test sau:

1. `normalize_*_query_results` trả canonical shape đúng.
2. Prompt builder nhận canonical result và compact đúng.
3. Empty / fallback / ok được phân biệt đúng.
4. Một vài regression test cho special fallback quan trọng theo domain.

---

## 11. Những nguyên tắc refactor cốt lõi đã được chứng minh hiệu quả

Đây là phần ngắn gọn nhất để tái dùng nhanh cho `how` và `why`.

### Nguyên tắc 1: Chuẩn hoá output là ưu tiên số một

Nếu output chưa canonical, mọi refactor khác chỉ là dọn cục bộ.

### Nguyên tắc 2: Mỗi flow phải có retrieval strategy đọc được bằng mắt

Chỉ cần mở YAML là hiểu:

- resolve gì trước
- branch nào ưu tiên
- khi nào fallback
- fallback mang ý nghĩa nghiệp vụ gì

### Nguyên tắc 3: Fallback phải phản ánh failure mode thật

Không nên chỉ có một fallback generic kiểu “không có thì crawled”.

### Nguyên tắc 4: Prompt chỉ nên nhìn thấy dữ liệu đã được làm sạch

LLM không nên gánh trách nhiệm lọc metadata nội bộ.

### Nguyên tắc 5: Test contract quan trọng hơn test chi tiết state tạm

State nội bộ có thể đổi, contract downstream thì không nên đổi bừa.

---

## 12. Checklist áp dụng nhanh cho refactor HOW / WHY

### Bước 1. Chốt canonical schema

- Tạo `HowQueryResult` / `WhyQueryResult`
- Xác định rõ:
  - `status`
  - `entities`
  - `answer`
  - `evidence`
  - `meta`
  - nếu cần thì thêm `procedure`, `argument_map`, `next_steps`, `cautions`

### Bước 2. Refactor query plan theo phase

- Viết lại `description`
- Thêm `Strategy`
- Chia phase rõ
- Ghi rõ special case và fallback policy

### Bước 3. Normalize kết quả cuối tại orchestrator

- Viết các hàm `_normalize_how_*` / `_normalize_why_*`
- Không để downstream đọc raw final_state

### Bước 4. Migrate answer plan sang canonical contract

- Đọc từ `answer.data` và `evidence`
- Cắt dependency vào key cũ

### Bước 5. Compact prompt payload

- remove metadata nội bộ
- giữ lại đúng facts cần cho answer generation

### Bước 6. Viết regression test

- ok / fallback / empty
- special fallback
- prompt compaction
- canonical shape

---

## 13. Đề xuất áp dụng cụ thể cho HOW

Với `how`, nhiều khả năng answer shape sẽ xoay quanh procedural guidance, nên có thể tái dùng pattern WHAT như sau:

- `entities`: entity chính, context entity
- `answer.kind`: ví dụ `procedure`, `roadmap`, `contact`, `eligibility`
- `answer.data`: các bước chính, điều kiện, hồ sơ, mốc thời gian, lưu ý
- `evidence.structured`: dữ liệu gốc để agent có thể diễn đạt lại
- `meta`: `graph` / `hybrid` / `crawled`

Fallback đáng cân nhắc:

- temporal fallback cho admission / fee / policy
- candidate-pool-like fallback khi đúng entity nhưng thiếu procedural detail
- contact fallback khi thiếu step chi tiết nhưng có đầu mối chính thức

---

## 14. Đề xuất áp dụng cụ thể cho WHY

Với `why`, phần khó nhất không nằm ở retrieval mà ở argument structure. Tuy nhiên pattern từ WHAT vẫn áp dụng rất tốt ở tầng dữ liệu:

- resolve entity/topic rõ ràng
- lấy evidence có cấu trúc trước
- normalize sang canonical result
- compact trước khi sang prompt argument builder

Khuyến nghị answer shape cho WHY:

- `answer.kind`: `argument`
- `answer.data`:
  - `claim`
  - `reasons`
  - `supporting_capabilities`
  - `comparison_frame`
  - `fit_direction`

Nếu chưa muốn để LLM tự tạo full `argument_map`, có thể normalize trước một phần ở orchestrator để prompt bớt gánh nặng.

---

## 15. Thứ tự ưu tiên nên làm tiếp

Để phần refactor `how` và `why` nhanh hơn, mình đề xuất bám đúng thứ tự này:

1. Chốt schema canonical cho `how` và `why`.
2. Chọn 1 flow đại diện mỗi family để làm mẫu end-to-end.
3. Tách prompt rules thành shared blocks như WHAT.
4. Migrate các answer plan còn bám raw state.
5. Sau khi mẫu ổn, mới nhân rộng sang toàn bộ `how_*` và `why_*`.

---

## Kết luận ngắn

Refactor WHAT thành công vì đã chuyển từ tư duy “sửa từng flow” sang “chuẩn hoá contract, retrieval strategy, fallback semantics, và prompt boundary”.

Nếu áp dụng lại đúng 4 trục này cho HOW / WHY:

1. canonical query result
2. phase-based query plan
3. compact prompt payload
4. contract-level tests

thì phần refactor tiếp theo sẽ nhanh hơn rất nhiều và ít rủi ro hơn so với tiếp tục sửa rời rạc từng file.
