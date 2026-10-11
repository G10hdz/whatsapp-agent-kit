"""Cerebras LLM API client - stdlib-only implementation.

Only provider='cerebras' supported. Model default: gpt-oss-120b.
Timeout: 20 seconds. Response validation included.
"""

import json
import os
import urllib.error
import urllib.request
from typing import Dict, Any, Optional


class CerebrasError(Exception):
    """Base exception for Cerebras client - never includes API key or content."""
    pass


class CerebrasTimeoutError(CerebrasError):
    """Request exceeded timeout."""
    pass


class CerebrasInvalidProviderError(CerebrasError):
    """Invalid provider specified."""
    pass


class CerebrasInvalidResponseError(CerebrasError):
    """Invalid or malformed response from API."""
    pass


def _get_api_key() -> str:
    """Read CEREBRAS_API_KEY from environment only when called.

    Returns:
        API key string.

    Raises:
        CerebrasError: If key not set.
    """
    key = os.environ.get("CEREBRAS_API_KEY")
    if not key:
        raise CerebrasError("CEREBRAS_API_KEY environment variable not set")
    return key


def call_cerebras(
    prompt: str,
    provider: str = "cerebras",
    model: str = "gpt-oss-120b",
    timeout: int = 20,
    response_format: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Call Cerebras API with stdlib urllib, bounded timeout, and validation.

    Args:
        prompt: Input prompt for the LLM.
        provider: API provider (only 'cerebras' supported).
        model: Model name (default: 'gpt-oss-120b').
        timeout: Request timeout in seconds (default: 20).
        response_format: Optional response format (e.g., {"type": "json_object"}).

    Returns:
        Response dict with keys: 'content', 'usage', 'model'.

    Raises:
        CerebrasInvalidProviderError: If provider not 'cerebras'.
        CerebrasTimeoutError: If request times out.
        CerebrasInvalidResponseError: If response malformed.
        CerebrasError: For other errors (no API key/content in message).
    """
    if provider != "cerebras":
        raise CerebrasInvalidProviderError(
            f"Only provider='cerebras' supported, got: {provider}"
        )

    api_key = _get_api_key()
    url = "https://api.cerebras.ai/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
    }

    if response_format:
        payload["response_format"] = response_format

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        with urllib.request.urlopen(req, timeout=timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))

            if "choices" not in response_data or not response_data["choices"]:
                raise CerebrasInvalidResponseError("No choices in API response")

            content = response_data["choices"][0]["message"]["content"]

            return {
                "content": content,
                "usage": response_data.get("usage", {}),
                "model": response_data.get("model", model),
            }

    except urllib.error.HTTPError as e:
        raise CerebrasError(f"HTTP error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise CerebrasTimeoutError("Request timed out")
        raise CerebrasError(f"URL error: {e.reason}")
    except json.JSONDecodeError as e:
        raise CerebrasInvalidResponseError("Failed to parse JSON response")
    except Exception as e:
        raise CerebrasError(f"Unexpected error during API call")