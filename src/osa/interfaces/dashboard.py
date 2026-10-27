from pathlib import Path

def page() -> str:
    return (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
