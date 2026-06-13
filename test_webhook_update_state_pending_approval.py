import importlib
import sys
import types
import unittest
from types import SimpleNamespace

from fastapi import APIRouter

from schemas.webhook_crm_integrate import WebhookAdmissionUpdateStateRequest


class _FakeAdmissionService:
    def __init__(self):
        self.calls = []

    def is_ready(self):
        return True

    async def update_state_service(self, **kwargs):
        self.calls.append(kwargs)


def _install_import_stubs():
    fake_service = _FakeAdmissionService()

    admission_agent_service_module = types.ModuleType("admission_agent_service")

    async def _get_admission_agent_service():
        return fake_service

    async def _shutdown_admission_agent_service():
        return None

    admission_agent_service_module.get_admission_agent_service = _get_admission_agent_service
    admission_agent_service_module.shutdown_admission_agent_service = _shutdown_admission_agent_service
    sys.modules["admission_agent_service"] = admission_agent_service_module

    config_service_module = types.ModuleType("configs.config_service")
    config_service_module.get_settings = lambda: SimpleNamespace()
    sys.modules["configs.config_service"] = config_service_module

    crud_package = types.ModuleType("crud")
    crud_package.__path__ = []
    sys.modules.setdefault("crud", crud_package)
    crud_webhook_module = types.ModuleType("crud.webhook")
    crud_webhook_module.build_default_greeting_message = lambda *args, **kwargs: "hello"
    crud_webhook_module.get_webhook_friendly_error = lambda error: str(error)

    async def _process_webhook_admission(*args, **kwargs):
        return [
            {
                "event_id": "evt-1",
                "status": "completed",
                "data": "Đã ghi nhận trạng thái.",
                "state": {
                    "current_stage": "advising",
                    "current_status": "pending_approval",
                    "is_pending_approval": True,
                },
                "processing_info": {},
                "extra_data": None,
            }
        ]

    crud_webhook_module.process_webhook_admission = _process_webhook_admission
    sys.modules["crud.webhook"] = crud_webhook_module

    routers_module = types.ModuleType("routers")
    routers_module.update_agent_history = SimpleNamespace(router=APIRouter())
    routers_module.validate_username = SimpleNamespace(router=APIRouter())
    sys.modules["routers"] = routers_module

    logging_config_module = types.ModuleType("utils.logging_config")
    logging_config_module.configure_logging = lambda: None
    logging_config_module.log_event = lambda *args, **kwargs: None
    logging_config_module.short_id = lambda value: value
    logging_config_module.truncate_text = lambda value, *_args, **_kwargs: value
    sys.modules["utils.logging_config"] = logging_config_module

    return fake_service


_original_admission_agent_service_module = sys.modules.get("admission_agent_service")
fake_service = _install_import_stubs()
main = importlib.import_module("main")
if _original_admission_agent_service_module is not None:
    sys.modules["admission_agent_service"] = _original_admission_agent_service_module
else:
    del sys.modules["admission_agent_service"]


class WebhookUpdateStatePendingApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_state_endpoint_supports_pending_approval_flow(self):
        request = WebhookAdmissionUpdateStateRequest(
            user_id="user-1",
            session_id="session-1",
            platform="facebook",
            update_state={"is_pending_approval": True},
        )

        response = await main.webhook_update_state_endpoint(request)

        self.assertEqual(len(fake_service.calls), 1)
        self.assertEqual(
            fake_service.calls[0]["update_state"],
            {"is_pending_approval": True, "platform": "facebook"},
        )
        self.assertEqual(response.status, "success")
        self.assertEqual(response.state["current_stage"], "advising")
        self.assertEqual(response.state["current_status"], "pending_approval")
        self.assertTrue(response.state["is_pending_approval"])


if __name__ == "__main__":
    unittest.main()
