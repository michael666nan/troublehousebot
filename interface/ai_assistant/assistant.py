# =============================================================================
# ASSISTANT - Main Orchestrator
# =============================================================================
# The single class telegram_bot.py interacts with.
# Wires together conversation, memory, context, tools, and LLM.
#
# Public interface:
#   assistant.enter_chat_mode(chat_id)      -> str
#   assistant.exit_chat_mode(chat_id)       -> str
#   assistant.handle_message(chat_id, text) -> AssistantResponse
#
# AssistantResponse is a dataclass with:
#   .text:       str            — the reply to send as a message
#   .documents:  list[str]      — file paths to send as Telegram documents
#
# Tool call flow:
#   1. Send message + tools to Claude
#   2. Claude responds with tool_use request(s)
#   3. We execute tools against state
#   4. If a tool returned a _chart_file, store the path
#   5. Send tool results back to Claude
#   6. Claude responds with final text
#   7. Return text + any collected document paths to telegram_bot
# =============================================================================

import json
import logging
import asyncio
import os
from dataclasses import dataclass, field

from .conversation import ConversationManager
from .context_builder import build_system_prompt
from .llm_client import get_reply
from .tools.definitions import TOOL_DEFINITIONS
from .tools.handlers import dispatch
from . import memory

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 5


@dataclass
class AssistantResponse:
    """
    Return type from handle_message().

    text:      The reply text to send to Telegram.
    documents: File paths to send as Telegram document attachments.
               Files should be deleted by telegram_bot.py after sending.
    """
    text:      str
    documents: list[str] = field(default_factory=list)


class AIAssistant:

    def __init__(self, state):
        self._state = state
        self._conversation = ConversationManager()
        logger.info("✅ AI Assistant initialized")

    # =========================================================================
    # Public interface
    # =========================================================================

    def enter_chat_mode(self, chat_id: int) -> str:
        self._conversation.enter_chat_mode(chat_id)
        return (
            "🤖 <b>AI Assistant active</b>\n\n"
            "Ask me anything about the heating system, energy prices, weather, "
            "or ask me to plot historical data.\n\n"
            "Type /exit to return to normal commands."
        )

    def exit_chat_mode(self, chat_id: int) -> str:
        self._conversation.exit_chat_mode(chat_id)
        return "👋 Exited AI chat mode. Normal commands are active again."

    async def handle_message(self, chat_id: int, text: str) -> AssistantResponse | None:
        """
        Handle an incoming plain-text message.

        Returns AssistantResponse (text + optional document paths),
        or None if not in chat mode.
        """
        if not self._conversation.is_active(chat_id):
            return None

        self._conversation.add_user_message(chat_id, text)

        system_prompt = build_system_prompt(self._state)
        history       = self._conversation.get_history(chat_id)

        response = await asyncio.get_event_loop().run_in_executor(
            None,
            self._run_tool_loop,
            chat_id,
            system_prompt,
            history,
        )

        self._conversation.add_assistant_message(chat_id, response.text)
        return response

    # =========================================================================
    # Tool call loop
    # =========================================================================

    def _run_tool_loop(
        self,
        chat_id: int,
        system_prompt: str,
        history: list[dict],
    ) -> AssistantResponse:
        """
        Run the LLM + tool call loop synchronously (runs in thread pool).

        Collects any chart file paths returned by plot_history alongside
        the final text reply.
        """
        working_history = list(history)
        collected_docs  = []  # Paths of chart files to send

        for _ in range(_MAX_TOOL_ROUNDS):
            reply_text, tool_calls = get_reply(
                system_prompt=system_prompt,
                history=working_history,
                tools=TOOL_DEFINITIONS,
            )

            if reply_text is not None:
                return AssistantResponse(text=reply_text, documents=collected_docs)

            if not tool_calls:
                return AssistantResponse(
                    text="Sorry, I received an unexpected response. Please try again.",
                    documents=collected_docs,
                )

            logger.info(f"🔧 Tool calls: {[t['name'] for t in tool_calls]}")

            tool_results = []
            for tool_call in tool_calls:
                # Log to memory
                memory.append_tool_call(
                    chat_id,
                    tool_id=tool_call["id"],
                    name=tool_call["name"],
                    input=tool_call["input"],
                )

                # Execute
                result = dispatch(tool_call["name"], tool_call["input"], self._state, chat_id=chat_id)
                logger.info(f"🔧 {tool_call['name']} → {result}")

                # Collect chart files — don't pass the path to Claude,
                # just tell it the chart was generated
                if "_chart_file" in result:
                    filepath = result.pop("_chart_file")
                    result.pop("_filename", None)
                    if os.path.exists(filepath):
                        collected_docs.append(filepath)

                # Log result to memory
                memory.append_tool_result(
                    chat_id,
                    tool_id=tool_call["id"],
                    name=tool_call["name"],
                    content=result,
                )

                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tool_call["id"],
                    "content":     json.dumps(result),
                })

            # Append tool exchange to working history
            working_history.append({
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["input"]}
                    for t in tool_calls
                ],
            })
            working_history.append({
                "role":    "user",
                "content": tool_results,
            })

        # Exceeded max rounds — force a text reply
        logger.warning(f"⚠️ Reached max tool rounds ({_MAX_TOOL_ROUNDS})")
        reply_text, _ = get_reply(
            system_prompt=system_prompt,
            history=working_history,
            tools=None,
        )
        return AssistantResponse(
            text=reply_text or "Sorry, I could not complete the request.",
            documents=collected_docs,
        )