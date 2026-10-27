import re
import unicodedata
from difflib import SequenceMatcher


def normalized_memory(content: str) -> str:
    value = unicodedata.normalize('NFKC', content).casefold()
    return ''.join(character for character in value if character.isalnum())


def memory_keywords(content: str) -> set[str]:
    value = unicodedata.normalize('NFKC', content).casefold()
    words = set(re.findall(r'[a-z0-9_]{2,}', value))
    for phrase in re.findall(r'[\u4e00-\u9fff]+', value):
        words.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return words


def duplicate_memory(content: str, previous: str) -> bool:
    current = normalized_memory(content)
    older = normalized_memory(previous)
    if not current or not older:
        return False
    if current == older:
        return True
    if not memory_keywords(content) & memory_keywords(previous):
        return False
    current_numbers = re.findall(r'\d+', current)
    older_numbers = re.findall(r'\d+', older)
    if current_numbers != older_numbers:
        return False
    matcher = SequenceMatcher(None, current, older)
    if matcher.ratio() < 0.9:
        return False
    for operation, current_start, current_end, older_start, older_end in matcher.get_opcodes():
        if operation == 'equal':
            continue
        changed = current[current_start:current_end] + older[older_start:older_end]
        if len(changed) > 2 or any(character not in '的了很' for character in changed):
            return False
    return True
