import copy
import importlib
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.playbook_hint_processing import PlaybookDMNHintProcessor


class FakeState:
    def __init__(self, data):
        self._data = copy.deepcopy(data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def to_dict(self):
        return copy.deepcopy(self._data)


def make_context(state_data):
    return SimpleNamespace(
        state=FakeState(state_data),
        session=SimpleNamespace(id="test-session"),
    )


def load_stage_extractor_module():
    class FakeDriver:
        def verify_connectivity(self):
            return None

    sys.modules.pop("tools.stage_extractor", None)
    with patch("neo4j.GraphDatabase.driver", return_value=FakeDriver()):
        return importlib.import_module("tools.stage_extractor")


class PlaybookRegressionRestoreTests(unittest.TestCase):
    def test_stage_extractor_keeps_turn_intent_flags_on_current_turn_only(self):
        stage_extractor_module = load_stage_extractor_module()

        llm_payload = {
            "topic": "học phí và học bổng",
            "is_asking_tuition": True,
        }

        fake_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(llm_payload)))]
        )

        current_stage = stage_extractor_module.UserState(
            is_asking_deadline=True,
        )

        with patch.object(stage_extractor_module.litellm, "completion", return_value=fake_response):
            next_state = stage_extractor_module.stage_extractor(
                user_input="Học phí bên mình bao nhiêu ạ?",
                current_stage=current_stage,
                last_agent_message={"counselor_playbook_agent": "Em cần hỏi gì thêm không?"},
            )

        self.assertTrue(next_state.is_asking_tuition)
        self.assertFalse(next_state.is_asking_deadline)

    def test_ask_all_information_does_not_reask_major_when_user_already_has_major(self):
        processor = PlaybookDMNHintProcessor()
        context = make_context(
            {
                "user_state": {
                    "major": [{"7480201": "Công nghệ thông tin"}],
                }
            }
        )

        with patch("tools.playbook_hint_processing.check_missing_fields_user_profile", return_value=["Họ tên"]):
            processor._handle_answer_hint("func(ask_all_information)", context)

        self.assertEqual(context.state.get("playbook_answer_guidance"), "Họ tên")

    def test_action_hint_continues_after_one_hint_raises(self):
        processor = PlaybookDMNHintProcessor()
        context = make_context({})
        hints = [
            {"type": "query", "query": "boom"},
            {"type": "instruction", "prompt": "Xin chao"},
        ]

        with patch("tools.playbook_hint_processing.handle_action_type_query", side_effect=RuntimeError("boom")):
            processor._handle_action_hint(hints, context)

        self.assertEqual(context.state.get("playbook_action_instruction"), ["Xin chao"])


if __name__ == "__main__":
    unittest.main()
