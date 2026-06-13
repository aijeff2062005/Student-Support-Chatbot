"""
Admission WebSocket Handler - Pure Streaming Integration

 NEW: Pure streaming WebSocket endpoint for university admission system with:
- STREAMING responses from answer_query_agent and counselor_playbook_agent
- Multi-agent admission workflow
- Frappe session integration
- Extra data from session state
"""

import asyncio
import json
import logging
import time
import traceback
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from starlette.middleware.cors import CORSMiddleware

# import the admission agent service
from admission_agent_service import get_admission_agent_service, shutdown_admission_agent_service
from configs.config_service import get_settings
from crud.webhook import build_default_greeting_message, get_webhook_friendly_error, process_webhook_admission
from routers import update_agent_history, validate_username
from schemas.webhook_crm_integrate import (
    WebhookAdmissionRequest,
    WebhookAdmissionResponse,
    WebhookAdmissionUpdateStateRequest,
    WebhookErrorResponse,
)
from utils.logging_config import configure_logging, log_event, short_id, truncate_text

configure_logging()
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager
    Handles startup and shutdown of services
    """
    # Startup
    log_event(logger, "info", "app_starting", mode="streaming")

    try:
        # Pre-initialize admission service
        admission_service = await get_admission_agent_service()

        if admission_service.is_ready():
            log_event(logger, "info", "app_ready")
        else:
            log_event(logger, "warning", "app_not_ready")

    except Exception as e:
        logger.exception("event=app_start_error | error=%s", e)

    yield

    # Shutdown
    log_event(logger, "info", "app_stopping")

    try:
        await shutdown_admission_agent_service()
        log_event(logger, "info", "app_stopped")

    except Exception as e:
        logger.exception("event=app_stop_error | error=%s", e)


# FastAPI app instance with lifespan
app = FastAPI(
    title="Admission Multi-Agent System - Streaming",
    lifespan=lifespan,
    # root_path=settings.root_path,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _safe_send_json(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    data: dict,
) -> bool:
    """
    Send JSON data over the WebSocket safely, handling already-closed connections.
    """
    try:
        async with send_lock:
            await websocket.send_json(data)
        return True
    except (RuntimeError, WebSocketDisconnect):
        return False


def _build_ws_dedupe_key(data: dict[str, Any]) -> str | None:
    """Build a stable key for suppressing duplicate streamed chunks."""
    if data.get("event") != "message":
        return None

    status = data.get("status")
    if status not in {"streaming", "completed"}:
        return None

    event_id = data.get("event_id")
    if not event_id:
        return None

    author = data.get("author", "")
    session_id = data.get("session_id", "")
    user_id = data.get("user_id", "")
    return f"{session_id}|{user_id}|{author}|{status}|{event_id}"


async def _safe_send_json_dedup(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    data: dict[str, Any],
    sent_cache: deque[str],
    sent_index: set[str],
    max_cache_size: int = 2048,
) -> bool:
    """
    Send JSON data with duplicate suppression based on event metadata.
    """
    dedupe_key = _build_ws_dedupe_key(data)

    try:
        async with send_lock:
            if dedupe_key is not None and dedupe_key in sent_index:
                return True

            await websocket.send_json(data)

            if dedupe_key is not None:
                sent_cache.append(dedupe_key)
                sent_index.add(dedupe_key)
                if len(sent_cache) > max_cache_size:
                    expired = sent_cache.popleft()
                    sent_index.discard(expired)
        return True
    except (RuntimeError, WebSocketDisconnect):
        return False


async def _send_fallback_stream_error(
    ws: WebSocket, send_lock: asyncio.Lock, session_id: str, user_id: str, error: Exception
) -> None:
    logger.exception(
        "event=stream_handler_error | session_id=%s | user_id=%s | error=%s",
        short_id(session_id),
        short_id(user_id),
        error,
    )
    await _safe_send_json(
        ws,
        send_lock,
        {
            "event": "error",
            "data": "Có lỗi xảy ra khi xử lý câu hỏi. Vui lòng thử lại.",
            "session_id": session_id,
            "status": "error",
        },
    )


async def _parse_ws_message(raw_message: str, ws: WebSocket, send_lock: asyncio.Lock, session_id: str) -> dict | None:
    try:
        return json.loads(raw_message)
    except json.JSONDecodeError as e:
        logger.error("event=ws_invalid_json | session_id=%s | error=%s", short_id(session_id), e)
        await _safe_send_json(
            ws,
            send_lock,
            {
                "event": "error",
                "data": "Invalid message format. Please send valid JSON.",
                "status": "error",
            },
        )
        return None


async def _ensure_session_for_greeting(
    admission_service: Any, user_id: str, session_id: str, customer_info: dict, ws: WebSocket, send_lock: asyncio.Lock
) -> tuple[Any, bool] | None:
    try:
        return await admission_service.ensure_admission_session(
            user_id=user_id,
            session_id=session_id,
            customer_info=customer_info,
        )
    except Exception as session_error:
        logger.exception(
            "event=ws_greeting_session_error | session_id=%s | user_id=%s | error=%s",
            short_id(session_id),
            short_id(user_id),
            session_error,
        )
        await _safe_send_json(
            ws,
            send_lock,
            {
                "event": "error",
                "data": "Không thể khởi tạo phiên làm việc. Vui lòng thử lại.",
                "session_id": session_id,
                "status": "error",
            },
        )
        return None


async def handle_streaming_admission_message(
    ws: WebSocket,
    send_lock: asyncio.Lock,
    sent_cache: deque[str],
    sent_index: set[str],
    message_data: dict,
    session_id: str,
    customer_info: dict | None = None,
) -> None:
    """
     Handle streaming admission query processing.

    Process:
    1. Extract user info and message
    Streams responses from answer_query_agent and counselor_playbook_agent
    3. Send completed with extra_data from session state

    Args:
        ws: WebSocket connection
        message_data: Request data with session_id, user_id, message
        session_id: Frappe session ID for tracking
        customer_info: Optional customer information dictionary
    """

    # Extract message details
    user_id = message_data.get("user_id", "Unknown")
    message = message_data.get("data", "")
    attachments = message_data.get("attachments")

    # Validation
    has_message = isinstance(message, str) and bool(message.strip())
    has_attachments = bool(attachments)
    if not has_message and not has_attachments:
        await _safe_send_json(
            ws,
            send_lock,
            {
                "event": "error",
                "data": "Vui lòng nhập câu hỏi hoặc gửi ảnh QR cần hỗ trợ",
                "session_id": session_id,
                "status": "error",
            },
        )
        return

    if not user_id or user_id == "Unknown":
        await _safe_send_json(
            ws,
            send_lock,
            {"event": "error", "data": "Thiếu thông tin người dùng", "session_id": session_id, "status": "error"},
        )
        return

    log_event(
        logger,
        "info",
        "stream_message_received",
        user_id=short_id(user_id),
        session_id=short_id(session_id),
        preview=truncate_text(message, 100),
    )

    try:
        # Get admission service
        admission_service = await get_admission_agent_service()

        if not admission_service.is_ready():
            await _safe_send_json(
                ws,
                send_lock,
                {
                    "event": "error",
                    "data": "Hệ thống chưa sẵn sàng. Vui lòng thử lại.",
                    "session_id": session_id,
                    "status": "error",
                },
            )
            return

        #  STREAM RESPONSE CHUNKS
        accumulated = ""
        async for chunk in admission_service.stream_query_admission(
            user_id=user_id,
            message=message,
            session_id=session_id,
            customer_info=customer_info,
            attachments=attachments,
        ):
            # Track accumulated response for logging
            if chunk.get("status") == "streaming":
                accumulated += chunk.get("data", "")

            # Send chunk to client
            sent = await _safe_send_json_dedup(
                websocket=ws,
                send_lock=send_lock,
                data=chunk,
                sent_cache=sent_cache,
                sent_index=sent_index,
            )
            if not sent:
                return

            # Log completion
            if chunk.get("status") == "completed":
                # logger.info(f"Streaming completed: {chunk.get('total_length')} chars")

                #  LOG extra_data presence
                chunk.get("extra_data", {})
                # logger.info(f"Extra data keys: {list(extra_data.keys())}")

                #  LOG session_updates presence
                processing_info = chunk.get("processing_info", {})
                session_updates = processing_info.get("session_updates", {})
                log_event(
                    logger,
                    "debug",
                    "stream_completed",
                    session_id=short_id(session_id),
                    has_session_updates=bool(session_updates),
                )
                # break

            # Stop on error
            elif chunk.get("event") == "error":
                log_event(
                    logger,
                    "error",
                    "stream_chunk_error",
                    session_id=short_id(session_id),
                    detail=truncate_text(chunk.get("data"), 180),
                )
                # break

    except Exception as e:
        await _send_fallback_stream_error(ws=ws, send_lock=send_lock, session_id=session_id, user_id=user_id, error=e)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, sid: str | None = Query(None, description="Frappe Session ID")):
    await ws.accept()
    send_lock = asyncio.Lock()
    sent_chunk_cache: deque[str] = deque()
    sent_chunk_index: set[str] = set()
    pending_tasks: set[asyncio.Task] = set()

    session_id = sid

    log_event(logger, "info", "ws_connected", session_id=short_id(session_id))

    try:
        # ============================================================================
        #  STEP 1: Parse initial connection payload
        # ============================================================================
        initial_connect = await ws.receive_json()
        log_event(logger, "debug", "ws_init_payload_received", session_id=short_id(session_id))

        # Extract fields from payload
        user_id = initial_connect.get("user_id", "")
        received_session_id = initial_connect.get("session_id", "")
        customer_info = initial_connect.get("customer_info", {})
        message = initial_connect.get("data", "")  # Optional message field
        attachments = initial_connect.get("attachments")

        full_name = customer_info.get("full_name", "")

        # Override session_id from payload if available
        if received_session_id:
            session_id = received_session_id

        # Validation
        if not user_id:
            log_event(logger, "warning", "ws_init_missing_user_id", session_id=short_id(session_id))
            await _safe_send_json(
                ws, send_lock, {"event": "error", "data": "Thiếu thông tin user_id", "status": "error"}
            )
            return

        if not session_id:
            log_event(logger, "warning", "ws_init_missing_session_id", user_id=short_id(user_id))
            await _safe_send_json(
                ws, send_lock, {"event": "error", "data": "Thiếu thông tin session_id", "status": "error"}
            )
            return

        log_event(
            logger,
            "info",
            "ws_init_context",
            user_id=short_id(user_id),
            session_id=short_id(session_id),
            has_message=bool(message and message.strip()),
            has_full_name=bool(full_name),
        )

        # ============================================================================
        #  STEP 2: Check if session exists in database
        # ============================================================================
        admission_service = await get_admission_agent_service()
        if admission_service.session_service is None:
            raise RuntimeError("Session service is not initialized")

        session = await admission_service.session_service.get_session(
            app_name="agents", user_id=user_id, session_id=session_id
        )

        # ============================================================================
        #  STEP 3: Handle based on session status
        # ============================================================================
        if session is None:
            #  NEW SESSION - Send greeting only
            log_event(logger, "info", "ws_new_session", session_id=short_id(session_id), user_id=short_id(user_id))

            session_ready = await _ensure_session_for_greeting(
                admission_service=admission_service,
                user_id=user_id,
                session_id=session_id,
                customer_info=customer_info,
                ws=ws,
                send_lock=send_lock,
            )
            if session_ready is None:
                return
            session, session_created = session_ready
            log_event(
                logger,
                "info",
                "ws_session_ready_for_greeting",
                session_id=short_id(session_id),
                user_id=short_id(user_id),
                session_created=session_created,
            )

            log_event(logger, "info", "ws_greeting_sent", session_id=short_id(session_id), user_id=short_id(user_id))

            await _safe_send_json(
                ws,
                send_lock,
                {
                    "event": "message",
                    "data": build_default_greeting_message(
                        user_id=user_id,
                        customer_info=customer_info,
                        session_state=session.state if session else {},
                    ),
                    "session_id": session_id,
                    "user_id": user_id,
                },
            )

            # Note: We don't process the message field for new sessions
            # Client always sends an initial greeting that user doesn't see
            if message and message.strip():
                log_event(logger, "debug", "ws_ignore_initial_client_greeting", session_id=short_id(session_id))

        else:
            #  EXISTING SESSION - Reconnection
            log_event(logger, "info", "ws_reconnected", session_id=short_id(session_id), user_id=short_id(user_id))

            if (isinstance(message, str) and message.strip()) or attachments:
                log_event(
                    logger,
                    "info",
                    "ws_reconnect_message_processing",
                    session_id=short_id(session_id),
                    user_id=short_id(user_id),
                    preview=truncate_text(message, 100),
                )

                # Prepare message_data for streaming handler
                message_data = {
                    "user_id": user_id,
                    "data": message,
                    "session_id": session_id,
                    "attachments": attachments,
                }

                # Process message via streaming handler asynchronously
                task = asyncio.create_task(
                    handle_streaming_admission_message(
                        ws=ws,
                        send_lock=send_lock,
                        sent_cache=sent_chunk_cache,
                        sent_index=sent_chunk_index,
                        message_data=message_data,
                        session_id=session_id,
                        customer_info=customer_info,
                    )
                )
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)
            else:
                log_event(logger, "debug", "ws_reconnect_ack_only", session_id=short_id(session_id))
                # We don't send any message here, just wait for user's next message

        # Store customer_info for message loop
        initial_info = customer_info

        # ============================================================================
        #  STEP 4: Continue with message loop
        # ============================================================================
        while True:
            try:
                # Receive message
                raw_message = await ws.receive_text()
                log_event(logger, "debug", "ws_message_raw", session_id=short_id(session_id), preview=raw_message[:100])

                message_data = await _parse_ws_message(raw_message, ws=ws, send_lock=send_lock, session_id=session_id)
                if message_data is None:
                    continue

                # Update frappe_session_id from message if not from query param
                if not session_id:
                    session_id = message_data.get("session_id", "")

                # Get event type
                event_type = message_data.get("event", "message")

                if event_type == "message":
                    #  HANDLE STREAMING MESSAGE CONCURRENTLY
                    task = asyncio.create_task(
                        handle_streaming_admission_message(
                            ws=ws,
                            send_lock=send_lock,
                            sent_cache=sent_chunk_cache,
                            sent_index=sent_chunk_index,
                            message_data=message_data,
                            session_id=session_id,
                            customer_info=initial_info,
                        )
                    )
                    pending_tasks.add(task)
                    task.add_done_callback(pending_tasks.discard)

                elif event_type == "stop_stream":
                    log_event(logger, "info", "ws_stream_stop_requested", session_id=short_id(session_id))
                    await _safe_send_json(
                        ws,
                        send_lock,
                        {"event": "stream_stopped", "data": "Stream stopped by user", "session_id": session_id},
                    )
                    continue

                else:
                    log_event(
                        logger,
                        "warning",
                        "ws_unsupported_event_type",
                        session_id=short_id(session_id),
                        event_type=event_type,
                    )
                    await _safe_send_json(
                        ws,
                        send_lock,
                        {"event": "error", "data": f"Unsupported event type: {event_type}", "status": "error"},
                    )

            except Exception as receive_error:
                logger.exception(
                    "event=ws_receive_error | session_id=%s | error=%s", short_id(session_id), receive_error
                )
                break

    except WebSocketDisconnect:
        log_event(logger, "info", "ws_disconnected", session_id=short_id(session_id))

    except Exception as e:
        logger.exception("event=ws_error | session_id=%s | error=%s", short_id(session_id), e)

    finally:
        log_event(logger, "info", "ws_cleanup_completed", session_id=short_id(session_id))
        # Ensure pending tasks gracefully shutdown
        if pending_tasks:
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)


@app.get("/health/admission")
async def admission_health_check():
    """HTTP health check endpoint for admission system."""
    try:
        admission_service = await get_admission_agent_service()
        health = await admission_service.health_check()

        return {
            "service": "AdmissionMultiAgentSystem",
            "status": health.get("status", "unknown"),
            "streaming_mode": "enabled",
            "health": health,
            "endpoints": {"websocket": "/ws", "health": "/health/admission"},
        }
    except Exception as e:
        logger.exception("event=health_check_error | error=%s", e)
        return {"service": "AdmissionMultiAgentSystem", "status": "error", "error": str(e)}


@app.post(
    "/webhook/greeting",
    response_model=list[WebhookAdmissionResponse],
    responses={
        400: {"model": WebhookErrorResponse, "description": "Bad Request"},
        503: {"model": WebhookErrorResponse, "description": "Service Unavailable"},
        500: {"model": WebhookErrorResponse, "description": "Internal Server Error"},
    },
    summary="Create admission session and return default greeting",
    description="First-contact webhook endpoint for CRM integration with standardized field names (user_id, session_id)",
)
async def webhook_greeting_endpoint(request_data: WebhookAdmissionRequest) -> list[WebhookAdmissionResponse]:
    """Create or reuse a session, then return the standard greeting without invoking agents."""

    try:
        if not request_data.user_id.strip():
            log_event(
                logger, "warning", "webhook_greeting_invalid_user_id", session_id=short_id(request_data.session_id)
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "error",
                    "error": "user_id cannot be empty",
                    "user_id": request_data.user_id,
                    "session_id": request_data.session_id,
                },
            )

        if not request_data.session_id.strip():
            log_event(logger, "warning", "webhook_greeting_invalid_session_id", user_id=short_id(request_data.user_id))
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "error",
                    "error": "session_id cannot be empty",
                    "user_id": request_data.user_id,
                    "session_id": request_data.session_id,
                },
            )

        admission_service = await get_admission_agent_service()

        if not admission_service.is_ready():
            log_event(logger, "warning", "webhook_greeting_service_not_ready")
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "error",
                    "error": "Service not ready. Please try again later.",
                    "user_id": request_data.user_id,
                    "session_id": request_data.session_id,
                },
            )

        session, session_created = await admission_service.ensure_admission_session(
            user_id=request_data.user_id,
            session_id=request_data.session_id,
            customer_info=request_data.customer_info,
            sale_profile=request_data.sale_profile,
            platform=request_data.platform,
        )

        full_state = session.state if session else {}
        response = WebhookAdmissionResponse(
            event_id=str(uuid.uuid4()),
            status="success",
            response=build_default_greeting_message(
                user_id=request_data.user_id,
                customer_info=request_data.customer_info,
                session_state=full_state,
            ),
            user_id=request_data.user_id,
            session_id=request_data.session_id,
            extra_data=full_state.get("extra_data") or {"attachments": []},
            state=admission_service._build_filtered_state(full_state),
            processing_info={
                "event_type": "GREETING",
                "session_created": session_created,
                "message_processed": False,
            },
        )

        return [response]

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "event=webhook_greeting_failed | user_id=%s | session_id=%s",
            short_id(request_data.user_id),
            short_id(request_data.session_id),
        )
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error": get_webhook_friendly_error(e),
                "user_id": request_data.user_id,
                "session_id": request_data.session_id,
            },
        ) from e


@app.post(
    "/webhook/admission",
    # response_model=WebhookAdmissionResponse,
    responses={
        400: {"model": WebhookErrorResponse, "description": "Bad Request"},
        503: {"model": WebhookErrorResponse, "description": "Service Unavailable"},
        500: {"model": WebhookErrorResponse, "description": "Internal Server Error"},
    },
    summary="Process admission query via webhook",
    description="Non-streaming endpoint for CRM integration with standardized field names (user_id, session_id)",
)
async def webhook_admission_endpoint(request_data: WebhookAdmissionRequest) -> list[Any]:
    """
    Process admission query via webhook (non-streaming).

     STANDARDIZED NAMING:
    - user_id: User identifier (was: full_name)
    - session_id: Session/conversation ID (was: conversation)

    Returns only when processing is completed with:
    - Complete response text
    - extra_data from session state
    - processing_info with session updates
    """
    start_time = time.time()

    log_event(
        logger,
        "info",
        "webhook_admission_received",
        session_id=short_id(request_data.session_id),
        user_id=short_id(request_data.user_id),
        preview=truncate_text(request_data.message, 100),
    )

    try:
        # Validate input
        if not request_data.message.strip() and not request_data.attachments:
            log_event(
                logger,
                "warning",
                "webhook_admission_invalid_message",
                session_id=short_id(request_data.session_id),
                user_id=short_id(request_data.user_id),
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "error",
                    "error": "Message cannot be empty",
                    "user_id": request_data.user_id,
                    "session_id": request_data.session_id,
                },
            )

        # Check service ready
        admission_service = await get_admission_agent_service()

        if not admission_service.is_ready():
            log_event(logger, "warning", "webhook_admission_service_not_ready")
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "error",
                    "error": "Service not ready. Please try again later.",
                    "user_id": request_data.user_id,
                    "session_id": request_data.session_id,
                },
            )

        # Process via webhook handler (collects until completed)
        results = await process_webhook_admission(
            user_id=request_data.user_id,
            session_id=request_data.session_id,
            message=request_data.message,
            customer_info=request_data.customer_info,
            sale_profile=request_data.sale_profile,
            platform=request_data.platform,
            attachments=request_data.attachments,
        )

        int((time.time() - start_time) * 1000)
        responses = []

        if isinstance(results, dict):
            results = [results]

        # Handle result
        for result in results:
            if not isinstance(result, dict):
                continue
            log_event(logger, "debug", "webhook_admission_result_item", status=result.get("status"))

            if result.get("status") == "completed":
                # result.pop("extra_data", None)

                extra_data_payload = result.get("extra_data")
                if not isinstance(extra_data_payload, dict):
                    extra_data_payload = None
                state_payload = result.get("state")
                if not isinstance(state_payload, dict):
                    state_payload = None
                processing_info_payload = result.get("processing_info")
                if not isinstance(processing_info_payload, dict):
                    processing_info_payload = None

                response = WebhookAdmissionResponse(
                    event_id=result.get("event_id"),
                    invocation_id=result.get("invocation_id"),
                    status="success",
                    response=str(result.get("data", "")),
                    user_id=str(result.get("user_id") or request_data.user_id),
                    session_id=str(result.get("session_id") or request_data.session_id),
                    extra_data=extra_data_payload,
                    state=state_payload,
                    processing_info=processing_info_payload,
                )

                responses.append(response)

            else:
                # Processing failed
                log_event(
                    logger,
                    "warning",
                    "webhook_admission_processing_failed",
                    detail=truncate_text(result.get("message"), 180),
                )
                raise HTTPException(
                    status_code=204,
                )
        return responses
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise

    except Exception as e:
        logger.exception(
            "event=webhook_admission_error | user_id=%s | session_id=%s | error=%s",
            short_id(request_data.user_id),
            short_id(request_data.session_id),
            e,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error": get_webhook_friendly_error(e),
                "user_id": request_data.user_id,
                "session_id": request_data.session_id,
            },
        ) from e


@app.post(
    "/webhook/update_state",
    response_model=WebhookAdmissionResponse,
    responses={
        400: {"model": WebhookErrorResponse, "description": "Bad Request"},
        503: {"model": WebhookErrorResponse, "description": "Service Unavailable"},
        500: {"model": WebhookErrorResponse, "description": "Internal Server Error"},
    },
    summary="Process admission query via webhook",
    description="Non-streaming endpoint for CRM integration with standardized field names (user_id, session_id)",
)
async def webhook_update_state_endpoint(request_data: WebhookAdmissionUpdateStateRequest) -> WebhookAdmissionResponse:
    time.time()

    log_event(
        logger,
        "info",
        "webhook_update_state_received",
        session_id=short_id(request_data.session_id),
        user_id=short_id(request_data.user_id),
        update_keys=list(
            {
                *list((request_data.update_state or {}).keys()),
                *(["platform"] if request_data.platform is not None else []),
            }
        ),
    )

    try:
        admission_service_update = await get_admission_agent_service()
        event_id = str(uuid.uuid4())
        update_state_payload = dict(request_data.update_state or {})
        if request_data.platform is not None:
            update_state_payload["platform"] = request_data.platform

        if not admission_service_update.is_ready():
            raise HTTPException(status_code=503, detail="Service not ready. Please restart")
        await admission_service_update.update_state_service(
            user_id=request_data.user_id,
            session_id=request_data.session_id,
            event_id=event_id,
            update_state=update_state_payload or None,
        )

        # time.sleep(4)

        results = await process_webhook_admission(
            user_id=request_data.user_id, session_id=request_data.session_id, message=""
        )

        if isinstance(results, list):
            result = results[0] if len(results) > 0 else {"status": "error", "message": "No response"}
        else:
            result = results
        if not isinstance(result, dict):
            result = {"status": "error", "message": "No response"}

        if result.get("status") == "completed":
            state_payload = result.get("state")
            if isinstance(state_payload, dict):
                state_payload.pop("extra_data", None)

            processing_info_payload = result.get("processing_info")
            if not isinstance(processing_info_payload, dict):
                processing_info_payload = {}
            processing_info_payload = {**processing_info_payload, "event_type": "STATE_UPDATE"}

            # processing_time_ms = int((time.time() - start_time_upodate) * 1000)

            extra_data_payload = result.get("extra_data")
            if not isinstance(extra_data_payload, dict):
                extra_data_payload = None

            return WebhookAdmissionResponse(
                event_id=result.get("event_id", ""),
                status="success",
                response=str(result.get("data", "")),
                user_id=request_data.user_id,
                session_id=request_data.session_id,
                extra_data=extra_data_payload,
                state=state_payload if isinstance(state_payload, dict) else None,
                processing_info=processing_info_payload,
            )
        else:
            # Processing failed
            log_event(
                logger,
                "warning",
                "webhook_update_state_processing_failed",
                session_id=short_id(request_data.session_id),
                user_id=short_id(request_data.user_id),
                detail=truncate_text(result.get("message"), 180),
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "status": "error",
                    "error": result.get("message", "Processing failed"),
                    "user_id": request_data.user_id,
                    "session_id": request_data.session_id,
                },
            )

    except Exception as e:
        traceback.print_exc()
        logger.exception(
            "event=webhook_update_state_error | user_id=%s | session_id=%s",
            short_id(request_data.user_id),
            short_id(request_data.session_id),
        )

        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error": get_webhook_friendly_error(e),
                "user_id": request_data.user_id,
                "session_id": request_data.session_id,
            },
        ) from e


@app.post(
    "/webhook/update_blocked_conversation",
    responses={
        400: {"model": WebhookErrorResponse, "description": "Bad Request"},
        503: {"model": WebhookErrorResponse, "description": "Service Unavailable"},
        500: {"model": WebhookErrorResponse, "description": "Internal Server Error"},
    },
    summary="Update block status for a session",
    description="Non-streaming endpoint for CRM integration with standardized field names (user_id, session_id)",
)
async def webhook_update_blocked_state_endpoint(
    request_data: WebhookAdmissionUpdateStateRequest,
) -> dict[str, str | Any]:

    try:
        admission_service_update = await get_admission_agent_service()
        event_id = str(uuid.uuid4())

        if not admission_service_update.is_ready():
            raise HTTPException(status_code=503, detail="Service not ready")
        await admission_service_update.update_state_service(
            user_id=request_data.user_id,
            session_id=request_data.session_id,
            event_id=event_id,
            update_state=request_data.update_state,
        )

        # time.sleep(4)
        # logger.error("before update blocked state")
        results = await admission_service_update.get_session_info(
            user_id=request_data.user_id, session_id=request_data.session_id
        )
        # logger.error("after update blocked state")

        # processing_time_ms = int((time.time() - start_time_upodate) * 1000)

        return {
            "event_id": results.get("event_id", ""),
            "status": "success",
            "user_id": request_data.user_id,
            "session_id": request_data.session_id,
            "state": results.get("state"),
        }

    except Exception as e:
        traceback.print_exc()
        logger.exception(
            "event=webhook_update_state_error | user_id=%s | session_id=%s",
            short_id(request_data.user_id),
            short_id(request_data.session_id),
        )

        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error": get_webhook_friendly_error(e),
                "user_id": request_data.user_id,
                "session_id": request_data.session_id,
            },
        ) from e


app.include_router(update_agent_history.router)
app.include_router(validate_username.router)


if __name__ == "__main__":
    import uvicorn

    log_event(logger, "info", "server_starting", host="0.0.0.0", port=18004)
    uvicorn.run(app, host="0.0.0.0", port=18004, log_level="info")
