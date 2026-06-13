import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.apps.app import App
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from utils.logging_config import get_logger, log_event, short_id, truncate_text

logger = get_logger(__name__)

# Maximum number of messages allowed in queue per session
MAX_QUEUE_SIZE = 100


@dataclass
class QueuedMessage:
    message: str
    queue: asyncio.Queue
    role: str = "user"
    state_updates: dict[str, Any] | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)


class SessionQueue:
    """Manages message queue for a specific session."""

    def __init__(self, max_size: int = MAX_QUEUE_SIZE):
        # Store queued messages and their continuous responses
        self.queue: deque[QueuedMessage] = deque()
        self.max_size = max_size
        self.lock = asyncio.Lock()
        self.is_processing = False

    def add_message(
        self,
        message: str,
        queue: asyncio.Queue,
        role: str = "user",
        state_updates: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> bool:
        """
        Add message to queue.

        Returns:
            True if message was added, False if queue is full.
        """
        if len(self.queue) >= self.max_size:
            log_event(
                logger,
                "warning",
                "runtime_queue_limit_reached",
                max_size=self.max_size,
            )
            return False
        self.queue.append(
            QueuedMessage(
                message,
                queue=queue,
                role=role,
                state_updates=state_updates,
                kwargs=kwargs,
            )
        )
        return True

    def get_all_pending_messages(self) -> list[QueuedMessage]:
        """Get and clear all pending messages."""
        messages = list(self.queue)
        self.queue.clear()
        return messages

    def has_pending_messages(self) -> bool:
        """Check if there are pending messages."""
        return len(self.queue) > 0


class BaseRuntime:
    """
    A base runtime for executing ADK agents with message queuing support.

    This class handles the complexity of ADK App and Runner initialization,
    providing a simplified interface for running agents with automatic message batching.
    """

    def __init__(
        self,
        app_name: str,
        agent: BaseAgent,
        session_service: Any | None = None,
        artifact_service: Any | None = None,
        streaming: bool = False,
        message_separator: str = "\n\n---\n\n",
    ):
        """
        Initializes the runtime.

        Args:
            app_name: The name of the application.
            agent: The root agent to execute.
            session_service: Optional session service. Defaults to InMemorySessionService.
            streaming: Whether to enable streaming mode.
            message_separator: Separator used when combining multiple messages.
        """
        self.app_name = app_name
        self.agent = agent
        self.message_separator = message_separator

        # Initialize App
        self.app = App(
            name=app_name,
            root_agent=self.agent,
        )

        # Initialize Runner
        self.session_service = session_service or InMemorySessionService()
        self.artifact_service = artifact_service
        self.runner = Runner(
            app=self.app,
            session_service=self.session_service,
            artifact_service=self.artifact_service,
        )
        self.run_config = RunConfig(
            response_modalities=["TEXT"],
            streaming_mode=StreamingMode.SSE if streaming else StreamingMode.NONE,
        )

        # Session queues: {session_key: SessionQueue}
        self._session_queues: dict[str, SessionQueue] = {}

        log_event(logger, "info", "runtime_initialized", app_name=app_name)

    def _get_session_key(
        self,
        user_id: str,
        session_id: str,
    ) -> str:
        """Generate unique key for session."""
        return f"{user_id}:{session_id}"

    def _get_or_create_queue(
        self,
        user_id: str,
        session_id: str,
    ) -> SessionQueue:
        """Get or create queue for session."""
        session_key = self._get_session_key(user_id, session_id)
        if session_key not in self._session_queues:
            self._session_queues[session_key] = SessionQueue()
        return self._session_queues[session_key]

    def get_session_lock(
        self,
        user_id: str,
        session_id: str,
    ) -> asyncio.Lock:
        """Return the in-process lock used to serialize work for a session."""
        return self._get_or_create_queue(user_id, session_id).lock

    def process_pending_session_messages(
        self,
        user_id: str,
        session_id: str,
    ) -> None:
        """Start backlog processing after external code releases a session lock."""
        session_queue = self._get_or_create_queue(user_id, session_id)
        if session_queue.has_pending_messages() and not session_queue.lock.locked():
            asyncio.create_task(self._process_backlog(user_id, session_id))

    async def _ensure_session_async(
        self,
        user_id: str,
        session_id: str,
    ):
        """Ensures a session exists asynchronously."""
        session = await self.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if not session:
            log_event(
                logger,
                "info",
                "runtime_session_create",
                user_id=short_id(user_id),
                session_id=short_id(session_id),
            )
            await self.session_service.create_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id,
            )

    async def _process_message(
        self,
        user_id: str,
        session_id: str,
        message: str,
        role: str = "user",
        state_updates: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Event, None]:
        """
        Internal method to actually process a message with the agent.
        """
        log_event(
            logger,
            "debug",
            "runtime_message_processing",
            user_id=short_id(user_id),
            session_id=short_id(session_id),
            preview=truncate_text(message, 100),
        )

        # If state updates are provided, dispatch a separate event first
        if state_updates:
            state_event = Event(
                author="user",
                actions=EventActions(state_delta=state_updates),
            )
            # Retrieve session object to append event manually
            session = await self.session_service.get_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id,
            )
            if session:
                log_event(
                    logger,
                    "debug",
                    "runtime_state_update_applying",
                    session_id=short_id(session_id),
                    update_keys=list((state_updates or {}).keys()),
                )
                await self.session_service.append_event(session, state_event)
                log_event(logger, "debug", "runtime_state_update_applied", session_id=short_id(session_id))
            else:
                log_event(logger, "error", "runtime_state_update_missing_session", session_id=short_id(session_id))

        content = types.Content(
            role=role,
            parts=[types.Part.from_text(text=message)],
        )

        run_config = kwargs.pop("run_config", self.run_config)

        async for event in self.runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
            run_config=run_config,
            **kwargs,
        ):
            yield event

    async def _process_backlog(self, user_id: str, session_id: str):
        """
        Background task to process queued messages for a session.
        """
        session_queue = self._get_or_create_queue(user_id, session_id)

        # Acquire lock to ensure exclusive processing of the backlog
        async with session_queue.lock:
            session_queue.is_processing = True
            try:
                while session_queue.has_pending_messages():
                    # Combine all pending messages
                    pending_items = session_queue.get_all_pending_messages()
                    log_event(
                        logger,
                        "debug",
                        "runtime_backlog_combining",
                        session_id=short_id(session_id),
                        pending_count=len(pending_items),
                    )

                    pending_messages = [item.message for item in pending_items]
                    current_queues = [item.queue for item in pending_items]

                    merged_state_updates = None
                    merged_kwargs = {}
                    for item in pending_items:
                        if item.state_updates:
                            if merged_state_updates is None:
                                merged_state_updates = dict(item.state_updates)
                            else:
                                merged_state_updates.update(item.state_updates)
                        if item.kwargs:
                            merged_kwargs.update(item.kwargs)

                    first_role = pending_items[0].role
                    current_message = self.message_separator.join(pending_messages)

                    # Process backlog message
                    log_event(
                        logger,
                        "debug",
                        "runtime_backlog_processing",
                        session_id=short_id(session_id),
                        preview=truncate_text(current_message, 80),
                    )

                    try:
                        async for event in self._process_message(
                            user_id=user_id,
                            session_id=session_id,
                            message=current_message,
                            role=first_role,
                            state_updates=merged_state_updates,
                            **merged_kwargs,
                        ):
                            # Broadcast event to all waiting queues
                            for q in current_queues:
                                await q.put(event)
                    except Exception as e:
                        # Broadcast exception to all queues
                        for q in current_queues:
                            await q.put(e)
                    finally:
                        # Signal completion to all queues
                        for q in current_queues:
                            await q.put(None)

            except Exception as e:
                logger.exception(
                    "event=runtime_backlog_error | session_id=%s | user_id=%s | error=%s",
                    short_id(session_id),
                    short_id(user_id),
                    e,
                )
            finally:
                session_queue.is_processing = False

    async def run_async(
        self,
        user_id: str,
        session_id: str,
        message: str,
        role: str = "user",
        state_updates: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Event, None]:
        """
        Runs the agent asynchronously with message queuing support.
        """
        await self._ensure_session_async(user_id, session_id)

        session_queue = self._get_or_create_queue(user_id, session_id)

        # Try to acquire the lock
        if session_queue.lock.locked():
            # Another message is being processed, add to queue
            # Create a Queue to receive the stream
            response_queue = asyncio.Queue()

            if session_queue.add_message(message, response_queue, role=role, state_updates=state_updates, **kwargs):
                log_event(
                    logger,
                    "info",
                    "runtime_message_queued",
                    session_id=short_id(session_id),
                    user_id=short_id(user_id),
                    queue_size=len(session_queue.queue),
                )

                # Consume from the queue
                while True:
                    item = await response_queue.get()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    yield item
            else:
                # Queue full
                yield Event(
                    author="system",
                    content=types.Content(
                        role="system",
                        parts=[types.Part.from_text(text="Queue limit reached. Please try again later.")],
                    ),
                )
            return

        # Acquire lock and process ONLY the current message
        async with session_queue.lock:
            session_queue.is_processing = True
            try:
                # Process initial message
                log_event(
                    logger,
                    "debug",
                    "runtime_message_processing_direct",
                    session_id=short_id(session_id),
                    user_id=short_id(user_id),
                    preview=truncate_text(message, 80),
                )

                async for event in self._process_message(
                    user_id=user_id,
                    session_id=session_id,
                    message=message,
                    role=role,
                    state_updates=state_updates,
                    **kwargs,
                ):
                    yield event

            except Exception as e:
                logger.exception(
                    "event=runtime_process_error | session_id=%s | user_id=%s | error=%s",
                    short_id(session_id),
                    short_id(user_id),
                    e,
                )
            finally:
                session_queue.is_processing = False

        # After releasing lock, check if there are pending messages to process
        if session_queue.has_pending_messages():
            asyncio.create_task(self._process_backlog(user_id, session_id))

    def get_queue_size(
        self,
        user_id: str,
        session_id: str,
    ) -> int:
        """Get the current queue size for a session."""
        session_key = self._get_session_key(user_id, session_id)
        if session_key in self._session_queues:
            return len(self._session_queues[session_key].queue)
        return 0

    def clear_queue(
        self,
        user_id: str,
        session_id: str,
    ):
        """Clear the queue for a session."""
        session_key = self._get_session_key(user_id, session_id)
        if session_key in self._session_queues:
            self._session_queues[session_key].queue.clear()
            log_event(logger, "debug", "runtime_queue_cleared", session_id=short_id(session_id))

    def cleanup_session(
        self,
        user_id: str,
        session_id: str,
    ):
        """Remove session queue from memory."""
        session_key = self._get_session_key(user_id, session_id)
        if session_key in self._session_queues:
            del self._session_queues[session_key]
            log_event(
                logger,
                "debug",
                "runtime_session_cleaned",
                session_id=short_id(session_id),
                user_id=short_id(user_id),
            )

    def cleanup_all_sessions(self):
        """Remove all session queues from memory."""
        count = len(self._session_queues)
        self._session_queues.clear()
        log_event(logger, "info", "runtime_all_sessions_cleaned", count=count)

    def get_active_session_count(self) -> int:
        """Get the number of active session queues."""
        return len(self._session_queues)
