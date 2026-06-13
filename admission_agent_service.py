"""
AdmissionAgentService - Fixed version targeting response_synthesizer_agent only

KEY FIX: Only collect responses from event.author == 'response_synthesizer_agent'
"""

import asyncio
import hashlib
import tempfile
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from google.adk.agents.run_config import StreamingMode
from google.adk.artifacts import FileArtifactService
from google.adk.events import Event, EventActions
from google.adk.runners import RunConfig
from google.adk.sessions import DatabaseSessionService
from google.genai.types import Content, Part
from langfuse import Langfuse
from openinference.instrumentation.google_adk import GoogleADKInstrumentor

from agents.agent import root_agent  # admission_root_agent from existing code
from configs.config_service import get_settings
from services.qr_support import (
    QR_ERROR_CONTEXT_STATE_KEY,
    build_clear_qr_state_updates,
    build_qr_fast_path_state_updates,
    build_qr_runtime_message,
    inspect_qr_attachments,
)
from utils.submission_state import should_mark_crm_update_state
from session.base_runtime import BaseRuntime
from utils.logging_config import log_event, truncate_text, get_logger, short_id

GoogleADKInstrumentor().instrument()
# import agentops
# import admission components

logger = get_logger(__name__)
settings = get_settings()
STALE_SESSION_ERROR_MESSAGE = "The session has been modified in storage since it was loaded"
UPDATE_STATE_APPEND_MAX_ATTEMPTS = 3
UPDATE_STATE_APPEND_BACKOFF_SECONDS = 1
DUPLICATE_EVENT_ERROR_MARKERS = (
    "duplicate",
    "unique constraint",
    "already exists",
)
SEGMENT_SKIP_UCS = {"UC2", "UC3", "UC4"}
# agentops.init(
#     # service_name="AdmissionAgentService",
#     endpoint="https://agentops-api.dxfuturetech.com.vn/",
#     api_key=settings.agentops_api_key,
#     app_url="https://agentops.dxfuturetech.com.vn/"
# )

langfuse = Langfuse(
    secret_key=settings.langfuse_secret_key, public_key=settings.langfuse_public_key, host=settings.langfuse_base_url
)


def utc_now_iso():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AdmissionAgentService:
    """
    Multi-Agent Service for University Admission System

    FIXED: Only collect response from response_synthesizer_agent via event.author
    """

    def __init__(self):
        self.runtime: BaseRuntime | None = None
        self.session_service: DatabaseSessionService | None = None
        self.artifact_service: FileArtifactService | None = None
        self.settings = get_settings()
        self._initialized = False

        log_event(logger, "info", "admission_service_init_start")

    def _build_common_qa_recommendation_url(self, event_id: str) -> str | None:
        endpoint_template = self.settings.common_qa_recommendation_api_url.strip()
        if not endpoint_template:
            return None

        encoded_event_id = quote(event_id, safe="")
        if "{event_id}" in endpoint_template:
            return endpoint_template.replace("{event_id}", encoded_event_id)

        return f"{endpoint_template.rstrip('/')}/{encoded_event_id}/common-qa-recommendation"

    async def _trigger_common_qa_recommendation(
        self,
        *,
        event_id: str | None,
    ) -> None:
        if not event_id:
            return

        url = self._build_common_qa_recommendation_url(event_id)
        if not url:
            return

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.common_qa_recommendation_api_timeout_seconds,
            ) as client:
                response = await client.post(url, headers={"accept": "*/*"}, content=b"")
                response.raise_for_status()

            log_event(
                logger,
                "info",
                "common_qa_recommendation_triggered",
                event_id=event_id,
                status_code=response.status_code,
            )
        except httpx.HTTPStatusError as exc:
            response_text = exc.response.text if exc.response is not None else ""
            logger.warning(
                "event=common_qa_recommendation_http_error | event_id=%s | status_code=%s | response=%s",
                event_id,
                exc.response.status_code if exc.response is not None else None,
                truncate_text(response_text, 300),
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "event=common_qa_recommendation_request_failed | event_id=%s | error=%s",
                event_id,
                exc,
            )
        except Exception as exc:
            logger.exception(
                "event=common_qa_recommendation_unexpected_error | event_id=%s | error=%s",
                event_id,
                exc,
            )

    def _schedule_common_qa_recommendation(
        self,
        *,
        event_id: str | None,
    ) -> None:
        if not event_id:
            return

        asyncio.create_task(
            self._trigger_common_qa_recommendation(
                event_id=event_id,
            )
        )

    async def initialize(self):
        """Initialize Google ADK components for admission system."""
        if self._initialized:
            log_event(logger, "debug", "admission_service_already_initialized")
            return

        try:
            log_event(logger, "info", "admission_service_initializing")

            # Initialize InMemorySessionService (not Database)
            log_event(logger, "debug", "admission_service_session_service_creating")
            self.session_service = DatabaseSessionService(db_url=settings.session_database_url)
            artifact_root = self.settings.adk_artifact_root_dir or str(
                Path(tempfile.gettempdir()) / "admission-agent-artifacts"
            )
            self.artifact_service = FileArtifactService(root_dir=artifact_root)

            # import and initialize admission root agent
            log_event(logger, "debug", "admission_service_root_agent_loading")
            admission_agent = root_agent  # From agents/agent.py

            if not admission_agent:
                raise RuntimeError("admission_root_agent not found in agents/agent.py")

            # Initialize ADK Runner for non-streaming
            log_event(logger, "debug", "admission_service_runtime_initializing", streaming=True)
            self.runtime = BaseRuntime(
                agent=admission_agent,
                app_name="agents",
                session_service=self.session_service,
                artifact_service=self.artifact_service,
                streaming=True,  # BaseRuntime needs streaming enabled for SSE
            )

            self._initialized = True
            log_event(
                logger,
                "info",
                "admission_service_initialized",
                model=self.settings.litellm_agent_model,
                mcp_host=self.settings.mcp_host,
                mcp_port=self.settings.mcp_port,
                session_service="DatabaseSessionService",
                artifact_service="FileArtifactService",
                mode="multi-agent",
            )

        except Exception as e:
            logger.exception("event=admission_service_init_error | error=%s", e)
            raise RuntimeError(f"Failed to initialize AdmissionAgentService: {e}") from e

    def _create_initial_session_state(
        self,
        user_id: str,
        customer_info: dict[str, Any] | None,
        sale_profile: dict[str, Any] | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """Create initial session state for admission conversations."""
        user_state = {}

        if customer_info and customer_info.get("customer_type") == "student":
            user_state["full_name"] = customer_info.get("name", None)
            user_state["phone"] = customer_info.get("phone", None)
            user_state["email"] = customer_info.get("email", None)
            user_state["high_school"] = customer_info.get("high_school", None)

        return {
            "user_context": {
                "started_at": datetime.now().isoformat(),
                "last_updated": datetime.now().isoformat(),
                "message_count": 0,
                "current_topic": "",
                "user_id": user_id,
            },
            # Personal Information
            "customer_info": {
                "name": customer_info.get("full_name", "") if customer_info else "",
                "phone": customer_info.get("phone", "") if customer_info else "",
                "email": customer_info.get("email", "") if customer_info else "",
                "high_school": customer_info.get("high_school", "") if customer_info else "",
                "social_links": [],
                "location": "",
                "education_level": "",
                "gender": customer_info.get("gender", "") if customer_info else None,
                "customer_type": customer_info.get("customer_type", None) if customer_info else None,
            },
            "current_stage": customer_info.get("current_stage", None) if customer_info else None,
            "current_status": customer_info.get("current_status", None) if customer_info else None,
            "user_state": {
                "role": customer_info.get("customer_type", None) if customer_info else None,
                "topic": [],
                "major": [],
                "potential_majors": [],
                "interested_majors": [],
                "major_interest_events": [],
                "major_interest_scores": [],
                "is_requested_submit_application_now": False,
                "student_profile": user_state,
            },
            "self_pronoun": "mình",
            "user_pronoun": "bạn",
            "sale_profile": {
                "gender": sale_profile.get("sales_gender", "male") if sale_profile else None,
                "name": sale_profile.get("sales_name", "") if sale_profile else None,
            },
            # Customer Features for Segmentation (9 features)
            "customer_features": {
                "motivation_keywords": [],
                "tuition_sensitive_keywords": [],
                "core_values_keywords": [],
                "studying_goals_keywords": [],
                "environment_keywords": [],
                "interaction_frequency_keywords": [],
                "preferred_channel_keywords": [],
                "financial_behavior_keywords": [],
                "churn_risk_signals_keywords": [],
            },
            # Mode
            "qna_mode": customer_info.get("qna_mode", False) if customer_info else False,
            "platform": platform,
            # Agent Results
            "query_results": {},
            "collection_results": {},
            # Segmentation Results
            "latest_segments": "",
            "extra_data": {
                "attachments": [],
            },
            # Block state
            "is_blocked": False,
            "block_notified": False,
        }

    async def ensure_admission_session(
        self,
        user_id: str,
        session_id: str,
        customer_info: dict[str, Any] | None = None,
        sale_profile: dict[str, Any] | None = None,
        platform: str | None = None,
    ) -> tuple[Any, bool]:
        """Ensure the admission session exists and report whether it was newly created."""
        if self.session_service is None:
            raise RuntimeError("Session service is not initialized.")

        app_name = "agents"

        existing_session = await self.session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if existing_session:
            existing_session = await self._sync_session_platform_if_needed(
                user_id=user_id,
                session_id=session_id,
                session=existing_session,
                platform=platform,
            )
            log_event(
                logger,
                "debug",
                "admission_session_found",
                user_id=short_id(user_id),
                session_id=short_id(session_id),
            )
            return existing_session, False

        initial_state = self._create_initial_session_state(
            user_id,
            customer_info=customer_info,
            sale_profile=sale_profile,
            platform=platform,
        )

        session = await self.session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            state=initial_state,
        )
        return session, True

    async def _sync_session_platform_if_needed(
        self,
        user_id: str,
        session_id: str,
        session: Any,
        platform: str | None,
    ) -> Any:
        """Persist platform metadata without triggering the runtime message-processing path."""
        if not platform:
            return session

        session_state = getattr(session, "state", None)
        if isinstance(session_state, dict) and session_state.get("platform") == platform:
            return session

        event_id = str(uuid.uuid4())
        metadata_event = Event(
            id=event_id,
            invocation_id=event_id,
            author="system",
            actions=EventActions(state_delta={"platform": platform}),
        )

        if self.runtime is not None:
            async with self.runtime.get_session_lock(user_id=user_id, session_id=session_id):
                await self._append_update_state_event_with_retry(
                    user_id=user_id,
                    session_id=session_id,
                    event=metadata_event,
                    update_keys=["platform"],
                )
        else:
            await self._append_update_state_event_with_retry(
                user_id=user_id,
                session_id=session_id,
                event=metadata_event,
                update_keys=["platform"],
            )

        if self.session_service is None:
            raise RuntimeError("Session service is not initialized")

        refreshed_session = await self.session_service.get_session(
            app_name="agents",
            user_id=user_id,
            session_id=session_id,
        )
        return refreshed_session or session

    async def _ensure_session_exists(
        self,
        user_id: str,
        session_id: str,
        customer_info: dict[str, Any] | None = None,
        sale_profile: dict[str, Any] | None = None,
        platform: str | None = None,
    ):
        """Backward-compatible wrapper for callers that only need the session object."""
        try:
            session, _ = await self.ensure_admission_session(
                user_id=user_id,
                session_id=session_id,
                customer_info=customer_info,
                sale_profile=sale_profile,
                platform=platform,
            )
            return session
        except Exception as e:
            traceback.print_exc()
            logger.exception(
                "event=admission_session_ensure_error | user_id=%s | session_id=%s | error=%s",
                short_id(user_id),
                short_id(session_id),
                e,
            )
            return None

    # admission_agent_service.py

    async def stream_query_admission(
        self,
        user_id: str,
        message: str,
        session_id: str,
        customer_info: dict[str, Any] | None = None,
        sale_profile: dict[str, Any] | None = None,
        platform: str | None = None,
        attachments: Any | None = None,
    ):
        """
        Stream admission query from answer_query_agent AND counselor_playbook_agent.

        Logic (Atomic Flush):
        - Stream `answer_query_agent` IMMEDIATELY (priority).
        - Buffer `counselor_playbook_agent` chunks using Atomic Flush protection.
        - After `answer_query_agent` finishes, switch flag, flush buffer, then stream playbook real-time.
        """

        try:
            # Ensure session exists
            await self.ensure_admission_session(
                user_id=user_id,
                session_id=session_id,
                customer_info=customer_info,
                sale_profile=sale_profile,
                platform=platform,
            )

            original_message_text = message.strip() if isinstance(message, str) else ""
            state_updates = build_clear_qr_state_updates()
            message_text_for_runtime = original_message_text

            if attachments:
                qr_result = await inspect_qr_attachments(attachments)
                if qr_result.valid and qr_result.context and qr_result.image_payload:
                    artifact_info = None
                    try:
                        artifact_info = await self._save_qr_artifact(
                            user_id=user_id,
                            session_id=session_id,
                            image_bytes=qr_result.image_payload.image_bytes,
                            mime_type=qr_result.image_payload.mime_type,
                            source_filename=qr_result.image_payload.filename,
                            qr_context=qr_result.context,
                        )
                    except Exception as exc:
                        logger.warning(
                            "event=qr_artifact_save_failed | session_id=%s | error=%s",
                            short_id(session_id),
                            exc,
                            exc_info=True,
                        )
                    state_updates = build_qr_fast_path_state_updates(qr_result.context, artifact=artifact_info)
                    message_text_for_runtime = build_qr_runtime_message(original_message_text)
                    log_event(
                        logger,
                        "info",
                        "qr_fast_path_enabled",
                        user_id=short_id(user_id),
                        session_id=short_id(session_id),
                        error_count=len(qr_result.context.get("errors", [])),
                    )
                elif qr_result.attachment_seen and not message_text_for_runtime:
                    yield {
                        "event": "error",
                        "data": qr_result.user_message
                        or "Mình chưa đọc được mã QR hợp lệ. Bạn vui lòng gửi lại ảnh QR rõ hơn nhé.",
                        "session_id": session_id,
                        "created_at": str(utc_now_iso()),
                    }
                    return

            # Create message content
            message_content = Content(role="user", parts=[Part(text=message_text_for_runtime)])

            # Configure streaming
            run_config = RunConfig(response_modalities=["TEXT"], streaming_mode=StreamingMode.SSE, max_llm_calls=50)

            log_event(
                logger,
                "info",
                "admission_stream_start",
                user_id=short_id(user_id),
                session_id=short_id(session_id),
                preview=truncate_text(original_message_text, 100),
            )

            # Buffers and State
            answer_accumulator = ""
            playbook_accumulator = ""

            playbook_buffer = []  # List of dicts (events)
            is_flushing_buffer = False  # ⭐ NEW: Protect iteration during flush

            answer_finished = False

            ANSWER_AGENT = "answer_query_agent"
            PLAYBOOK_AGENT = "counselor_playbook_agent"

            query_agent_found = False
            playbook_agent_found = False

            if self.runtime is None:
                raise RuntimeError("Runtime is not initialized")
            if self.session_service is None:
                raise RuntimeError("Session service is not initialized")

            message_parts = message_content.parts
            if not message_parts:
                raise ValueError("Message content is empty")
            raw_message_text = message_parts[0].text
            message_text = raw_message_text if isinstance(raw_message_text, str) else ""
            if not message_text.strip():
                log_event(logger, "warning", "Message text is empty")

            async for event in self.runtime.run_async(
                user_id=user_id,
                session_id=session_id,
                message=message_text,
                state_updates=state_updates,
                run_config=run_config,
            ):
                event_author = getattr(event, "author", "unknown")

                # Filter for our two response agents
                if event_author not in [ANSWER_AGENT, PLAYBOOK_AGENT]:
                    continue

                # Extract text content
                text_delta = ""

                content_part_delta = getattr(event, "content_part_delta", None)
                delta_text = getattr(content_part_delta, "text", None)
                if isinstance(delta_text, str) and delta_text:
                    if delta_text:
                        # logger.error(f"Found content_part_delta text: {event.content_part_delta.text}")
                        text_delta = delta_text
                else:
                    event_content = getattr(event, "content", None)
                    event_parts = getattr(event_content, "parts", None)
                    if isinstance(event_parts, list) and event_parts:
                        for part in event_parts:
                            if hasattr(part, "text") and part.text:
                                if hasattr(event, "partial") and event.partial:
                                    # logger.error(f"Found content part text: {part.text}")
                                    text_delta += part.text

                event_content = getattr(event, "content", None)
                event_parts = getattr(event_content, "parts", None)
                content_has_parts = isinstance(event_parts, list) and len(event_parts) > 0

                if not text_delta and not event.is_final_response():
                    continue

                # --- FAST PATH: ANSWER AGENT (Priority 1) ---
                if event_author == ANSWER_AGENT:
                    query_agent_found = True
                    answer_accumulator += text_delta

                    # Yield chunk immediately
                    if text_delta:
                        # logger.error(f"YIELDING Answer Chunk: {len(text_delta)} chars")
                        yield {
                            "event": "message",
                            "event_id": getattr(event, "id", None),
                            "invocation_id": event.invocation_id,
                            "data": text_delta,
                            "session_id": session_id,
                            "user_id": user_id,
                            "status": "streaming",
                            "author": ANSWER_AGENT,
                            "created_at": str(utc_now_iso()),
                        }

                    # Handle Completion
                    if event.is_final_response() and query_agent_found and event.content and content_has_parts:
                        # Get state to check for attachments
                        updated_session = await self.session_service.get_session(
                            app_name="agents", user_id=user_id, session_id=session_id
                        )
                        full_state = updated_session.state if updated_session else {}
                        filtered_state = self._build_filtered_state(full_state)

                        #  ANSWER AGENT: Returns 'attachments' in extra_data
                        extra_data_source = full_state.get("extra_data", {})
                        attachments = extra_data_source.get("attachments", []) if extra_data_source else []
                        final_extra_data = {"attachments": attachments}

                        #  SUPPRESSION CHECK: Ignore if Empty Text AND No Attachments
                        has_text = bool(answer_accumulator and answer_accumulator.strip())
                        has_attachments = bool(attachments)

                        if not has_text and not has_attachments:
                            # logger.error("SUPPRESSING Answer Agent Completion (Empty Text + No Attachments). Waiting for real response...")
                            # Start looking for next potential Answer Agent event
                            # IMPORTANT: Do NOT set answer_finished = True
                            # IMPORTANT: Do NOT flush Playbook Buffer
                            continue

                        # logger.error(f"Answer Agent FINISHED (Valid). Text: {has_text}, Attachments: {has_attachments}. Initiating Flush.")
                        answer_finished = True

                        # Yield COMPLETED for Answer Agent
                        answer_completed_chunk = {
                            "event": "message",
                            "invocation_id": event.invocation_id,
                            "event_id": event.id,
                            "data": answer_accumulator,
                            "session_id": session_id,
                            "user_id": user_id,
                            "status": "completed",
                            "author": ANSWER_AGENT,
                            "state": filtered_state,
                            "extra_data": final_extra_data,
                            "created_at": str(utc_now_iso()),
                        }
                        yield answer_completed_chunk

                        # self._schedule_common_qa_recommendation(
                        #     event_id=answer_completed_chunk.get("event_id"),
                        # )

                        #  ATOMIC FLUSH: Flush Playbook Buffer
                        if playbook_buffer:
                            is_flushing_buffer = True  # ⭐ Set flag
                            # logger.error(f"Flushing {len(playbook_buffer)} buffered playbook chunks (ATOMIC)")

                            for buffered_chunk in playbook_buffer:
                                # logger.error("  -> Yielding Buffered Chunk")
                                yield buffered_chunk

                            playbook_buffer = []
                            is_flushing_buffer = False  # ⭐ Clear flag
                            # logger.error(" Buffer flushed (ATOMIC complete)")

                # --- SLOW PATH: PLAYBOOK AGENT (Priority 2) ---
                elif event_author == PLAYBOOK_AGENT:
                    playbook_agent_found = True
                    # logger.error(f"text_delta: {text_delta}")
                    playbook_accumulator += text_delta
                    # logger.error(f"playbook_accumulator data: {playbook_accumulator}")

                    # Construct chunk
                    chunk = {
                        "event": "message",
                        "event_id": getattr(event, "id", None),
                        "invocation_id": event.invocation_id,
                        "data": text_delta,
                        "session_id": session_id,
                        "user_id": user_id,
                        "status": "streaming",
                        "author": PLAYBOOK_AGENT,
                        "created_at": str(utc_now_iso()),
                    }

                    # Logic: Stream ONLY if Answer is done AND not flushing
                    if answer_finished and not is_flushing_buffer:
                        if text_delta:
                            # logger.error(f"YIELDING Playbook Chunk (Real-time): {len(text_delta)} chars")
                            yield chunk
                    else:
                        if text_delta:
                            # logger.error(f"BUFFERING Playbook Chunk: {len(text_delta)} chars")
                            playbook_buffer.append(chunk)

                    # Handle Completion
                    if event.is_final_response() and playbook_agent_found and event.content and content_has_parts:
                        # logger.error(" Playbook Agent FINISHED.")

                        updated_session = await self.session_service.get_session(
                            app_name="agents", user_id=user_id, session_id=session_id
                        )
                        full_state = updated_session.state if updated_session else {}
                        filtered_state = self._build_filtered_state(full_state)

                        #  PLAYBOOK AGENT: Returns 'buttons' in extra_data
                        extra_data_source = full_state.get("extra_data", {})
                        buttons = extra_data_source.get("buttons", [])

                        final_extra_data = {"buttons": buttons}

                        #  SUPPRESSION CHECK: Ignore if Empty Text AND No Buttons
                        has_text = bool(playbook_accumulator and playbook_accumulator.strip())
                        has_buttons = bool(buttons)

                        if not has_text and not has_buttons:
                            # logger.error("SUPPRESSING Playbook Agent Completion (Empty Text + No Buttons).")
                            continue

                            # logger.error(f"playbook_accumulator data: {playbook_accumulator}")

                        final_chunk = {
                            "event": "message",
                            "event_id": event.id,
                            "invocation_id": event.invocation_id,
                            "data": playbook_accumulator,
                            "session_id": session_id,
                            "user_id": user_id,
                            "status": "completed",
                            "author": PLAYBOOK_AGENT,
                            "state": filtered_state,
                            "extra_data": final_extra_data,
                            "created_at": str(utc_now_iso()),
                        }

                        if answer_finished and not is_flushing_buffer:
                            # logger.error(" YIELDING Playbook Completion (Real-time)")
                            yield final_chunk
                        else:
                            log_event(
                                logger,
                                "debug",
                                "admission_stream_playbook_completion_buffered",
                                user_id=short_id(user_id),
                                session_id=short_id(session_id),
                            )
                            playbook_buffer.append(final_chunk)

            #  FINAL FLUSH: If iterator ends but buffer implies pending data
            if playbook_buffer:
                # logger.error("Loop ended with events in buffer, flushing now...")
                for buffered_chunk in playbook_buffer:
                    # logger.error(f"playbook_accumulator data: {playbook_accumulator}")
                    yield buffered_chunk

            if not query_agent_found and not playbook_agent_found:
                yield {"event": "error", "data": "Hệ thống đang bận, vui lòng thử lại sau.", "session_id": session_id}

            elif not answer_accumulator and not playbook_accumulator:
                yield {
                    "event": "error",
                    "data": "Không thể tạo phản hồi. Vui lòng thử lại.",
                    "session_id": session_id,
                    "created_at": str(utc_now_iso()),
                }

        except Exception as e:
            logger.exception(
                "event=admission_stream_error | user_id=%s | session_id=%s | error=%s",
                short_id(user_id),
                short_id(session_id),
                e,
            )
            yield {"event": "error", "data": "Đã có lỗi xảy ra.", "session_id": session_id}

    async def _save_qr_artifact(
        self,
        *,
        user_id: str,
        session_id: str,
        image_bytes: bytes,
        mime_type: str,
        source_filename: str,
        qr_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist the QR upload as an ADK artifact for trace/debug without exposing it to the LLM."""
        if self.artifact_service is None:
            return None

        suffix = "png" if mime_type == "image/png" else "jpg"
        digest = hashlib.sha256(image_bytes).hexdigest()[:16]
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
        filename = f"qr-support/{session_digest}/{digest}.{suffix}"
        errors = qr_context.get("errors") if isinstance(qr_context, dict) else []
        first_error = errors[0] if isinstance(errors, list) and errors else {}
        metadata = {
            "source_filename": source_filename,
            "mime_type": mime_type,
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
            "qr_context_state_key": QR_ERROR_CONTEXT_STATE_KEY,
        }
        if isinstance(first_error, dict):
            metadata["error_code"] = first_error.get("code")
            metadata["error_field"] = first_error.get("field")

        version = await self.artifact_service.save_artifact(
            app_name="agents",
            user_id=user_id,
            session_id=session_id,
            filename=filename,
            artifact=Part.from_bytes(data=image_bytes, mime_type=mime_type),
            custom_metadata=metadata,
        )
        return {
            "filename": filename,
            "version": version,
            "mime_type": mime_type,
            "sha256": metadata["sha256"],
        }

    def _build_filtered_state(self, full_state: dict[str, Any]) -> dict[str, Any]:
        """Build filtered state with only required keys for client response."""
        return {
            "customer_info": full_state.get(
                "customer_info",
                {
                    "name": None,
                    "phone": None,
                    "email": None,
                    "high_school": None,
                    "social_links": [],
                    "location": None,
                    "education_level": None,
                    "gender": None,
                },
            ),
            "current_stage": full_state.get("current_stage", None),
            "crm_school_id": full_state.get("crm_school_id", None),
            "current_status": full_state.get("current_status", None),
            "user_state": full_state.get(
                "user_state",
                {
                    "role": None,
                    "topic": [],
                    "major": [],
                    "potential_majors": [],
                    "interested_majors": [],
                    "major_interest_events": [],
                    "major_interest_scores": [],
                    "is_requested_submit_application_now": False,
                },
            ),
            "self_pronoun": full_state.get("self_pronoun", "mình"),
            "user_pronoun": full_state.get("user_pronoun", "bạn"),
            "student_name": full_state.get("student_name", None),
            "student_phone": full_state.get("student_phone", None),
            "student_email": full_state.get("student_email", None),
            "old_high_school_province": full_state.get("old_high_school_province", None),
            "old_province_location": full_state.get("old_province_location", None),
            "has_profile_info": full_state.get("has_profile_info", False),
            "qna_mode": full_state.get("qna_mode", False),
            "platform": full_state.get("platform", None),
            "is_submitted": full_state.get("is_submitted", None),
            "is_pending_approval": full_state.get("is_pending_approval", None),
            "is_admission_topic": full_state.get("is_admission_topic", None),
            "user_context_intent": full_state.get("user_context_intent", []),
            "is_blocked": full_state.get("is_blocked", False),
            "block_notified": full_state.get("block_notified", False),
            "uc": full_state.get("uc", None),
            "latest_segments": full_state.get("latest_segments", []),
            "segment_agent_output": self._build_segment_agent_output(full_state),
        }

    def _build_segment_agent_output(self, full_state: dict[str, Any]) -> dict[str, Any] | None:
        """Return current segment agent output only when it is safe to expose."""
        segment_agent_output = full_state.get("segment_agent_output")
        if not isinstance(segment_agent_output, dict):
            return None

        if (
            full_state.get("is_blocked") is True
            or full_state.get("qna_mode") is True
            or full_state.get("crm_update_state") is True
            or full_state.get("is_admission_topic") is False
            or full_state.get("uc") in SEGMENT_SKIP_UCS
        ):
            return None

        return segment_agent_output

    async def process_admission_query(self, user_id: str, message: str, session_id: str) -> dict[str, Any]:
        """
        Process admission query through multi-agent system.

        FIXED: Only collect response from response_synthesizer_agent
        """
        if not self._initialized:
            raise RuntimeError("AdmissionAgentService not initialized. Call initialize() first.")

        # Input validation
        if not message or not message.strip():
            return {"status": "error", "message": "Vui lòng nhập câu hỏi của bạn.", "session_id": session_id}

        if not user_id:
            return {"status": "error", "message": "Lỗi xác thực người dùng.", "session_id": session_id}

        log_event(
            logger,
            "info",
            "admission_query_start",
            user_id=short_id(user_id),
            session_id=short_id(session_id),
            preview=truncate_text(message, 100),
        )

        try:
            # Ensure session exists with admission state
            await self._ensure_session_exists(user_id, session_id)

            # Create message content for ADK
            message_content = Content(role="user", parts=[Part(text=message.strip())])

            # Configure non-streaming run
            run_config = RunConfig(response_modalities=["TEXT"], streaming_mode=StreamingMode.SSE, max_llm_calls=50)

            log_event(
                logger,
                "debug",
                "admission_query_pipeline_start",
                strategy="response_polishing_agent_only",
            )

            # FIXED: Process with response_synthesizer_agent filtering
            response_text = await self._process_with_synthesizer_filtering(
                user_id=user_id, session_id=session_id, message_content=message_content, run_config=run_config
            )

            # Get updated session state for additional info
            if self.session_service is None:
                raise RuntimeError("Session service is not initialized")
            await self.session_service.get_session(app_name="agents", user_id=user_id, session_id=session_id)

            # Build comprehensive response
            response_data = {
                "status": "success",
                "message": response_text,
                "session_id": session_id,
                "user_id": user_id,
                "processing_info": {
                    "agent_type": "multi_agent_admission",
                    "response_length": len(response_text),
                    # "session_updates": self._get_session_summary(updated_session) if updated_session else {}
                },
            }

            log_event(
                logger,
                "info",
                "admission_query_completed",
                user_id=short_id(user_id),
                session_id=short_id(session_id),
                response_length=len(response_text),
            )

            return response_data

        except Exception as e:
            logger.exception(
                "event=admission_query_error | user_id=%s | session_id=%s | error=%s",
                short_id(user_id),
                short_id(session_id),
                e,
            )

            return {
                "status": "error",
                "message": self._get_user_friendly_error(e),
                "session_id": session_id,
                "user_id": user_id,
            }

    async def _process_with_synthesizer_filtering(
        self, user_id: str, session_id: str, message_content: Content, run_config: RunConfig
    ) -> str:
        """
        ðŸŽ¯ CORE FIX: Only collect responses from response_synthesizer_agent
        """

        if self.runtime is None:
            raise RuntimeError("Runtime is not initialized")

        message_parts = message_content.parts
        if not message_parts:
            logger.warning("Message content is empty")
        message_text = message_parts[0].text
        if not isinstance(message_text, str) or not message_text.strip():
            logger.warning("Message text is empty")

        # Create generator
        generator = self.runtime.run_async(
            user_id=user_id,
            session_id=session_id,
            message=message_text,
            run_config=run_config,
        )

        # Track responses by agent
        synthesizer_responses = []
        all_agents_found = set()
        total_events = 0

        start_time = time.time()
        timeout_seconds = 300

        try:

            async def safe_iterate():
                nonlocal total_events
                async for event in generator:
                    total_events += 1

                    # Safety timeout
                    if time.time() - start_time > timeout_seconds:
                        log_event(logger, "warning", "admission_filter_timeout", timeout_seconds=timeout_seconds)
                        return

                    # Get event author
                    event_author = getattr(event, "author", "unknown")
                    all_agents_found.add(event_author)

                    logger.debug(
                        "event=admission_generator_event | index=%s | author=%s | type=%s",
                        total_events,
                        event_author,
                        type(event).__name__,
                    )

                    #  KEY FIX: Only process response_synthesizer_agent
                    if event_author == "response_polishing_agent":
                        log_event(logger, "debug", "admission_filter_response_agent_event")

                        # Extract content
                        content_text = ""
                        if hasattr(event, "content") and event.content:
                            if hasattr(event.content, "parts") and event.content.parts:
                                for part in event.content.parts:
                                    if hasattr(part, "text") and part.text:
                                        content_text += part.text

                        if content_text.strip():
                            synthesizer_responses.append(content_text)
                            log_event(
                                logger,
                                "debug",
                                "admission_filter_response_collected",
                                response_length=len(content_text),
                                preview=truncate_text(content_text, 150),
                            )

                    # Don't break early - let generator complete naturally

            # Execute with timeout protection
            await asyncio.wait_for(safe_iterate(), timeout=timeout_seconds)

        except TimeoutError:
            logger.warning("event=admission_generator_timeout | timeout_seconds=%s", timeout_seconds)
        except Exception as e:
            logger.exception("event=admission_generator_error | error=%s", e)

        # Process results
        elapsed_time = time.time() - start_time

        log_event(
            logger,
            "info",
            "admission_filter_summary",
            duration_seconds=round(elapsed_time, 2),
            total_events=total_events,
            agents=sorted(all_agents_found),
            response_count=len(synthesizer_responses),
        )

        if synthesizer_responses:
            # Combine all synthesizer responses
            final_response = "".join(synthesizer_responses)
            log_event(logger, "info", "admission_filter_success", final_length=len(final_response))
            return final_response
        else:
            log_event(
                logger,
                "error",
                "admission_filter_no_response",
                agents=sorted(all_agents_found),
            )

            # Debugging suggestions
            if "response_synthesizer_agent" not in all_agents_found:
                log_event(
                    logger, "warning", "admission_filter_agent_not_found", missing_agent="response_synthesizer_agent"
                )

            return "Hệ thống multi-agent đã hoạt động nhưng response synthesizer không tạo được phản hồi. Vui lòng kiểm tra cấu hình agent."

    def _extract_response_text(self, result) -> str:
        """Extract response text from agent result."""
        try:
            if hasattr(result, "content") and result.content and result.content.parts:
                return "".join(part.text for part in result.content.parts if hasattr(part, "text"))
            elif isinstance(result, str):
                return result
            else:
                logger.debug("No extractable content from result")
                return ""
        except Exception as e:
            logger.exception("event=admission_extract_response_error | error=%s", e)
            return ""

    def _get_session_summary(self, session) -> dict[str, Any]:
        """Get session summary for monitoring."""
        try:
            if not session or not hasattr(session, "state"):
                return {}

            state = session.state
            customer_info = state.get("customer_info", {})
            customer_features = state.get("customer_features", {})

            # Calculate completeness
            personal_fields = ["name", "phone", "email", "location", "education_level"]
            completed_personal = sum(1 for field in personal_fields if customer_info.get(field))

            feature_fields = [k for k, v in customer_features.items() if v and v != []]

            return {
                "message_count": state.get("user_context", {}).get("message_count", 0),
                "customer_name": customer_info.get("name", "Unknown"),
                "completeness": {
                    "customer_info": f"{completed_personal}/{len(personal_fields)}",
                    "customer_features": f"{len(feature_fields)}/9",
                },
                "latest_segments": state.get("latest_segments", ""),
                "last_query": state.get("query_results", {}).get("query", ""),
            }
        except Exception as e:
            logger.exception("event=admission_session_summary_error | error=%s", e)
            return {"error": str(e)}

    def _get_user_friendly_error(self, error: Exception) -> str:
        """Convert technical errors to user-friendly Vietnamese messages."""
        error_str = str(error).lower()

        if "timeout" in error_str:
            return "Câu hỏi đang được xử lý quá lâu. Vui lòng thử câu hỏi ngắn gọn hơn."
        elif "connection" in error_str or "network" in error_str:
            return "Có lỗi kết nối. Vui lòng thử lại sau ít phút."
        elif "mcp" in error_str or "server" in error_str:
            return "Hệ thống cơ sở dữ liệu tạm thời bảo trì. Vui lòng thử lại sau."
        elif "session" in error_str:
            return "Lỗi phiên làm việc. Vui lòng refresh và thử lại."
        elif "agent" in error_str:
            return "Hệ thống tư vấn tạm thời không khả dụng. Vui lòng thử lại."
        else:
            return "Có lỗi xảy ra khi xử lý câu hỏi tuyển sinh. Vui lòng thử lại."

    def is_ready(self) -> bool:
        """Check if service is ready to handle admission queries."""
        return self._initialized and self.runtime is not None and self.session_service is not None

    async def health_check(self) -> dict:
        """Comprehensive health check for admission system."""
        health_status = {
            "service": "AdmissionAgentService",
            "initialized": self._initialized,
            "runner": self.runtime is not None,
            "session_service": self.session_service is not None,
            "streaming_mode": "enabled",  #  UPDATED: Now supports streaming
            "multi_agent": True,
            "frappe_integration": True,
            "synthesizer_filtering": True,
            "response_polishing_agent_filter": True,  #  NEW
        }

        if not self._initialized:
            health_status["status"] = "not_initialized"
            return health_status

        try:
            if self.runtime and self.session_service:
                health_status["runner_functional"] = True
                health_status["session_service_functional"] = True
            else:
                health_status["runner_functional"] = False
                health_status["session_service_functional"] = False

            health_status["status"] = (
                "healthy" if all([health_status["runner"], health_status["session_service"]]) else "degraded"
            )

        except Exception as e:
            logger.exception("event=admission_health_check_error | error=%s", e)
            health_status["status"] = "unhealthy"
            health_status["error"] = str(e)

        return health_status

    async def update_state_service(
        self, user_id: str, session_id: str, event_id: str, update_state: dict[str, Any] | None = None
    ):
        """Update session state using EventActions.state_delta"""
        log_event(
            logger,
            "info",
            "admission_update_state_start",
            user_id=short_id(user_id),
            session_id=short_id(session_id),
            update_keys=list((update_state or {}).keys()),
        )

        if update_state is not None:
            state_delta = dict(update_state)

            # Handle is_blocked changes: reset block_notified and spam_count as needed
            if "is_blocked" in state_delta:
                state_delta["block_notified"] = False  # Force re-notification
                if not state_delta["is_blocked"]:
                    state_delta["spam_count"] = 0  # Reset spam count on unblock
            if should_mark_crm_update_state(state_delta):
                state_delta["crm_update_state"] = True

            # Create EventActions with state_delta
            if self.session_service is None:
                raise RuntimeError("Session service is not initialized")
            actions_with_update = EventActions(state_delta=state_delta)
            system_event = Event(
                id=event_id,
                invocation_id=event_id,
                author="user",  # Or 'agent', 'tool' etc.
                actions=actions_with_update,
                # timestamp=time.time(),
            )

            if self.runtime is not None:
                runtime = self.runtime
                async with runtime.get_session_lock(user_id=user_id, session_id=session_id):
                    appended = await self._append_update_state_event_with_retry(
                        user_id=user_id,
                        session_id=session_id,
                        event=system_event,
                        update_keys=list(state_delta.keys()),
                    )
                runtime.process_pending_session_messages(user_id=user_id, session_id=session_id)
            else:
                appended = await self._append_update_state_event_with_retry(
                    user_id=user_id,
                    session_id=session_id,
                    event=system_event,
                    update_keys=list(state_delta.keys()),
                )
            if appended:
                log_event(logger, "info", "admission_update_state_done", session_id=short_id(session_id))

        else:
            log_event(logger, "warning", "admission_update_state_empty_payload", session_id=short_id(session_id))

    def _is_stale_session_error(self, error: Exception) -> bool:
        return isinstance(error, ValueError) and STALE_SESSION_ERROR_MESSAGE in str(error)

    def _is_duplicate_event_error(self, error: Exception) -> bool:
        error_text = str(error).lower()
        return any(marker in error_text for marker in DUPLICATE_EVENT_ERROR_MARKERS)

    def _session_has_event_id(self, session: Any, event_id: str) -> bool:
        events = getattr(session, "events", None) or []
        return any(getattr(event, "id", None) == event_id for event in events)

    async def _event_already_appended(self, user_id: str, session_id: str, event_id: str) -> bool:
        if self.session_service is None:
            raise RuntimeError("Session service is not initialized")
        session = await self.session_service.get_session(app_name="agents", user_id=user_id, session_id=session_id)
        return bool(session and self._session_has_event_id(session, event_id))

    async def _append_update_state_event_with_retry(
        self,
        user_id: str,
        session_id: str,
        event: Event,
        update_keys: list[str],
    ) -> bool:
        """Append a state update event, reloading the session on ADK stale-session errors."""
        if self.session_service is None:
            raise RuntimeError("Session service is not initialized")

        event_id = str(getattr(event, "id", ""))
        for attempt in range(1, UPDATE_STATE_APPEND_MAX_ATTEMPTS + 1):
            session = await self.session_service.get_session(
                app_name="agents",
                user_id=user_id,
                session_id=session_id,
            )
            if not session:
                log_event(
                    logger,
                    "error",
                    "admission_update_state_missing_session",
                    session_id=short_id(session_id),
                    event_id=event_id,
                    attempt=attempt,
                )
                return False

            try:
                log_event(
                    logger,
                    "debug",
                    "admission_update_state_append_attempt",
                    session_id=short_id(session_id),
                    event_id=event_id,
                    attempt=attempt,
                    update_keys=update_keys,
                )
                await self.session_service.append_event(session, event)
                return True
            except Exception as exc:
                if self._is_duplicate_event_error(exc) and await self._event_already_appended(
                    user_id=user_id, session_id=session_id, event_id=event_id
                ):
                    log_event(
                        logger,
                        "info",
                        "admission_update_state_event_already_appended",
                        session_id=short_id(session_id),
                        event_id=event_id,
                        attempt=attempt,
                    )
                    return True

                if self._is_stale_session_error(exc) and attempt < UPDATE_STATE_APPEND_MAX_ATTEMPTS:
                    log_event(
                        logger,
                        "warning",
                        "admission_update_state_stale_session_retry",
                        session_id=short_id(session_id),
                        event_id=event_id,
                        attempt=attempt,
                        backoff_seconds=UPDATE_STATE_APPEND_BACKOFF_SECONDS,
                        update_keys=update_keys,
                    )
                    await asyncio.sleep(UPDATE_STATE_APPEND_BACKOFF_SECONDS)
                    continue

                logger.exception(
                    "event=admission_update_state_append_failed | user_id=%s | session_id=%s | event_id=%s | "
                    "attempt=%s | update_keys=%s",
                    short_id(user_id),
                    short_id(session_id),
                    event_id,
                    attempt,
                    update_keys,
                )
                raise

        return False

    async def get_session_info(self, user_id: str, session_id: str) -> dict[str, Any]:
        """Get session information for debugging/monitoring."""
        try:
            if self.session_service is None:
                raise RuntimeError("Session service is not initialized")
            session = await self.session_service.get_session(app_name="agents", user_id=user_id, session_id=session_id)
            # logger.error(f"Session info: {session}")

            if not session:
                return {"status": "session_not_found"}
            log_event(logger, "debug", "admission_session_info_loaded", session_id=short_id(session_id))
            return {
                "status": "success",
                "session_id": session.id,
                "user_id": session.user_id,
                "state": session.state,
            }
        except Exception as e:
            logger.exception(
                "event=admission_session_info_error | user_id=%s | session_id=%s | error=%s",
                short_id(user_id),
                short_id(session_id),
                e,
            )
            return {"status": "error", "message": str(e)}

    async def shutdown(self):
        """Clean shutdown of admission agent service."""
        try:
            self._initialized = False
            self.runner = None
            self.session_service = None
            log_event(logger, "info", "admission_service_shutdown_done")

        except Exception as e:
            logger.exception("event=admission_service_shutdown_error | error=%s", e)


# Global singleton instance
_admission_agent_service: AdmissionAgentService | None = None


async def get_admission_agent_service() -> AdmissionAgentService:
    """Get global AdmissionAgentService instance (singleton)."""
    global _admission_agent_service

    if _admission_agent_service is None:
        log_event(logger, "info", "admission_service_singleton_create")
        _admission_agent_service = AdmissionAgentService()
        await _admission_agent_service.initialize()

    return _admission_agent_service


# agentops.validate_trace_spans(trace_context=tracer)
async def shutdown_admission_agent_service():
    """Shutdown global AdmissionAgentService instance."""
    global _admission_agent_service

    if _admission_agent_service:
        await _admission_agent_service.shutdown()
        _admission_agent_service = None
        log_event(logger, "info", "admission_service_singleton_shutdown")
