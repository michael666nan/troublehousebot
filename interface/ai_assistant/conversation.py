# =============================================================================
# CONVERSATION - Session State and Message History
# =============================================================================
# Manages the active in-memory session for each Telegram chat_id.
#
# Responsibilities:
#   - Track which chat_ids are in chat mode
#   - Store rolling message history for the active session
#   - Seed new sessions from long-term memory
#   - Write new messages to long-term memory
#   - Handle session timeouts
#
# Relationship with memory.py:
#   - conversation.py = fast, in-memory, current session only
#   - memory.py       = slow, on-disk, full history across all sessions
# =============================================================================

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field

import config
from . import memory

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: SESSION DATA STRUCTURE
# =============================================================================

@dataclass
class Session:
    """Represents one active chat session."""
    active: bool = False
    history: list[dict] = field(default_factory=list)
    last_activity: datetime = field(default_factory=datetime.now)

    def is_timed_out(self, timeout_minutes: int) -> bool:
        return datetime.now() - self.last_activity > timedelta(minutes=timeout_minutes)

    def touch(self):
        self.last_activity = datetime.now()


# =============================================================================
# SECTION 2: CONVERSATION MANAGER
# =============================================================================

class ConversationManager:
    """
    Manages all active chat sessions.

    On session start: seeds history from long-term memory.
    On each message: appends to both in-memory history and long-term memory.
    """

    def __init__(self):
        self._sessions: dict[int, Session] = {}

    def _get_or_create(self, chat_id: int) -> Session:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = Session()
        return self._sessions[chat_id]

    # =========================================================================
    # Mode management
    # =========================================================================

    def enter_chat_mode(self, chat_id: int):
        """
        Start a chat session.
        Seeds history from the last N messages in long-term memory.
        """
        session = self._get_or_create(chat_id)
        session.active = True
        session.touch()

        # Seed from long-term memory so Claude has context from past sessions
        seed_count = getattr(config, "AI_MEMORY_SEED_MESSAGES", 10)
        session.history = memory.get_recent(chat_id, seed_count)

        if session.history:
            logger.info(f"💬 Chat started for {chat_id}, seeded with {len(session.history)} messages")
        else:
            logger.info(f"💬 Chat started for {chat_id} (no prior history)")

    def exit_chat_mode(self, chat_id: int):
        """End a chat session and clear in-memory history."""
        session = self._get_or_create(chat_id)
        session.active = False
        session.history = []
        logger.info(f"🔇 Chat ended for {chat_id}")

    def is_active(self, chat_id: int) -> bool:
        """Return True if this chat_id is in chat mode and not timed out."""
        session = self._sessions.get(chat_id)
        if session is None or not session.active:
            return False

        if session.is_timed_out(config.AI_SESSION_TIMEOUT_MINUTES):
            logger.info(f"⏰ Session timed out for {chat_id}")
            self.exit_chat_mode(chat_id)
            return False

        return True

    # =========================================================================
    # History management
    # =========================================================================

    def add_user_message(self, chat_id: int, text: str):
        """Append a user message to both session history and long-term memory."""
        session = self._get_or_create(chat_id)
        entry = {"role": "user", "content": text}
        session.history.append(entry)
        session.touch()
        self._trim(session)

        # Persist to long-term memory
        memory.append_message(chat_id, "user", text)

    def add_assistant_message(self, chat_id: int, text: str):
        """Append an assistant reply to both session history and long-term memory."""
        session = self._get_or_create(chat_id)
        entry = {"role": "assistant", "content": text}
        session.history.append(entry)
        session.touch()

        # Persist to long-term memory
        memory.append_message(chat_id, "assistant", text)

    def get_history(self, chat_id: int) -> list[dict]:
        """Return the current session history."""
        return self._get_or_create(chat_id).history.copy()

    def _trim(self, session: Session):
        """Keep session history within the configured rolling window."""
        max_messages = config.AI_MAX_HISTORY_MESSAGES
        while len(session.history) > max_messages:
            session.history.pop(0)
            if session.history and session.history[0]["role"] == "assistant":
                session.history.pop(0)