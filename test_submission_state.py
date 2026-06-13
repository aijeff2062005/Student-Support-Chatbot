import unittest

from utils.submission_state import (
    get_active_submission_like_routes,
    get_submission_like_ack_key,
    get_submission_like_route,
    get_submission_like_stage_status,
    is_submission_like_acknowledged,
    should_mark_crm_update_state,
)


class SubmissionStateTests(unittest.TestCase):
    def test_pending_approval_triggers_crm_update_state(self):
        self.assertTrue(should_mark_crm_update_state({"is_pending_approval": True}))

    def test_pending_approval_route_has_priority_over_submitted(self):
        state = {"is_pending_approval": True, "is_submitted": True}

        self.assertEqual(get_active_submission_like_routes(state), ("pending_approval", "submitted"))
        self.assertEqual(get_submission_like_route(state), "pending_approval")
        self.assertEqual(get_submission_like_stage_status("pending_approval"), ("advising", "pending_approval"))

    def test_acknowledgement_keys_match_expected_state_names(self):
        self.assertEqual(get_submission_like_ack_key("pending_approval"), "is_pending_approval_acknowledged")
        self.assertEqual(get_submission_like_ack_key("submitted"), "is_submitted_acknowledged")
        self.assertTrue(
            is_submission_like_acknowledged({"is_pending_approval_acknowledged": True}, "pending_approval")
        )


if __name__ == "__main__":
    unittest.main()
