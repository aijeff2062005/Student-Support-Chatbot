import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _install_import_stubs():
    agents_module = types.ModuleType("agents")
    agents_module.__path__ = [str(Path(__file__).resolve().parent / "agents")]
    agent_module = types.ModuleType("agents.agent")
    agent_module.root_agent = object()
    sys.modules.setdefault("agents", agents_module)
    sys.modules.setdefault("agents.agent", agent_module)

    langfuse_module = types.ModuleType("langfuse")

    class _Langfuse:
        def __init__(self, *args, **kwargs):
            pass

    langfuse_module.Langfuse = _Langfuse
    sys.modules.setdefault("langfuse", langfuse_module)

    openinference_module = types.ModuleType("openinference")
    instrumentation_module = types.ModuleType("openinference.instrumentation")
    google_adk_module = types.ModuleType("openinference.instrumentation.google_adk")

    class _GoogleADKInstrumentor:
        def instrument(self):
            return None

    google_adk_module.GoogleADKInstrumentor = _GoogleADKInstrumentor
    sys.modules.setdefault("openinference", openinference_module)
    sys.modules.setdefault("openinference.instrumentation", instrumentation_module)
    sys.modules.setdefault("openinference.instrumentation.google_adk", google_adk_module)


_install_import_stubs()
admission_agent_service = importlib.import_module("admission_agent_service")


class _FakeSessionService:
    def __init__(self, append_errors=None):
        self.append_errors = list(append_errors or [])
        self.get_session_calls = 0
        self.append_event_calls = 0
        self.appended_events = []
        self.appended_state_deltas = []

    async def get_session(self, app_name, user_id, session_id):
        self.get_session_calls += 1
        return SimpleNamespace(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            events=list(self.appended_events),
            state={},
        )

    async def append_event(self, session, event):
        self.append_event_calls += 1
        if self.append_errors:
            error = self.append_errors.pop(0)
            raise error
        self.appended_events.append(event)
        self.appended_state_deltas.append(dict(event.actions.state_delta))


class _NoopAsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRuntime:
    def __init__(self):
        self.lock = _NoopAsyncLock()
        self.lock_requests = []
        self.pending_process_requests = []

    def get_session_lock(self, user_id, session_id):
        self.lock_requests.append((user_id, session_id))
        return self.lock

    def process_pending_session_messages(self, user_id, session_id):
        self.pending_process_requests.append((user_id, session_id))


class UpdateStateRetryTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, session_service, runtime=None):
        service = object.__new__(admission_agent_service.AdmissionAgentService)
        service.session_service = session_service
        service.runtime = runtime
        return service

    async def test_retry_reloads_session_after_stale_error(self):
        stale_error = ValueError("The session has been modified in storage since it was loaded")
        session_service = _FakeSessionService(append_errors=[stale_error])
        service = self._service(session_service)
        event = admission_agent_service.Event(
            id="event-1",
            invocation_id="event-1",
            author="user",
            actions=admission_agent_service.EventActions(state_delta={"is_blocked": True}),
        )

        with patch("admission_agent_service.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            await service._append_update_state_event_with_retry(
                user_id="user-1",
                session_id="session-1",
                event=event,
                update_keys=["is_blocked"],
            )

        self.assertEqual(session_service.get_session_calls, 2)
        self.assertEqual(session_service.append_event_calls, 2)
        self.assertEqual(session_service.appended_events[0].id, "event-1")
        sleep_mock.assert_awaited_once_with(1)

    async def test_update_state_copies_payload_and_applies_business_rules(self):
        session_service = _FakeSessionService()
        runtime = _FakeRuntime()
        service = self._service(session_service, runtime=runtime)
        update_state = {"is_blocked": False, "is_submitted": True}

        await service.update_state_service(
            user_id="user-1",
            session_id="session-1",
            event_id="event-1",
            update_state=update_state,
        )

        self.assertEqual(update_state, {"is_blocked": False, "is_submitted": True})
        self.assertEqual(runtime.lock_requests, [("user-1", "session-1")])
        self.assertEqual(runtime.pending_process_requests, [("user-1", "session-1")])
        self.assertEqual(
            session_service.appended_state_deltas,
            [
                {
                    "is_blocked": False,
                    "is_submitted": True,
                    "block_notified": False,
                    "spam_count": 0,
                    "crm_update_state": True,
                }
            ],
        )

    async def test_update_state_pending_approval_sets_crm_update_state(self):
        session_service = _FakeSessionService()
        runtime = _FakeRuntime()
        service = self._service(session_service, runtime=runtime)
        update_state = {"is_blocked": False, "is_pending_approval": True}

        await service.update_state_service(
            user_id="user-1",
            session_id="session-1",
            event_id="event-2",
            update_state=update_state,
        )

        self.assertEqual(update_state, {"is_blocked": False, "is_pending_approval": True})
        self.assertEqual(runtime.lock_requests, [("user-1", "session-1")])
        self.assertEqual(runtime.pending_process_requests, [("user-1", "session-1")])
        self.assertEqual(
            session_service.appended_state_deltas,
            [
                {
                    "is_blocked": False,
                    "is_pending_approval": True,
                    "block_notified": False,
                    "spam_count": 0,
                    "crm_update_state": True,
                }
            ],
        )

    async def test_duplicate_event_is_treated_as_success_when_event_exists(self):
        duplicate_error = RuntimeError("duplicate key value violates unique constraint")
        session_service = _FakeSessionService(append_errors=[duplicate_error])
        service = self._service(session_service)
        event = admission_agent_service.Event(
            id="event-1",
            invocation_id="event-1",
            author="user",
            actions=admission_agent_service.EventActions(state_delta={"is_blocked": True}),
        )
        session_service.appended_events.append(event)

        await service._append_update_state_event_with_retry(
            user_id="user-1",
            session_id="session-1",
            event=event,
            update_keys=["is_blocked"],
        )

        self.assertEqual(session_service.append_event_calls, 1)
        self.assertEqual(session_service.get_session_calls, 2)


if __name__ == "__main__":
    unittest.main()
