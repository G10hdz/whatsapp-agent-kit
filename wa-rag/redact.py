"""Redaction utilities for WhatsApp content.

Provides safe redaction of JIDs, phone numbers, and other sensitive identifiers
while preserving message_id for citations and maintaining traceability.
"""

import re
from typing import Dict, Optional, Set


# Safe regex patterns for unmapped JIDs and phone-like values
_JID_PATTERN = re.compile(
    r'\b\d{1,3}[A-Za-z0-9._%+-]+@s\.whatsapp\.net\b'
)
_PHONE_PATTERN = re.compile(
    r'\b\d{10,15}\b'  # 10-15 digit numbers (typical phone length)
)
_MESSAGE_ID_PATTERN = re.compile(
    r'\b[A-Fa-f0-9]{24}\b'  # 24-char hex IDs
)


def redact_text(
    text: str,
    caller_alias_map: Optional[Dict[str, str]] = None,
    preserve_message_ids: bool = True,
) -> str:
    """Redact sensitive identifiers from text.

    Args:
        text: Input text to redact.
        caller_alias_map: Optional mapping from JID/phone to display alias.
        preserve_message_ids: If True, preserve message_id format for citations.

    Returns:
        Redacted text with identifiers replaced.

    Privacy: never includes original JIDs, phones, or API keys in output.
    """
    if caller_alias_map is None:
        caller_alias_map = {}

    result = text

    # First, apply mapped replacements (higher priority)
    for identifier, alias in caller_alias_map.items():
        # Escape special regex chars in identifier
        escaped = re.escape(identifier)
        result = re.sub(escaped, alias, result)

    # Then, redact unmapped JIDs
    def _redact_jid(match: re.Match) -> str:
        jid = match.group(0)
        if jid in caller_alias_map:
            return caller_alias_map[jid]
        # Redact with generic placeholder
        if '@' in jid:
            local = jid.split('@')[0]
            if local.isdigit():
                # Phone-based JID: show last 4 digits
                return f"[REDACTED_PHONE:{local[-4:]}]"
            else:
                # Other JID: show first 2 chars
                return f"[REDACTED_JID:{local[:2]}...]"
        return "[REDACTED_JID]"

    result = _JID_PATTERN.sub(_redact_jid, result)

    # Redact unmapped phone numbers (10-15 digits)
    def _redact_phone(match: re.Match) -> str:
        phone = match.group(0)
        if phone in caller_alias_map:
            return caller_alias_map[phone]
        # Show last 4 digits
        return f"[REDACTED_PHONE:{phone[-4:]}]"

    result = _PHONE_PATTERN.sub(_redact_phone, result)

    # Preserve message_ids if requested (needed for citations)
    if not preserve_message_ids:
        def _redact_message_id(match: re.Match) -> str:
            mid = match.group(0)
            return f"[MSG_ID:{mid[:8]}...]"

        result = _MESSAGE_ID_PATTERN.sub(_redact_message_id, result)

    return result


def redact_windows(
    windows: list,
    caller_alias_map: Optional[Dict[str, str]] = None,
    preserve_message_ids: bool = True,
) -> list:
    """Redact sensitive content from message windows.

    Args:
        windows: List of message window dicts.
        caller_alias_map: Optional mapping from JID/phone to display alias.
        preserve_message_ids: If True, preserve message_id format for citations.

    Returns:
        Redacted windows with sensitive identifiers replaced.
    """
    redacted = []
    for window in windows:
        redacted_window = {
            "score": window.get("score"),
            "chat_jid": redact_text(
                window.get("chat_jid", ""),
                caller_alias_map,
                preserve_message_ids,
            ),
            "start_timestamp": window.get("start_timestamp"),
            "end_timestamp": window.get("end_timestamp"),
            "anchor_doc_id": window.get("anchor_doc_id"),
            "messages": [],
        }

        for msg in window.get("messages", []):
            redacted_msg = {
                "message_id": msg.get("message_id") if preserve_message_ids else
                redact_text(msg.get("message_id", ""), caller_alias_map, False),
                "sender": redact_text(
                    msg.get("sender", ""),
                    caller_alias_map,
                    preserve_message_ids,
                ),
                "timestamp": msg.get("timestamp"),
                "content": redact_text(
                    msg.get("content", ""),
                    caller_alias_map,
                    preserve_message_ids,
                ),
            }
            redacted_window["messages"].append(redacted_msg)

        redacted.append(redacted_window)

    return redacted


def validate_citations(
    response_text: str,
    retrieved_ids: Set[str],
) -> bool:
    """Validate that all citations in response are valid.

    Args:
        response_text: LLM response text potentially containing citations.
        retrieved_ids: Set of valid message IDs that were retrieved.

    Returns:
        True if citations are valid, False otherwise.

    Validation rules:
        - At least one bracketed ID must be present (e.g., [ABC123...])
        - Every cited ID must be in the supplied retrieved_ids set
        - Errors are generic (no details about which IDs failed)
    """
    if not retrieved_ids:
        return False

    # Find all bracketed IDs (e.g., [ABC123DEF456...])
    bracketed_ids = set(re.findall(r'\[([A-Fa-f0-9]{24})\]', response_text))

    # Must have at least one citation
    if not bracketed_ids:
        return False

    # All cited IDs must be in retrieved_ids
    return bracketed_ids.issubset(retrieved_ids)


def extract_message_ids(text: str) -> Set[str]:
    """Extract 24-char hex message IDs from text.

    Args:
        text: Input text to search.

    Returns:
        Set of message IDs found.
    """
    return set(_MESSAGE_ID_PATTERN.findall(text))