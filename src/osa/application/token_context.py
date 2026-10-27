"""Conservative, tokenizer-independent budgeting for chat model requests."""

import json
import math
import re


_PIECES = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|[^\s]", re.UNICODE)


class ContextBudgetExceeded(ValueError):
    pass


def estimate_text_tokens(value: str) -> int:
    total = 0
    for match in _PIECES.finditer(value):
        part = match.group()
        total += math.ceil(len(part) / 3) if part.isascii() and (part.isalnum() or "_" in part) else 1
    return total


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 3
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    total += 2048
                elif isinstance(part, dict) and part.get('type') == 'video_url':
                    total += 16384
                else:
                    total += estimate_text_tokens(json.dumps(part, ensure_ascii=False))
        else:
            total += estimate_text_tokens(str(content))
        total += 6
    return total


def input_token_budget(context_window: int, output_limit: int) -> int:
    return math.floor(context_window * 0.85) - output_limit - 256


def _tool_result(message: dict) -> bool:
    if message.get("role") != "user" or not isinstance(message.get("content"), str):
        return False
    try:
        content = json.loads(message["content"])
    except (TypeError, ValueError):
        return False
    return isinstance(content, dict) and set(content) == {"tool_result", "tool_call_id"}


def _current_request(message: dict) -> bool:
    content = message.get("content")
    return (message.get("role") == "user" and isinstance(content, str)
            and (content.startswith("<untrusted_message_context ")
                 or content.startswith("<untrusted_quoted_message ")
                 or content.startswith("<untrusted_message_batch ")
                 or "子任务事件" in content))


def fit_messages(messages: list[dict], budget: int) -> tuple[list[dict], int]:
    """Drop oldest optional turns, keeping system, current request and latest tool receipt."""
    if budget < 1:
        raise ContextBudgetExceeded("Model context window leaves no input tokens")
    if estimate_messages_tokens(messages) <= budget:
        return list(messages), 0

    tool_indices = [index for index, message in enumerate(messages) if _tool_result(message)]
    first_tool = tool_indices[0] if tool_indices else len(messages)
    original_requests = [index for index, message in enumerate(messages[:first_tool])
                         if message.get("role") == "user"]
    pinned = {index for index, message in enumerate(messages) if message.get("role") == "system"}
    current_requests = [index for index in original_requests if _current_request(messages[index])]
    if current_requests:
        pinned.update(current_requests)
    elif original_requests:
        pinned.add(original_requests[-1])
    if tool_indices:
        pinned.add(tool_indices[-1])
        if tool_indices[-1] and messages[tool_indices[-1] - 1].get("role") == "assistant":
            pinned.add(tool_indices[-1] - 1)
    elif messages:
        pinned.add(len(messages) - 1)

    groups = []
    index = 0
    while index < len(messages):
        if index + 1 < len(messages) and messages[index].get("role") == "assistant" and _tool_result(messages[index + 1]):
            groups.append((index, index + 1))
            index += 2
        else:
            groups.append((index,))
            index += 1
    retained = set(range(len(messages)))
    for group in groups:
        if any(index in pinned for index in group):
            continue
        retained.difference_update(group)
        selected = [message for index, message in enumerate(messages) if index in retained]
        if estimate_messages_tokens(selected) <= budget:
            return selected, len(messages) - len(selected)
    raise ContextBudgetExceeded("System, current request and latest tool receipt exceed model input budget")
