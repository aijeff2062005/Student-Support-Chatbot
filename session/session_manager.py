"""Session Management for Admission Agent System.

Provides SessionService setup and utilities for managing conversation state
across multi-agent workflows with ADK framework.
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.genai.types import Content, Part

logger = logging.getLogger(__name__)


class AdmissionSessionManager:
    """Manages sessions for admission agent conversations."""

    def __init__(self, app_name: str = "admission_agent"):
        """Initialize session manager.

        Args:
                app_name: Application name for session identification
        """
        self.app_name = app_name
        self.session_service = InMemorySessionService()
        logger.info("AdmissionSessionManager initialized")
        logger.info(f"App: {app_name}")
        logger.info("Storage: InMemorySessionService (development mode)")

    def create_initial_state(self) -> dict[str, Any]:
        """Create initial session state template.

        Returns:
                Dict with initialized state structure for admission conversations
        """
        return {
            "user_context": {
                "started_at": datetime.now().isoformat(),
                "last_updated": datetime.now().isoformat(),
                "message_count": 0,
                "current_topic": "",
            },
            # Personal Information
            "customer_info": {
                "name": "",
                "phone": "",
                "email": "",
                "social_links": [],
                "location": "",
                "education_level": "",
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
            # Agent Results
            "query_results": {},
            "collection_results": {},
            # Segmentation Results
            "latest_segments": "",
        }

    async def create_session(self, user_id: str, session_id: str | None = None) -> Session:
        """Create new session for user.

        Args:
                user_id: User identifier
                session_id: Optional specific session ID, generates if None

        Returns:
                Created Session object
        """
        if session_id is None:
            session_id = str(uuid.uuid4())

        initial_state = self.create_initial_state()

        session = await self.session_service.create_session(
            app_name=self.app_name, user_id=user_id, state=initial_state, session_id=session_id
        )

        logger.info(f"Session created: {session_id[:8]}... for user {user_id}")
        return session

    async def get_session(self, user_id: str, session_id: str) -> Session | None:
        """Get existing session.

        Args:
                user_id: User identifier
                session_id: Session identifier

        Returns:
                Session object or None if not found
        """
        try:
            session = await self.session_service.get_session(
                app_name=self.app_name, user_id=user_id, session_id=session_id
            )
            return session
        except Exception as e:
            logger.warning(f"Session not found: {session_id[:8]}... - {e}")
            return None

    async def update_session_context(self, session: Session, message_text: str, topic: str | None = None) -> None:
        """Update session context with new message.

        Args:
                session: Session object to update
                message_text: User message text
                topic: Optional topic update
        """
        try:
            # Update user context
            current_context = session.state.get("user_context", {})
            current_context["last_updated"] = datetime.now().isoformat()
            current_context["message_count"] = current_context.get("message_count", 0) + 1

            if topic:
                current_context["current_topic"] = topic

            # Create state update event
            state_delta = {"user_context": current_context}

            # Create system event to update state
            context_event = Event(
                author="session_manager",
                content=Content(
                    role="system",
                    parts=[Part(text=f"Session context updated: message #{current_context['message_count']}")],
                ),
                actions=EventActions(state_delta=state_delta),
                timestamp=datetime.now().timestamp(),
            )

            await self.session_service.append_event(session, context_event)
            logger.debug(f" Session context updated: msg #{current_context['message_count']}")

        except Exception as e:
            logger.error(f"Failed to update session context: {e}")

    async def get_session_summary(self, session: Session) -> dict[str, Any]:
        """Get summary of session state.

        Args:
                session: Session to summarize

        Returns:
                Dict with session summary
        """
        try:
            state = session.state
            customer_info = state.get("customer_info", {})
            customer_features = state.get("customer_features", {})
            user_context = state.get("user_context", {})

            # Calculate completeness
            personal_fields = ["name", "phone", "email", "location", "education_level"]
            completed_personal = sum(1 for field in personal_fields if customer_info.get(field))

            feature_fields = [k for k, v in customer_features.items() if v and v != "unknown" and v != []]

            return {
                "session_id": session.id,
                "user_id": session.user_id,
                "started_at": user_context.get("started_at"),
                "message_count": user_context.get("message_count", 0),
                "current_topic": user_context.get("current_topic", ""),
                "customer": {
                    "name": customer_info.get("name", "Unknown"),
                    "location": customer_info.get("location", "Unknown"),
                    "email": customer_info.get("email", ""),
                    "phone": customer_info.get("phone", ""),
                },
                "profile_completeness": {
                    "customer_info": f"{completed_personal}/{len(personal_fields)}",
                    "customer_features": f"{len(feature_fields)}/9",
                    "overall_percent": round(
                        (completed_personal + len(feature_fields)) / (len(personal_fields) + 9) * 100
                    ),
                },
                "last_results": {
                    "query": state.get("query_results", {}),
                    "collection": state.get("collection_results", {}),
                },
            }

        except Exception as e:
            logger.error(f"Failed to get session summary: {e}")
            return {"error": str(e)}

    async def list_user_sessions(self, user_id: str) -> dict[str, Any]:
        """List all sessions for a user.

        Args:
                user_id: User identifier

        Returns:
                Dict with session list
        """
        try:
            response = await self.session_service.list_sessions(app_name=self.app_name, user_id=user_id)
            session_ids = getattr(response, "session_ids", None)
            if not isinstance(session_ids, list):
                sessions = getattr(response, "sessions", [])
                if isinstance(sessions, list):
                    session_ids = [
                        getattr(item, "id", None) or getattr(item, "session_id", None)
                        for item in sessions
                        if getattr(item, "id", None) or getattr(item, "session_id", None)
                    ]
                else:
                    session_ids = []

            return {"user_id": user_id, "session_count": len(session_ids), "session_ids": session_ids}

        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return {"error": str(e)}

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        """Delete a session.

        Args:
                user_id: User identifier
                session_id: Session to delete

        Returns:
                True if deleted successfully
        """
        try:
            await self.session_service.delete_session(app_name=self.app_name, user_id=user_id, session_id=session_id)
            logger.info(f"Session deleted: {session_id[:8]}...")
            return True

        except Exception as e:
            logger.error(f"Failed to delete session: {e}")
            return False

    def create_runner(self, agent) -> Runner:
        """Create ADK Runner with session service.

        Args:
                agent: Agent to run

        Returns:
                Configured Runner instance
        """
        return Runner(agent=agent, session_service=self.session_service, app_name=self.app_name)


# Global session manager instance
_session_manager: AdmissionSessionManager | None = None


def get_session_manager() -> AdmissionSessionManager:
    """Get global session manager instance (singleton).

    Returns:
            AdmissionSessionManager instance
    """
    global _session_manager
    if _session_manager is None:
        _session_manager = AdmissionSessionManager()
    return _session_manager
