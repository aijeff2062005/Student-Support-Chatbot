import asyncio
import base64
import json
import unittest
from types import SimpleNamespace

import services.qr_support as qr_support
from prompts.qr_error_answer import build_qr_error_answer_prompt
from services.qr_support import (
    QR_ERROR_CONTEXT_STATE_KEY,
    QR_ERROR_FAST_PATH_STATE_KEY,
    QrPayloadValidationError,
    build_qr_fast_path_state_updates,
    inspect_qr_attachments,
    inspect_qr_content_parts,
    parse_qr_error_context,
    sync_qr_fast_path_from_adk_user_content,
)


class QrSupportTests(unittest.TestCase):
    def test_parse_qr_error_context_accepts_support_payload(self):
        decoded_text = json.dumps(
            {
                "response": {
                    "message": "Yêu cầu không hợp lệ",
                    "errors": [
                        {
                            "message": "Cuộc hội thoại này đã có đơn đăng ký",
                            "field": "conversation_id",
                            "code": "204",
                        }
                    ],
                }
            },
            ensure_ascii=False,
        )

        context = parse_qr_error_context(decoded_text)

        self.assertEqual(context["source"], "validated_qr_support_payload")
        self.assertEqual(context["response_message"], "Yêu cầu không hợp lệ")
        self.assertEqual(
            context["errors"],
            [
                {
                    "message": "Cuộc hội thoại này đã có đơn đăng ký",
                    "field": "conversation_id",
                    "code": "204",
                }
            ],
        )

    def test_parse_qr_error_context_rejects_non_support_json(self):
        with self.assertRaises(QrPayloadValidationError):
            parse_qr_error_context(json.dumps({"url": "https://example.com"}))

    def test_qr_fast_path_state_updates_routes_to_answer_agent_only(self):
        context = {
            "source": "validated_qr_support_payload",
            "response_message": "Yêu cầu không hợp lệ",
            "errors": [{"message": "Đã có đơn đăng ký", "field": "conversation_id", "code": "204"}],
        }

        updates = build_qr_fast_path_state_updates(context, artifact={"filename": "qr-support/x.png", "version": 0})

        self.assertTrue(updates[QR_ERROR_FAST_PATH_STATE_KEY])
        self.assertEqual(updates[QR_ERROR_CONTEXT_STATE_KEY]["errors"][0]["code"], "204")
        self.assertTrue(updates["is_query"])
        self.assertTrue(updates["is_admission_topic"])
        self.assertTrue(updates["skip_playbook_scenario"])
        self.assertIsNone(updates["query_results"])
        self.assertIsNone(updates["multi_query_pairs"])

    def test_adk_inline_image_upload_can_trigger_fast_path(self):
        decoded_text = json.dumps(
            {
                "response": {
                    "message": "Yêu cầu không hợp lệ",
                    "errors": [{"message": "Đã có đơn đăng ký", "field": "conversation_id", "code": "204"}],
                }
            },
            ensure_ascii=False,
        )
        original_decoder = qr_support._decode_qr_text
        qr_support._decode_qr_text = lambda _: decoded_text
        try:
            part = SimpleNamespace(
                inline_data=SimpleNamespace(data=b"\x89PNG\r\n\x1a\nfake-image", mime_type="image/png")
            )
            text_part = SimpleNamespace(text="em bị lỗi lúc gửi hồ sơ")

            result = inspect_qr_content_parts([text_part, part])

            self.assertTrue(result.valid)
            self.assertEqual(result.context["errors"][0]["code"], "204")

            state = _FakeState()
            callback_context = SimpleNamespace(state=state, user_content=SimpleNamespace(parts=[text_part, part]))
            self.assertTrue(sync_qr_fast_path_from_adk_user_content(callback_context))
            self.assertIn("em bị lỗi lúc gửi hồ sơ", state["current_user_message"])
        finally:
            qr_support._decode_qr_text = original_decoder

    def test_adk_sync_clears_stale_qr_state_on_plain_text_turn(self):
        state = _FakeState(
            {
                QR_ERROR_FAST_PATH_STATE_KEY: True,
                QR_ERROR_CONTEXT_STATE_KEY: {"errors": [{"code": "204"}]},
            }
        )
        callback_context = SimpleNamespace(
            state=state,
            user_content=SimpleNamespace(parts=[SimpleNamespace(text="xin chào")]),
        )

        triggered = sync_qr_fast_path_from_adk_user_content(callback_context)

        self.assertFalse(triggered)
        self.assertFalse(state[QR_ERROR_FAST_PATH_STATE_KEY])
        self.assertIsNone(state[QR_ERROR_CONTEXT_STATE_KEY])

    def test_generic_image_type_hint_does_not_reject_valid_png(self):
        decoded_text = json.dumps(
            {
                "response": {
                    "message": "Yêu cầu không hợp lệ",
                    "errors": [{"message": "Đã có đơn đăng ký", "field": "conversation_id", "code": "204"}],
                }
            },
            ensure_ascii=False,
        )
        original_decoder = qr_support._decode_qr_text
        qr_support._decode_qr_text = lambda _: decoded_text
        try:
            image_base64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image").decode()

            result = asyncio.run(
                inspect_qr_attachments([{"type": "image", "filename": "support.png", "base64": image_base64}])
            )

            self.assertTrue(result.valid)
            self.assertEqual(result.context["errors"][0]["code"], "204")
        finally:
            qr_support._decode_qr_text = original_decoder

    def test_qr_answer_prompt_asks_for_friendly_non_technical_tone(self):
        prompt = build_qr_error_answer_prompt(
            qr_error_context={
                "source": "validated_qr_support_payload",
                "response_message": "Yêu cầu không hợp lệ",
                "errors": [
                    {
                        "message": "Cuộc hội thoại này đã có đơn đăng ký",
                        "field": "conversation_id",
                        "code": "204",
                    }
                ],
            },
            self_pronoun="mình",
            user_pronoun="bạn",
        )

        self.assertIn("Giọng văn thân thiện, mềm", prompt)
        self.assertIn("không nhắc lại nguyên văn", prompt)
        self.assertIn("đã có đơn đăng ký", prompt)
        self.assertNotIn("Cuộc hội thoại này", prompt)
        self.assertNotIn("Hệ thống", prompt)
        self.assertIn("Bạn đã có đơn đăng ký trước đó", prompt)
        self.assertNotIn("hỗ trợ kiểm tra đơn cũ trước", prompt)
        self.assertIn("thử đăng nhập bằng thông tin đã đăng ký để cập nhật hồ sơ", prompt)
        self.assertIn("Không tự thêm lời mời", prompt)

    def test_qr_answer_prompt_prioritizes_context_solution_without_forced_escalation(self):
        prompt = build_qr_error_answer_prompt(
            qr_error_context={
                "source": "validated_qr_support_payload",
                "response_message": "Yêu cầu không hợp lệ",
                "solution": "Đăng nhập bằng tài khoản đã đăng ký để cập nhật hồ sơ.",
                "errors": [
                    {
                        "message": "Số điện thoại bạn vừa nhập đã được sử dụng cho một hồ sơ đăng ký trước đó.",
                        "field": "phone",
                        "code": "204",
                    }
                ],
            },
            self_pronoun="mình",
            user_pronoun="bạn",
        )

        self.assertIn("phải ưu tiên dùng chính guidance đó", prompt)
        self.assertIn("không thêm step ngoài scope", prompt)
        self.assertIn("Không tự thêm lời mời", prompt)
        self.assertIn("không mặc định khuyên bạn dùng số điện thoại khác", prompt)


class _FakeState(dict):
    def to_dict(self):
        return dict(self)


if __name__ == "__main__":
    unittest.main()
