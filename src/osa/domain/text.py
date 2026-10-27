def chunks(pages: list[dict], size: int = 900, overlap: int = 120) -> list[dict]:
    if size <= overlap or overlap < 0:
        raise ValueError("Invalid chunk parameters")
    result = []
    for page in pages:
        for start in range(0, len(page["text"]), size-overlap):
            value = page["text"][start:start+size].strip()
            if value:
                result.append({"page": page["page"], "text": value, "start": start})
    return result[:5000]
