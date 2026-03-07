# =============================================================================
# CONVERSATION - Session State and Message History
# =============================================================================
# Manages per-chat message history.
#
# Every plain-text message is handled by the AI — there is no chat mode toggle.
# Timeout clears the history so the next message starts a fresh context.
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


@dataclass
class Session:
    """Represents one chat session."""
    history: list[dict] = field(default_factory=list)
    last_activity: datetime = field(default_factory=datetime.now)

    def is_timed_out(self, timeout_minutes: int) -> bool:
        return datetime.now() - self.last_activity > timedelta(minutes=timeout_minutes)

    def touch(self):
        self.last_activity = datetime.now()


class ConversationManager:
    """
    Manages all chat sessions.

    On first message (or after timeout): seeds history from long-term memory.
    On each message: appends to both in-memory history and long-term memory.
    Timeout clears in-memory history — next message starts fresh.
    """

    def __init__(self):
        self._sessions: dict[int, Session] = {}

    def _get_or_create(self, chat_id: int) -> Session:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = Session()
        return self._sessions[chat_id]

    def _check_timeout(self, chat_id: int):
        """Clear history if session has timed out since last message."""
        session = self._sessions.get(chat_id)
        if session and session.history and session.is_timed_out(config.AI_SESSION_TIMEOUT_MINUTES):
            session.history = []
            logger.info(f"⏰ Session timed out for {chat_id} — history cleared")

    def add_user_message(self, chat_id: int, text: str):
        """Append a user message to session history and long-term memory."""
        self._check_timeout(chat_id)
        session = self._get_or_create(chat_id)

        # Seed from long-term memory if starting fresh
        if not session.history:
            seed_count = getattr(config, "AI_MEMORY_SEED_MESSAGES", 0)
            if seed_count:
                session.history = memory.get_recent(chat_id, seed_count)
                if session.history:
                    logger.info(f"💬 Seeded {len(session.history)} messages from memory for {chat_id}")

        session.history.append({"role": "user", "content": text})
        session.touch()
        self._trim(session)
        memory.append_message(chat_id, "user", text)

    def add_assistant_message(self, chat_id: int, text: str):
        """Append an assistant reply to session history and long-term memory."""
        session = self._get_or_create(chat_id)
        session.history.append({"role": "assistant", "content": text})
        session.touch()
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