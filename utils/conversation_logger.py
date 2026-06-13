"""Conversation Logger - Track conversation state changes to JSON.

Logs each turn with full state, agent outputs, and metadata for debugging.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConversationLogger:
    """Logger that saves conversation turns to JSON file."""

    def __init__(self, log_dir: str = "conversation_logs"):
        """Initialize logger with output directory.

        Args:
            log_dir: Directory to save conversation logs
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

    def log_turn(
        self,
        session_id: str,
        turn_number: int,
        user_message: str,
        state_before: dict[str, Any],
        state_after: dict[str, Any],
        query_results: dict[str, Any] | None,
        collection_results: dict[str, Any] | None,
        final_response: str,
        metadata: dict[str, Any] | None = None,
        debug_info: dict[str, Any] | None = None,
    ):
        """Log a single conversation turn.

        Args:
            session_id: Session identifier
            turn_number: Turn number in conversation
            user_message: User's input message
            state_before: Session state before processing
            state_after: Session state after processing
            query_results: Output from Query Analyzer Agent
            collection_results: Output from Data Collector Agent
            final_response: Final synthesized response
            metadata: Additional metadata (dmn_called, segment_updated, etc.)
        """
        try:
            log_file = self.log_dir / f"{session_id}.json"
            conversation_log: dict[str, Any]

            # Load existing log or create new
            if log_file.exists():
                with open(log_file, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    conversation_log = dict(loaded)
                else:
                    conversation_log = {}
            else:
                conversation_log = {"session_id": session_id, "started_at": datetime.now().isoformat(), "turns": []}

            if not isinstance(conversation_log.get("turns"), list):
                conversation_log["turns"] = []

            # Create turn entry
            turn_entry = {
                "turn": turn_number,
                "timestamp": datetime.now().isoformat(),
                "user_message": user_message,
                "state_before": self._serialize_state(state_before),
                "state_after": self._serialize_state(state_after),
                "agent_outputs": {"query_analyzer": query_results, "data_collector": collection_results},
                "final_response": final_response,
                "metadata": metadata or {},
                "state_changes": self._compute_state_changes(state_before, state_after),
                "debug_info": debug_info or {},
            }

            # Add turn
            turns = conversation_log.get("turns")
            if not isinstance(turns, list):
                turns = []
                conversation_log["turns"] = turns
            turns.append(turn_entry)
            conversation_log["last_updated"] = datetime.now().isoformat()
            conversation_log["total_turns"] = len(turns)

            # Save to file
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(conversation_log, f, ensure_ascii=False, indent=2)

            logger.info(f"Logged turn {turn_number} to {log_file}")

        except Exception as e:
            logger.error(f"Failed to log conversation turn: {e}")

    def _serialize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Serialize state for JSON storage.

        Args:
            state: Session state dict

        Returns:
            Serializable state dict
        """
        # Only include persistent state, NOT agent outputs
        # (query_results and collection_results are in agent_outputs separately)
        return {
            "customer_info": state.get("customer_info", {}),
            "customer_features": state.get("customer_features", {}),
            "latest_segments": state.get("latest_segments", ""),
            "user_context": state.get("user_context", {}),
        }

    def _compute_state_changes(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        """Compute what changed between before and after states.

        Args:
            before: State before processing
            after: State after processing

        Returns:
            Dict describing changes
        """
        changes = {
            "customer_info_updated": [],
            "features_updated": [],
            "segment_changed": False,
            "message_count_increased": False,
        }

        # Check personal info changes
        before_personal = before.get("customer_info", {})
        after_personal = after.get("customer_info", {})
        for key in ["name", "phone", "email", "location", "education_level"]:
            if before_personal.get(key) != after_personal.get(key):
                changes["customer_info_updated"].append(key)

        # Check feature changes
        before_features = before.get("customer_features", {})
        after_features = after.get("customer_features", {})
        for key in before_features.keys():
            before_val = before_features.get(key, [])
            after_val = after_features.get(key, [])
            if before_val != after_val:
                changes["features_updated"].append(key)

        # Check segment change
        changes["segment_changed"] = before.get("latest_segments") != after.get("latest_segments")

        # Check message count
        before_count = before.get("user_context", {}).get("message_count", 0)
        after_count = after.get("user_context", {}).get("message_count", 0)
        changes["message_count_increased"] = after_count > before_count

        return changes

    def get_conversation_summary(self, session_id: str) -> dict[str, Any] | None:
        """Get summary of conversation from log file.

        Args:
            session_id: Session identifier

        Returns:
            Conversation summary or None if not found
        """
        try:
            log_file = self.log_dir / f"{session_id}.json"
            if not log_file.exists():
                return None

            with open(log_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load conversation summary: {e}")
            return None


# Global instance
_conversation_logger = None


def get_conversation_logger() -> ConversationLogger:
    """Get singleton conversation logger instance.

    Returns:
        ConversationLogger instance
    """
    global _conversation_logger
    if _conversation_logger is None:
        _conversation_logger = ConversationLogger()
    return _conversation_logger
