"""Keyword ranking shared by persistent and in-memory memory stores."""

import re


_WORDS = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


def search_terms(query: str) -> list[str]:
    terms = []
    for word in _WORDS.findall(query.casefold()):
        if "\u4e00" <= word[0] <= "\u9fff" and len(word) > 2:
            terms.extend(word[index:index + 2] for index in range(len(word) - 1))
        else:
            terms.append(word)
    return list(dict.fromkeys(terms))[:24]


def rank_memories(memories, query: str, limit: int):
    terms = search_terms(query)
    if not terms:
        return list(memories)[:limit]
    needle = query.strip().casefold()

    def score(memory):
        content = memory.content.casefold()
        return (10 if needle and needle in content else 0) + sum(
            (3 if len(term) > 2 else 1) for term in terms if term in content
        )

    return sorted((memory for memory in memories if score(memory) > 0),
                  key=lambda memory: -score(memory))[:limit]
