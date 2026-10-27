"""Package the audited public source tree, never the deployment directory."""

import hashlib
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from public_release import export_source


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    destination = root / "dist"
    destination.mkdir(exist_ok=True)
    target = destination / f"tidetalk-v{version}.zip"
    with tempfile.TemporaryDirectory(prefix="public-", dir=destination) as directory:
        staging = Path(directory)
        source = staging / "source"
        export_source(root, source)
        archive_path = staging / target.name
        with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    archive.write(path, str(Path("tidetalk") / path.relative_to(source)))
        with ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("Archive validation failed")
        archive_path.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    print(f"Created {target.name} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
