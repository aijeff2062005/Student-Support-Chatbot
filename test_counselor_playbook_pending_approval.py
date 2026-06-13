import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def _install_import_stubs():
    agents_package = sys.modules.get("agents")
    if agents_package is None:
        agents_package = types.ModuleType("agents")
        sys.modules["agents"] = agents_package
    agents_package.__path__ = [str(Path(__file__).resolve().parent / "agents")]

    litellm_module = types.ModuleType("litellm")
    sys.modules["litellm"] = litellm_module

    google_module = types.ModuleType("google")
    adk_module = types.ModuleType("google.adk")
    agents_module = types.ModuleType("google.adk.agents")
    callback_context_module = types.ModuleType("google.adk.agents.callback_context")
    models_module = types.ModuleType("google.adk.models")
    lite_llm_module = types.ModuleType("google.adk.models.lite_llm")
    genai_module = types.ModuleType("google.genai")
    genai_types_module = types.ModuleType("google.genai.types")

    class _Agent:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _CallbackContext:
        pass

    class _LiteLlm:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _Part:
        def __init__(self, text=None):
            self.text = text

    class _Content:
        def __init__(self, parts=None, role=None):
            self.parts = parts or []
            self.role = role

    agents_module.Agent = _Agent
    callback_context_module.CallbackContext = _CallbackContext
    lite_llm_module.LiteLlm = _LiteLlm
    genai_types_module.Part = _Part
    genai_types_module.Content = _Content
    genai_module.types = genai_types_module

    sys.modules["google"] = google_module
    sys.modules["google.adk"] = adk_module
    sys.modules["google.adk.agents"] = agents_module
    sys.modules["google.adk.agents.callback_context"] = callback_context_module
    sys.modules["google.adk.models"] = models_module
    sys.modules["google.adk.models.lite_llm"] = lite_llm_module
    sys.modules["google.genai"] = genai_module
    sys.modules["google.genai.types"] = genai_types_module

    data_collector_module = types.ModuleType("agents.data_collector_agent")
    data_collector_module.get_conversation_turns = lambda *args, **kwargs: []
    sys.modules["agents.data_collector_agent"] = data_collector_module

    config_service_module = types.ModuleType("configs.config_service")
    config_service_module.get_settings = lambda: SimpleNamespace(litellm_stage_extractor_model="dummy-model")
    sys.modules["configs.config_service"] = config_service_module

    llm_client_module = types.ModuleType("configs.llm_client")
    llm_client_module.get_litellm_config = lambda *args, **kwargs: {}
    sys.modules["configs.llm_client"] = llm_client_module

    prompt_module = types.ModuleType("prompts.evaluate_playbook_follow")
    prompt_module.SYSTEM_PROMPT = "stub"
    sys.modules["prompts.evaluate_playbook_follow"] = prompt_module

    qr_support_module = types.ModuleType("services.qr_support")
    qr_support_module.QR_ATTACHMENT_NOOP_STATE_KEY = "qr_attachment_noop"
    qr_support_module.QR_ERROR_FAST_PATH_STATE_KEY = "qr_error_fast_path"
    sys.modules["services.qr_support"] = qr_support_module

    media_policy_module = types.ModuleType("tools.media_attachment_policy")
    media_policy_module.fetch_interested_major_media_once = lambda *args, **kwargs: {}
    sys.modules["tools.media_attachment_policy"] = media_policy_module

    playbook_hint_module = types.ModuleType("tools.playbook_hint_processing")
    playbook_hint_module.resolve_playbook_text_hint = lambda *args, **kwargs: None
    sys.modules["tools.playbook_hint_processing"] = playbook_hint_module

    response_prompt_module = types.ModuleType("tools.response_agent_prompt_builder")

    class _CounselorPlaybookPromptState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    response_prompt_module.CounselorPlaybookPromptState = _CounselorPlaybookPromptState
    response_prompt_module._build_counselor_scope_guard_block = lambda *args, **kwargs: ""
    response_prompt_module.build_counselor_playbook_prompt = lambda *args, **kwargs: ""
    sys.modules["tools.response_agent_prompt_builder"] = response_prompt_module


_install_import_stubs()
counselor_playbook_agent = importlib.import_module("agents.counselor_playbook_agent")


class CounselorPlaybookPendingApprovalTests(unittest.TestCase):
    def test_pending_approval_acknowledges_once_and_resets_crm_flag(self):
        callback_context = SimpleNamespace(
            state={
                "is_pending_approval": True,
                "crm_update_state": True,
                "self_pronoun": "mình",
                "user_pronoun": "bạn",
                "user_state": {},
            }
        )

        result = counselor_playbook_agent.counselor_playbook_prompt_builder(callback_context)

        self.assertIsNone(result)
        self.assertTrue(callback_context.state["is_pending_approval_acknowledged"])
        self.assertFalse(callback_context.state["crm_update_state"])
        self.assertIn("đã nhận được hồ sơ đăng ký dự tuyển", callback_context.state["counselor_playbook_prompt"])

    def test_pending_approval_takes_priority_and_marks_both_ack_flags_when_both_states_are_true(self):
        callback_context = SimpleNamespace(
            state={
                "is_pending_approval": True,
                "is_submitted": True,
                "crm_update_state": True,
                "self_pronoun": "mình",
                "user_pronoun": "bạn",
                "user_state": {},
            }
        )

        counselor_playbook_agent.counselor_playbook_prompt_builder(callback_context)

        self.assertTrue(callback_context.state["is_pending_approval_acknowledged"])
        self.assertTrue(callback_context.state["is_submitted_acknowledged"])

    def test_pending_approval_does_not_repeat_after_acknowledged(self):
        callback_context = SimpleNamespace(
            state={
                "is_pending_approval": True,
                "is_pending_approval_acknowledged": True,
                "crm_update_state": True,
                "self_pronoun": "mình",
                "user_pronoun": "bạn",
                "user_state": {},
            }
        )

        result = counselor_playbook_agent.counselor_playbook_prompt_builder(callback_context)

        self.assertIsNotNone(result)
        self.assertNotIn("counselor_playbook_prompt", callback_context.state)


if __name__ == "__main__":
    unittest.main()
