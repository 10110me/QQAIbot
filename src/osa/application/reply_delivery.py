"""Natural sentence boundaries, persisted as individual outbox messages."""
import re


def split_reply(text: str, max_parts: int = 4) -> list[str]:
    if len(text) < 1200 or '```' in text or re.search(r'(?m)^\s*(?:[-*#]|\d+[.)])\s', text):
        return [text]
    protected = {i for m in re.finditer(r'https?://\S+|`[^`]+`|\d+\.\d+|[（(][^()（）\n]{0,100}[)）]|“[^”]*”|「[^」]*」', text)
                 for i in range(m.start(), m.end())}
    parts, start = [], 0
    while len(text) - start > 1200 and len(parts) < max_parts - 1:
        minimum, preferred, maximum = start + 500, start + 900, start + 1200
        boundaries = [index + 1 for index in range(minimum, min(maximum, len(text)))
                      if index not in protected and text[index] in '。！？!?；;\n，,']
        before = [index for index in boundaries if index <= preferred]
        end = before[-1] if before else (boundaries[0] if boundaries else maximum)
        parts.append(text[start:end])
        start = end
    if text[start:]:
        parts.append(text[start:])
    return parts or [text]


def delivery_parts(result):
    return split_reply(result.text) if result.provider not in {'workflow', 'fake'} and result.model not in {'command', 'capability-response'} else [result.text]
