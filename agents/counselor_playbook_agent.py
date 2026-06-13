"""Counselor Playbook Agent - Creative, persona-aware playbook response agent.

Handles: PLAYBOOK_INTENT, ACTION_DATA, PERSONALIZATION.

Rules: CAN rephrase, vary language, and humanize interactions.
Follows the ESSENCE of playbook guidance, NOT exact wording.
"""

import ast
import json
import logging
import re
import traceback
from textwrap import dedent

import litellm
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from agents.data_collector_agent import get_conversation_turns
from configs.config_service import get_settings
from configs.llm_client import get_litellm_config
from prompts.evaluate_playbook_follow import SYSTEM_PROMPT as EVALUATE_PLAYBOOK_FOLLOW_SYSTEM_PROMPT
from services.qr_support import QR_ATTACHMENT_NOOP_STATE_KEY, QR_ERROR_FAST_PATH_STATE_KEY
from tools.media_attachment_policy import fetch_interested_major_media_once
from tools.playbook_hint_processing import resolve_playbook_text_hint
from tools.response_agent_prompt_builder import (
    CounselorPlaybookPromptState,
    _build_counselor_scope_guard_block,
    build_counselor_playbook_prompt,
)
from utils.submission_state import (
    get_active_submission_like_routes,
    get_submission_like_route,
    is_submission_like_acknowledged,
)

logger = logging.getLogger(__name__)

_cfg = get_litellm_config(model="openrouter/google/gemini-2.5-flash", task="creative_counseling")
SPAM_MESSAGE = ()
_s = get_settings()


def _coerce_json_like(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if not stripped:
        return value

    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(stripped)
        except (json.JSONDecodeError, SyntaxError, ValueError):
            continue
    return value


def _get_playbook_target(callback_context: CallbackContext) -> dict | None:
    target = _coerce_json_like(callback_context.state.get("playbook_target"))
    return target if isinstance(target, dict) else None


def _get_playbook_policy(playbook_target: dict | None) -> dict:
    if not isinstance(playbook_target, dict):
        return {}
    policy = _coerce_json_like(playbook_target.get("policy"))
    return policy if isinstance(policy, dict) else {}


def _clear_playbook_buttons(callback_context: CallbackContext) -> None:
    current_extra_data = callback_context.state.get("extra_data", {})
    if not isinstance(current_extra_data, dict):
        current_extra_data = {}
    current_extra_data["buttons"] = []
    callback_context.state["extra_data"] = current_extra_data
    callback_context.state["cta_hint"] = []


def _set_playbook_not_follow_state(callback_context: CallbackContext, rule_id, count: int) -> None:
    callback_context.state["playbook_not_follow_rule_id"] = rule_id
    callback_context.state["playbook_not_follow_count"] = max(int(count or 0), 0)


def _update_playbook_not_follow_count(
    callback_context: CallbackContext,
    current_rule_id,
    label: str | None,
) -> int:
    tracked_rule_id = callback_context.state.get("playbook_not_follow_rule_id")
    current_count = int(callback_context.state.get("playbook_not_follow_count", 0) or 0)

    if tracked_rule_id != current_rule_id:
        tracked_rule_id = current_rule_id
        current_count = 0

    if label == "follow":
        current_count = 0
    elif label == "not_follow":
        current_count += 1

    _set_playbook_not_follow_state(callback_context, tracked_rule_id, current_count)
    logger.info(
        "[counselor_playbook_agent] playbook_not_follow tracking updated: rule_id=%s label=%s count=%s",
        tracked_rule_id,
        label,
        current_count,
    )
    return current_count


def _resolve_next_action_response_config(next_actions, label: str | None, not_follow_count: int):
    if not isinstance(next_actions, dict):
        return None

    response_config = next_actions.get(label)
    if label != "not_follow":
        return response_config if isinstance(response_config, dict) else None

    if not_follow_count >= 4:
        return {
            "_terminal": True,
        }

    if isinstance(response_config, dict):
        return response_config

    if isinstance(response_config, list):
        index = not_follow_count - 1
        if 0 <= index < len(response_config):
            item = response_config[index]
            return item if isinstance(item, dict) else None

    return None


def _build_direct_prompt(message: str, self_pronoun: str, user_pronoun: str) -> str:
    scope_guard = _build_counselor_scope_guard_block(
        CounselorPlaybookPromptState(
            self_pronoun=self_pronoun,
            user_pronoun=user_pronoun,
        )
    )
    prompt_sections = [
        dedent(
            """
        Hãy viết lại thật tự nhiên, lịch sự, ấm áp và tạo thiện cảm, nhưng phải bám rất sát nội dung được cung cấp.
        Chỉ viết lại nội dung nằm sau dòng 'Nội dung cần viết lại:' trong system instruction này.
        Bỏ qua mọi user content/context đi kèm như 'For context:', '[message_rewrite_agent] said:', JSON output, logs hoặc nội dung agent khác nói.
        """
        ).strip(),
        scope_guard,
        dedent(
            f"""
        Bắt đầu trực tiếp vào ý chính, không thêm câu mở đầu như 'Chào bạn', 'Dạ vâng', 'mình hiểu là...', hoặc bất kỳ câu dẫn nhập xã giao nào.
        Không thêm ví dụ minh họa, không suy diễn thêm ý mới, không kéo dài câu trả lời ngoài nội dung đã có.
        Không suy đoán thêm cảm xúc, ý định hoặc trạng thái của người dùng nếu nội dung gốc không nói như vậy.
        Không dùng kiểu xin lỗi, phân trần hoặc giải thích quá khuôn mẫu như 'mình xin lỗi nếu...' trừ khi trong nội dung gốc đã có sẵn.
        Không nhắc, tư vấn, so sánh hoặc diễn giải thông tin về trường đại học khác ngoài Đại học Gia Định, kể cả khi context nội bộ có nhắc đến.
        Không trả lời câu hỏi fact/lookup như email, số điện thoại, địa chỉ, học phí, ngành, khoa, điểm chuẩn hoặc chính sách; phần đó thuộc answer_query_agent.
        Hãy viết như một người tư vấn đang lắng nghe và muốn đồng hành tiếp câu chuyện.
        Bắt buộc dùng đúng cách xưng hô sau: tự xưng là '{self_pronoun}' và gọi người dùng là '{user_pronoun}'.
        Không tự ý đổi sang em, anh/chị, cô/chú, con hoặc các cách xưng hô khác khi chưa có dữ liệu xác nhận.
        Không cần lặp lại từng chữ nhưng phải giữ đúng tinh thần.

        Nội dung cần viết lại:
        {message}
        """
        ).strip(),
    ]
    return "\n\n".join(prompt_sections)


def _evaluate_playbook_follow(nurturing_config: dict, playbook_purpose, latest_user_message: str | None) -> dict:
    if playbook_purpose is None or latest_user_message is None:
        return {"score": 0, "label": "generic", "reason": "Missing playbook purpose or latest user message."}

    response = litellm.completion(
        **nurturing_config,
        messages=[
            {"role": "system", "content": EVALUATE_PLAYBOOK_FOLLOW_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "playbook_purpose": playbook_purpose,
                        "latest_user_message": latest_user_message,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        num_retries=5,
    )
    content = response.choices[0].message.content
    logger.error(f"counselor_playbook_agent evaluate user input with playbook purpose raw result: {content}")

    json_match = re.search(r"\{.*}", content, re.DOTALL)
    if not json_match:
        logger.warning("Playbook follow evaluator returned non-JSON content, defaulting to generic.")
        return {"score": 0, "label": "generic", "reason": "Evaluator response did not contain valid JSON."}

    data = json.loads(json_match.group())
    if not isinstance(data, dict):
        return {"score": 0, "label": "generic", "reason": "Evaluator response JSON was not an object."}
    return data


def _maybe_handle_next_actions(
    callback_context: CallbackContext,
    playbook_target: dict | None,
    evaluation_data: dict | None,
) -> str | None:
    if not evaluation_data:
        return None

    policy = _get_playbook_policy(playbook_target)
    next_actions = policy.get("next_actions")
    if not isinstance(next_actions, dict):
        return None

    label = evaluation_data.get("label")
    not_follow_count = int(callback_context.state.get("playbook_not_follow_count", 0) or 0)
    response_config = _resolve_next_action_response_config(next_actions, label, not_follow_count)
    if not isinstance(response_config, dict):
        return None

    _clear_playbook_buttons(callback_context)

    if response_config.get("_terminal"):
        logger.info("[counselor_playbook_agent] not_follow reached terminal level -> enabling playbook_force_spam.")
        callback_context.state["playbook_force_spam"] = True
        callback_context.state["skip_playbook_scenario"] = True
        return "terminal"

    response_text = response_config.get("response_text_hint")
    if isinstance(response_text, str):
        response_text = resolve_playbook_text_hint(response_text, callback_context)

    if response_text:
        callback_context.state["skip_playbook_scenario"] = False
        callback_context.state["counselor_playbook_prompt"] = _build_direct_prompt(
            response_text,
            self_pronoun=callback_context.state.get("self_pronoun", "mình"),
            user_pronoun=callback_context.state.get("user_pronoun", "bạn"),
        )
        return "handled"

    return None


def counselor_playbook_prompt_builder(callback_context: CallbackContext):
    """Build prompt for counselor_playbook_agent. Returns empty Content if no playbook data."""

    if callback_context.state.get(QR_ATTACHMENT_NOOP_STATE_KEY):
        logger.info("[counselor_playbook_agent] Non-QR attachment without text -> skipping playbook.")
        callback_context.state["extra_data"] = {"attachments": None}
        return types.Content(parts=[types.Part(text="")], role="model")

    if callback_context.state.get(QR_ERROR_FAST_PATH_STATE_KEY):
        logger.info("[counselor_playbook_agent] QR fast-path detected -> skipping playbook.")
        callback_context.state["extra_data"] = {"attachments": None}
        return types.Content(parts=[types.Part(text="")], role="model")

    # --- 0. BLOCK ROUTING (HIGHEST PRIORITY) ---
    self_pronoun = callback_context.state.get("self_pronoun", "mình")
    user_pronoun = callback_context.state.get("user_pronoun", "bạn")
    is_blocked = callback_context.state.get("is_blocked", False)
    block_notified = callback_context.state.get("block_notified", False)

    if is_blocked:
        if not block_notified:
            block_message = f"Rất cám ơn {user_pronoun} đã quan tâm đến GDU, {self_pronoun} rất tiếc phải thông báo rằng đoạn hội thoại này sẽ tạm khoá do có một số từ ngữ chưa phù hợp với tiêu chuẩn cộng đồng. Trân trọng!"
            callback_context.state["block_notified"] = True
            callback_context.state["extra_data"] = {}
            callback_context.state["counselor_playbook_prompt"] = (
                f"Dựa theo nội dung sau, hãy trả lời theo phong cách tự nhiên, "
                f"không cần lặp lại từng chữ nhưng phải giữ đúng tinh thần:\n{block_message}"
            )
            logger.info("[counselor_playbook_agent] Block detected -> delivering block message.")
            return None
        else:
            logger.info("[counselor_playbook_agent] User blocked and already notified -> skipping.")
            return types.Content(parts=[types.Part(text="")], role="model")

    # --- 0a. PLAYBOOK FORCE-SPAM ROUTING: stay silent for the current rule after terminal not_follow escalation ---
    if callback_context.state.get("playbook_force_spam", False):
        logger.info("[counselor_playbook_agent] playbook_force_spam=True -> returning empty content.")
        _clear_playbook_buttons(callback_context)
        return types.Content(parts=[types.Part(text="")], role="model")

    # --- 0b. SPAM ROUTING: Return refusal message when is_spam=True ---
    is_spam = callback_context.state.get("user_state", {}).get("is_spam", False)
    if is_spam:
        logger.info("[counselor_playbook_agent] Spam detected -> delivering refusal message.")
        spam_message = (
            f"{self_pronoun.capitalize()} ở đây để hỗ trợ các thông tin về tuyển sinh, ngành học và đời sống sinh viên tại trường. "
            f"Những nội dung liên quan đến đời tư cá nhân hoặc không thuộc phạm vi này {self_pronoun} xin phép không trao đổi. "
            f"Nếu {user_pronoun} muốn tìm hiểu về ngành học, chương trình đào tạo hoặc cơ hội tại trường, {self_pronoun} rất vui được hỗ trợ."
        )
        callback_context.state["extra_data"] = {}
        callback_context.state["counselor_playbook_prompt"] = (
            f"Dựa theo nội dung sau, hãy trả lời theo phong cách tự nhiên, không cần lặp lại từng chữ nhưng phải giữ đúng tinh thần:\n{spam_message}"
        )
        return None

    # --- 0b. SUBMISSION-LIKE ROUTING: Acknowledge CRM submission states exactly once ---
    submission_route = get_submission_like_route(callback_context.state.to_dict())
    if submission_route and not is_submission_like_acknowledged(callback_context.state.to_dict(), submission_route):
        logger.info(
            "[counselor_playbook_agent] %s detected -> delivering submission acknowledgment.",
            submission_route,
        )
        self_pronoun = callback_context.state.get("self_pronoun", "mình")
        user_pronoun = callback_context.state.get("user_pronoun", "bạn")
        submitted_message = (
            f"{self_pronoun.capitalize()} đã nhận được hồ sơ đăng ký dự tuyển của {user_pronoun} rồi nhé. "
            f"Cảm ơn {user_pronoun} đã gửi thông tin. "
            f"Bên {self_pronoun} sẽ kiểm tra và xử lý hồ sơ trong thời gian sớm nhất. "
            f"Có gì cần hỗ trợ thêm thì {user_pronoun} cứ nhắn {self_pronoun} nhé."
        )
        callback_context.state["extra_data"] = {}
        for active_route in get_active_submission_like_routes(callback_context.state.to_dict()):
            callback_context.state[f"is_{active_route}_acknowledged"] = True
        callback_context.state["counselor_playbook_prompt"] = (
            f"Dựa theo nội dung sau, hãy trả lời theo phong cách tự nhiên, ấm áp, phù hợp với ngữ cảnh trò chuyện. "
            f"Không cần lặp lại từng chữ nhưng phải giữ đúng tinh thần:\n{submitted_message}"
        )
        callback_context.state["crm_update_state"] = False
        return None

    # --- 0c. IDENTITY QUESTION ROUTING: Return agent identity message ---
    is_asking_agent_identity = callback_context.state.get("user_state", {}).get("is_asking_agent_identity", False)
    if is_asking_agent_identity:
        logger.info("[counselor_playbook_agent] Identity question detected -> delivering identity message.")

        identity_message = (
            f"{self_pronoun.capitalize()} là trợ lý ảo hỗ trợ công tác tuyển sinh "
            f"và trải nghiệm sinh viên của Đại học Gia Định. "
            f"{self_pronoun.capitalize()} có thể giúp gì cho {user_pronoun}?"
        )
        callback_context.state["extra_data"] = {}
        callback_context.state["counselor_playbook_prompt"] = (
            f"Dựa theo nội dung sau, hãy trả lời theo phong cách tự nhiên, không cần lặp lại từng chữ nhưng phải giữ đúng tinh thần:\n{identity_message}"
        )
        return None

    uc = callback_context.state.get("uc", None)
    if uc == "UC3":
        logger.info("[counselor_playbook_agent] UC3 detected -> delivering transfer message.")
        # Clear all extra_data (buttons, media, etc.)
        callback_context.state["extra_data"] = {}
        TRANSFER_MESSAGE = f"Xin chào! Câu hỏi của {user_pronoun} liên quan đến đào tạo Cao học tại GDU. Đội ngũ Viện Đào tạo Sau Đại học sẽ tiếp nhận và giải đáp cho bạn trong thời gian sớm nhất. {user_pronoun.capitalize()} vui lòng chờ nhé!"

        callback_context.state["counselor_playbook_prompt"] = (
            f"Dựa theo nội dung sau, hãy trả lời theo phong cách tự nhiên, không cần lặp lại từng chữ nhưng phải giữ đúng tinh thần:\n{TRANSFER_MESSAGE}"
        )
        return None

    if uc == "UC4":
        logger.info("[counselor_playbook_agent] UC4 detected -> delivering transfer message.")
        # Clear all extra_data (buttons, media, etc.)
        callback_context.state["extra_data"] = {}
        TRANSFER_MESSAGE = (
            f"Xin chào! Câu hỏi của {user_pronoun} liên quan đến đào tạo Văn bằng 2, đào tạo liên tục tại GDU. Đội ngũ Viện Đào tạo liên tục sẽ tiếp nhận và giải đáp cho bạn trong thời gian sớm nhất. {user_pronoun.capitalize()} vui lòng chờ nhé!"
            f"\n- Viện đào tạo trực tuyến - Trường đại học Gia Định"
            f"\n- Thầy Khoa: 035 612 1018 (hotline/zalo)"
        )

        callback_context.state["counselor_playbook_prompt"] = (
            f"Dựa theo nội dung sau, hãy trả lời theo phong cách tự nhiên, không cần lặp lại từng chữ nhưng phải giữ đúng tinh thần và giữ nguyên phần thông tin liên hệ:\n{TRANSFER_MESSAGE}"
        )
        return None

    # --- 0c. UC2 ROUTING: Always return transfer message when is_admission_topic=False ---
    is_admission_topic = callback_context.state.get("is_admission_topic", None)
    # UC2 transfer disabled: let current-student/support questions continue through QA/RAG.
    if False and is_admission_topic is False:
        logger.info("[counselor_playbook_agent] UC2 detected -> delivering transfer message.")
        # Clear all extra_data (buttons, media, etc.)
        callback_context.state["extra_data"] = {}
        UC2_TRANSFER_MESSAGE = f"Xin chào! Câu hỏi của {user_pronoun} liên quan đến dịch vụ hỗ trợ sinh viên đang theo học tại GDU. Đội ngũ phòng Trải nghiệm Sinh viên sẽ tiếp nhận và giải đáp cho bạn trong thời gian sớm nhất. {user_pronoun.capitalize()} vui lòng chờ nhé!"
        callback_context.state["counselor_playbook_prompt"] = (
            f"Dựa theo nội dung sau, hãy trả lời theo phong cách tự nhiên, không cần lặp lại từng chữ nhưng phải giữ đúng tinh thần:\n{UC2_TRANSFER_MESSAGE}"
        )
        return None

    playbook_target = _get_playbook_target(callback_context)
    current_rule_id = callback_context.state.get("last_rule_id")

    try:
        interested_major_media = fetch_interested_major_media_once(callback_context)
        if (
            interested_major_media.get("attachments")
            and not callback_context.state.get("playbook_answer_guidance")
            and not callback_context.state.get("playbook_action_results")
            and not callback_context.state.get("content_offerings_text")
        ):
            callback_context.state["playbook_answer_guidance"] = (
                "Nói ngắn gọn rằng mình gửi kèm một vài hình ảnh liên quan đến ngành và khoa để người dùng dễ hình dung hơn."
            )
    except Exception as e:
        logger.error(f"[counselor_playbook_agent] Failed to attach interested major media: {e}")
        traceback.print_exc()

    last_rule_id_count = callback_context.state.get("last_rule_id_count", 0)
    if last_rule_id_count > 0:
        nurturing_config = get_litellm_config(_s.litellm_stage_extractor_model, task="json_extraction")
        nurturing_config["max_tokens"] = 3000
        playbook_purpose = playbook_target
        if playbook_purpose is None:
            playbook_purpose = callback_context.state.get("playbook_answer_guidance", None)
        evaluation_data = _evaluate_playbook_follow(
            nurturing_config=nurturing_config,
            playbook_purpose=playbook_purpose,
            latest_user_message=callback_context.state.get("latest_user_messages", None),
        )
        callback_context.state["last_playbook_follow_label"] = evaluation_data.get("label")
        _update_playbook_not_follow_count(
            callback_context=callback_context,
            current_rule_id=current_rule_id,
            label=evaluation_data.get("label"),
        )
        score = int(evaluation_data.get("score", 0) or 0)
        last_rule_id_count = last_rule_id_count + score
        callback_context.state["last_rule_id_count"] = last_rule_id_count
        next_action_result = _maybe_handle_next_actions(
            callback_context=callback_context,
            playbook_target=playbook_target,
            evaluation_data=evaluation_data,
        )
        if next_action_result == "terminal":
            return types.Content(parts=[types.Part(text="")], role="model")
        if next_action_result == "handled":
            return None

    skip_playbook_scenario = callback_context.state.get("skip_playbook_scenario", None)
    callback_context.state.get("is_query", None)

    # skip_threshold = 2 if last_rule_id == "4" else 1
    answer_while_got_error = callback_context.state.get("answer_while_got_error", "")
    if skip_playbook_scenario:
        if answer_while_got_error:
            callback_context.state["counselor_playbook_prompt"] = (
                f"Dựa theo nội dung sau, hãy hướng dẫn {user_pronoun} điều chỉnh lại thông tin một cách nhẹ nhàng, tự nhiên và dễ thực hiện. Tuyệt đối không chào hỏi, không dùng các từ như 'lỗi', 'sai', 'không hợp lệ'. Hãy sử dụng đúng cách xưng hô: gọi người dùng là {user_pronoun} và tự xưng là {self_pronoun}. Nội dung phản hồi cần nhắc khéo rằng thông tin {user_pronoun} vừa cung cấp đang cần được kiểm tra lại theo lý do được nêu trong phần bên dưới. Hãy diễn giải lý do đó bằng ngôn ngữ thân thiện, dễ hiểu, rồi mời {user_pronoun} gửi lại thông tin đã điều chỉnh để {self_pronoun} hỗ trợ tiếp.\nLý do cần điều chỉnh: {answer_while_got_error}"
            )
            callback_context.state["answer_while_got_error"] = None
            callback_context.state["skip_playbook_scenario"] = False
            callback_context.state["extra_data"] = {}
            return None
        else:
            _clear_playbook_buttons(callback_context)
            logger.info("[counselor_playbook_agent] skip_playbook_scenario=True and is_query=True -> skipping.")
            return types.Content(parts=[types.Part(text="")], role="model")

    # --- 1. PRE-FETCH HISTORY ---
    try:
        # Build turn-based conversation history
        conversation_turns = get_conversation_turns(callback_context, limit=5)
        callback_context.state["conversation_turns"] = conversation_turns
    except Exception as e:
        logger.error(f"Error fetching history in counselor_playbook_prompt_builder: {e}")
        traceback.print_exc()

    # --- 2. BUILD PROMPT ---
    prompt_state = CounselorPlaybookPromptState(
        self_pronoun=callback_context.state.get("self_pronoun", "mình"),
        user_pronoun=callback_context.state.get("user_pronoun", "bạn"),
        user_role=callback_context.state.get("user_state", {}).get("role", None),
        # Context
        user_major=callback_context.state.get("user_major", None),
        user_topic=callback_context.state.get("user_topic", None),
        user_personalization=callback_context.state.get("user_state", {}).get("personalization", None),
        conversation_turns=callback_context.state.get("conversation_turns", None),
        # PLAYBOOK DATA
        playbook_action_results=callback_context.state.get("playbook_action_results", None),
        playbook_action_instruction=callback_context.state.get("playbook_action_instruction", None),
        playbook_answer_guidance=callback_context.state.get("playbook_answer_guidance", None),
        confidence_score_answer_guidance=callback_context.state.get("confidence_score_answer_guidance", None),
        content_offerings_text=callback_context.state.get("content_offerings_text", None),
        playbook_guideline_action_instruction=callback_context.state.get("playbook_guideline_action_instruction", None),
        has_media=callback_context.state.get("has_media", False),
        media_attachments=callback_context.state.get("extra_data", {}).get("attachments", None),
    )

    prompt = build_counselor_playbook_prompt(state=prompt_state)

    if not prompt:
        return types.Content(parts=[types.Part(text="")], role="model")

    callback_context.state["counselor_playbook_prompt"] = prompt
    return None


counselor_playbook_agent = Agent(
    model=LiteLlm(**_cfg),
    name="counselor_playbook_agent",
    instruction="{counselor_playbook_prompt}",
    before_agent_callback=counselor_playbook_prompt_builder,
    include_contents="none",
)
