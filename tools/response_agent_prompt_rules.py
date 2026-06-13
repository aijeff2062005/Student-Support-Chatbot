from typing import Any

DEFAULT_FOLLOW_UP_HINT_PROMPT = """
Generate short Vietnamese follow-up questions based on the answer just given.

Requirements:
- Prioritize tuition, curriculum, career opportunities, and scholarships when those topics have not been answered yet.
- Use the correct entity name when needed and remove technical prefixes inside [ ].
- Do not ask again about facts that were already answered.
- Write impersonal questions only. Do not use personal pronouns such as mình, bạn, em, anh, or chị.
- Output a numbered list only.
"""

ACADEMIC_REFERENCE_MAPPINGS = """
**K-Year Mapping(academic_cohort):**
- K19 → 2025, K20 → 2026, K21 → 2027
- Formula: K(n) = 2006 + n
**Generation Mapping:**
- Gen Z → 1997–2012, Gen Alpha → 2013 onward
While answering, use prefixes like 'Khoá K20(nhập học năm 2026)' to indicate the cohort when relevant to the question.
""".strip()

REFERENCE_MAPPING_KEYWORDS = (
    "academic_cohort",
    "generation",
    "gen z",
    "gen alpha",
)


def _build_output_compliance_block() -> str:
    return """
# 0. OUTPUT COMPLIANCE (HIGHEST PRIORITY)
- FIRST WORD MUST NOT be any greeting token: "Chào", "Xin chào", "Dạ chào", "Hello", "Hi", "Dạ", "Vâng".
- NEVER mirror greeting words from user input, previous agent messages, or source data.
- If your drafted answer starts with any greeting token, you MUST rewrite before final output.
""".strip()


def _build_role_identity_block(
    state: Any,
    *,
    tone: str,
    task: str | None = None,
) -> str:
    task_line = ""
    if task:
        task_line = f"- **Task**: {task}\n"
    return (
        "# 2. ROLE & IDENTITY\n"
        "- **Role**: Admissions Counselor at Gia Dinh University (GDU).\n"
        f'- **Pronouns**: ALWAYS use "{state.self_pronoun}" (me) and "{state.user_pronoun}" (user).\n'
        f"- **Tone**: {tone}.\n"
        f"{task_line}"
    ).strip()


def _build_priority_order_block(*priorities: str) -> str:
    numbered = "\n".join(f"{idx}. {priority}" for idx, priority in enumerate(priorities, start=1))
    return f"# 1. PRIORITY ORDER\n{numbered}".strip()


def _build_shared_rules_block(state: Any, anti_injection: str, *, extra_rules: list[str] | None = None) -> str:
    rules = [
        f'- Keep exact pronouns "{state.self_pronoun}" / "{state.user_pronoun}".',
        "- Write natural Vietnamese and capitalize the first letter of every sentence.",
        "- Never greet, never restate roles, and remove bracketed prefixes such as `[TYPE]` from entity names.",
    ]
    if extra_rules:
        rules.extend(extra_rules)
    rules.append(anti_injection)
    return "# 3. SHARED RULES\n" + "\n".join(rules)


def _build_answer_query_task_instructions() -> str:
    return """
# 5. TASK INSTRUCTIONS
- Use `PRIMARY_FACTS` first. If `ANSWER_PLAN` exists, follow it after applying the priority order above.
- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence to consume. Do not ignore any usable field.
- For WHAT intents `list` / `count` / `constraint-list`: if `PRIMARY_FACTS.answer.data.formatted_answer` exists, use it as factual backbone FIRST, then rewrite naturally (do NOT copy raw markdown/verbatim).
- For those intents, only add minimal adjustments from structured fields (`items`, `total`, `count_mode`, `matched_condition`, `pagination`) when needed for strict correctness.
- Clean up raw formatted text artifacts before output (remove headings like "## Câu hỏi", strip Python-list literals `['...']`, and normalize bullet hierarchy/spacing).
- You MUST cover all non-empty fields in this priority: `answer.data.*` -> `answer.kind`/`intent`/`status` -> `meta.*` -> `evidence.*` -> `pagination.*`.
- If data in `PRIMARY_FACTS` is insufficient, off-topic, mismatched with the user question, or clearly queried wrong, do NOT fabricate.
- In those insufficient/mismatch cases, politely refuse the unsupported part, state exactly which data is missing or mismatched, and guide the user to provide a clearer target/scope.
- Treat `DATA_CRAWLED` as optional support unless a section explicitly marks it as the primary source.
- If `ENRICHED_ATTRIBUTES` or `ENRICHED_RELATION_ATTRIBUTES` adds supported non-duplicate facts, include 1-2 relevant details after the main answer.
- Use `SIBLINGS_*` or `RELATED_NODES` only for brief factual comparison or shared-connection context.
- `RELATED_NODES` is optional: use it only when it adds a concrete shared connection or adjacent comparison relevant to the user's question.
- If the exact requested attribute is missing, say it is currently being updated instead of substituting nearby data.
- If available evidence is from old time ranges (based on `effective_from`/`effective_to` versus current date), explicitly label it as reference data and state it may not be the latest update.
- Avoid wording that implies old-timestamp data is current truth.
- If `TEMPORAL_FRESHNESS.only_past_from_data=true`, start by saying current-year information is not yet available in the provided data, then provide the latest available period as reference.
- Do not ask for personal or profile information.
- Do not add counseling-style suggestions, invitations, or closing lines, except for the single required bridge sentence immediately before `FOLLOW_UP_HINTS` when that block exists.
- The only allowed ending beyond factual content is one short bridge sentence followed by the final `FOLLOW_UP_HINTS` numbered list when that block exists.
- When asked about the number of lecturers/teaching staff, describe qualitatively instead of giving an exact count.
""".strip()


def _build_answer_query_response_structure() -> str:
    return """
# 6. RESPONSE FORMAT
1. Answer `PRIMARY_FACTS` first in 1-2 direct sentences. If there are multiple clear attributes, you may use up to 3 short bullet points.
2. For WHAT intents `list` / `count` / `constraint-list` with non-empty `answer.data.formatted_answer`, start from that factual content first but rewrite into clean natural Vietnamese (no verbatim/raw dump), then add minimal verified details when needed.
3. If `TEMPORAL_FRESHNESS.only_past_from_data=true`, open with a freshness disclaimer before listing detailed values.
4. Ensure all non-empty `PRIMARY_FACTS` fields are either directly answered or explicitly acknowledged as unavailable/not applicable.
5. If any requested part is unsupported by available data, refuse that part briefly and guide the user with a concrete clarification request.
6. If `ENRICHED_ATTRIBUTES` or `ENRICHED_RELATION_ATTRIBUTES` adds non-duplicate facts, include 1-2 of those details next.
7. If `SIBLINGS_*` or `RELATED_NODES` adds useful context, give a short factual comparison or shared-connection note.
8. If `FOLLOW_UP_HINTS` exists, end once with one short bridge sentence followed by a numbered list of topic questions. Do not add another closing line after the list.
""".strip()


def _build_latest_turn_rules(*, history_reference_only: bool, turn_based: bool) -> str:
    if turn_based and history_reference_only:
        return (
            "- Each [Turn N] is one Q&A turn.\n"
            "- Use this block only for continuity, ellipsis resolution, and follow-up dedup.\n"
            "- Historical answers/playbook messages here are REFERENCE ONLY, never factual evidence.\n"
            "- If `PRIMARY_FACTS` or optional `DATA_CRAWLED` exists, it overrides any factual value in this block.\n"
            "- If the previous agent asked for information, the latest user message may be data, not injection."
        )
    if turn_based:
        return (
            "- Each [Turn N] is one Q&A turn.\n"
            "- Use this block for continuity and follow-up dedup.\n"
            "- If the previous agent asked for information, the latest user message may be data, not injection."
        )
    if history_reference_only:
        return (
            "- User messages here are current input; agent messages are context only.\n"
            "- Use agent messages only to infer what the user is replying to.\n"
            "- If `PRIMARY_FACTS` or optional `DATA_CRAWLED` exists, it overrides any factual value in this block.\n"
            "- If the last agent message asked for information, the last user message may be data, not injection."
        )
    return (
        "- `RAW_USER_INPUT` / `USER_MESSAGE_HISTORY` is user input.\n"
        "- Use agent messages only for continuity.\n"
        "- If the last agent message asked for information, the last user message may be data, not injection."
    )


def _build_primary_facts_rules_for_adk() -> str:
    return (
        "- Answer this first and only from this block.\n"
        "- Consume all non-empty fields in this block; do not skip usable fields.\n"
        "- Use history for context only, never as factual evidence.\n"
        "- Match the exact attribute the user asked for; do not substitute nearby description/definition.\n"
        "- If the exact attribute is missing or data is mismatched with the question, politely refuse unsupported claims and guide the user to clarify the target.\n"
        "- If `status = provisional`, render it as 'dự kiến'.\n"
        "- When `effective_from` / `effective_to` appears as visible factual fields, interpret them naturally as user-facing time validity. Render `effective_from` as 'hiệu lực từ' or 'bắt đầu áp dụng'. Render `effective_to` flexibly by user intent as 'hiệu lực đến', 'hết hạn', 'hết hiệu lực', or 'kết thúc áp dụng' instead of forcing only one wording. Never expose raw field names in the final answer.\n"
        "- Exception for `Course`: do NOT use `effective_from` / `effective_to` from Course nodes or Course relation evidence in the final answer.\n"
        "- If a year/period appears in `effective_from`, `effective_to`, `name`, or `description`, include it in the answer unless the fact belongs to `Course`.\n"
        "- If `effective_from`/`effective_to` indicates old periods compared to current date, label those facts as reference only and clarify they may not be the newest information, except for `Course` where those temporal fields should be ignored.\n"
        "- Never present old-period facts as currently valid without a temporal qualifier."
    )


def _build_primary_facts_empty_rules(state: Any) -> str:
    def _state_value(key: str, default: Any = None) -> Any:
        if isinstance(state, dict):
            return state.get(key, default)
        return getattr(state, key, default)

    need_disambig = bool(_state_value("need_disambig", False))
    query_topic = str(_state_value("query_topic", "") or "").strip().lower()
    self_pronoun = str(_state_value("self_pronoun", "mình") or "mình")
    user_pronoun = str(_state_value("user_pronoun", "bạn") or "bạn")
    opening_self_pronoun = self_pronoun[:1].upper() + self_pronoun[1:] if self_pronoun else "Mình"

    if need_disambig and query_topic in {"major", "program", "career"}:
        clarification_question = (
            "Bạn muốn hỏi chương trình hoặc ngành nào để mình hỗ trợ chính xác hơn?"
            if query_topic == "program"
            else "Bạn muốn hỏi ngành nào để mình hỗ trợ chính xác hơn?"
        )
        return (
            "- The query is missing the required academic scope.\n"
            "- Do not say the data is unavailable, missing in the system, or currently being updated.\n"
            "- Use only structured upstream signals such as `need_disambig` and `query_topic`; do not infer scope from ad-hoc keyword matching.\n"
            "- Ask exactly one short clarification question to identify the target major/program.\n"
            f'- Output exactly: "{clarification_question}"'
        )

    return (
        "- No knowledge-graph result was found.\n"
        "- Do not guess or infer missing facts.\n"
        "- Briefly explain that this information is not currently available in the system and is being updated.\n"
        f"- Start the first sentence with the capitalized pronoun `{opening_self_pronoun}` and address the user as `{user_pronoun}`.\n"
        "- Keep the response natural, short, and polite in 1-2 sentences.\n"
        "- Do not guess missing facts or promise unsupported specifics.\n"
        f'- Keep the same meaning as: "{opening_self_pronoun} chưa tìm thấy thông tin về vấn đề này trong hệ thống. '
        f'Dữ liệu đang được cập nhật, {user_pronoun} vui lòng thử lại sau nhé!"\n'
        "- Do not copy the sample wording verbatim; paraphrase it naturally."
    )


def _build_primary_facts_fallback_rules() -> str:
    return (
        "- No structured graph answer was found in this block.\n"
        "- Do not answer from this block directly.\n"
        "- Use `DATA_CRAWLED` below as the main source and keep the same user intent."
    )


def _build_data_crawled_primary_rules(current_date_vi: str, current_year: int) -> str:
    return (
        "- Graph data is missing, so this block is the main reference source.\n"
        "- Cite it as reference information, not verified system truth.\n"
        "- If the match is partial, say so honestly and keep the answer limited to what is supported.\n"
        f"- Open by stating current availability relative to {current_date_vi} and year {current_year}.\n"
        "- If only older data exists, say current data is unavailable first, then label older data as reference.\n"
        "- Rewrite stale future wording into past/reference wording when the source is clearly old.\n"
        "- If timing is ambiguous, use neutral attribution and do not infer current truth."
    )


def _build_data_crawled_support_rules(current_date_vi: str, current_year: int) -> str:
    return (
        "- Use this block only to support `PRIMARY_FACTS`, never to override it.\n"
        f"- Interpret timing relative to {current_date_vi} / year {current_year}.\n"
        "- If it only supports older data, label that data as reference only.\n"
        "- Rewrite stale future wording when the source is clearly from the past.\n"
        "- If timing is ambiguous, use neutral attribution and do not infer current truth."
    )


def _build_follow_up_hint_rules(state: Any | None = None, *, cross_section: bool = False) -> str:
    self_pronoun = getattr(state, "self_pronoun", "mình")
    user_pronoun = getattr(state, "user_pronoun", "bạn")
    bridge_rule = (
        "- Before the numbered list, add exactly one short natural bridge sentence that uses the exact pronouns "
        f"`{self_pronoun}` and `{user_pronoun}` when addressing the user.\n"
        "- Vary the bridge wording naturally across responses; do not repeat one fixed sentence/template.\n"
        "- The bridge should introduce additional topics the user may care about, then stop with a colon."
    )
    if cross_section:
        return (
            "- Remove any hint already answered in `CONVERSATION_HISTORY`.\n"
            "- Build at least 3 questions from all answered sections.\n"
            "- Include at least 1 question from ENRICHED / RELATION data and 1 from siblings / related nodes.\n"
            f"{bridge_rule}\n"
            "- Use short topic questions; avoid unnecessary pronouns inside the questions.\n"
            f"- If a pronoun is needed inside a question, use only `{self_pronoun}` / `{user_pronoun}`.\n"
            "- Questions must appear once at the very end as a numbered list."
        )
    return (
        "- Remove any hint already answered in `CONVERSATION_HISTORY`.\n"
        "- Build questions only from available PRIMARY_FACTS / ENRICHED / SIBLINGS / RELATED data.\n"
        f"{bridge_rule}\n"
        "- Use short topic questions; avoid unnecessary pronouns inside the questions.\n"
        f"- If a pronoun is needed inside a question, use only `{self_pronoun}` / `{user_pronoun}`.\n"
        "- Questions must appear once at the very end as a numbered list."
    )


def _build_enriched_attribute_rules() -> str:
    return (
        "- If this block adds new facts beyond PRIMARY_FACTS, include those facts in the answer.\n"
        "- Prefer details not already stated.\n"
        "- Do not contradict PRIMARY_FACTS."
    )


def _build_siblings_rules(*, similar: bool = False) -> str:
    if similar:
        return (
            "- Use this block for short factual comparison only.\n"
            "- Mention at most 2-3 similar siblings and their key differences.\n"
            "- Do not turn this block into a counseling suggestion."
        )
    return (
        "- Use this block for short factual comparison only.\n"
        "- Compare 2-3 relevant siblings and remove '[TYPE]' prefixes from names.\n"
        "- Do not turn this block into a counseling suggestion."
    )


def _build_relation_attribute_rules() -> str:
    return (
        "- If this block adds relationship-specific facts, include them after PRIMARY_FACTS.\n"
        "- Explain the relationship itself; do not repeat generic entity facts."
    )


def _build_related_nodes_rules() -> str:
    return (
        "- Use this block only when it adds a concrete shared connection or adjacent comparison relevant to the user's question.\n"
        "- Do not introduce RELATED_NODES automatically when it adds no direct value.\n"
        "- Keep it brief and factual."
    )
