# =============================================================================
# LLM CLIENT - Stateless Claude API Wrapper
# =============================================================================
# Handles communication with the Claude API.
# Supports both plain responses and tool use responses.
#
# Input:  system prompt + message history + optional tool definitions
# Output: either a text reply or a list of tool call requests
#
# This module knows nothing about your house, state, or Telegram.
# =============================================================================

import logging
from anthropic import Anthropic

import config

logger = logging.getLogger(__name__)

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.CLAUDE_API_KEY)
    return _client


def get_reply(
    system_prompt: str,
    history: list[dict],
    tools: list[dict] | None = None,
) -> tuple[str | None, list[dict] | None]:
    """
    Send a conversation to Claude and return the response.

    Claude may respond in two ways:
      1. A plain text reply  → returns (text, None)
      2. A tool use request  → returns (None, [tool_calls])

    Args:
        system_prompt: System prompt string.
        history:       List of {"role": ..., "content": ...} dicts.
        tools:         Optional list of tool definition dicts.

    Returns:
        (reply_text, None)       if Claude replied with text
        (None, tool_calls)       if Claude wants to call tools
        (error_message, None)    on API error

    Tool call format returned:
        [{"id": "...", "name": "get_temperatures", "input": {}}]
    """
    try:
        client = _get_client()

        kwargs = dict(
            model=config.AI_MODEL,
            max_tokens=config.AI_MAX_TOKENS,
            system=system_prompt,
            messages=history,
        )

        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)

        # Check stop reason to determine response type
        if response.stop_reason == "tool_use":
            # Extract all tool use blocks
            tool_calls = [
                {
                    "id":    block.id,
                    "name":  block.name,
                    "input": block.input,
                }
                for block in response.content
                if block.type == "tool_use"
            ]
            return None, tool_calls

        # Plain text response
        text_blocks = [b.text for b in response.content if b.type == "text"]
        return "".join(text_blocks), None

    except Exception as e:
        logger.error(f"❌ LLM API error: {e}")
        return "Sorry, I couldn't reach the AI service right now. Please try again.", None