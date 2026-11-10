"""Write the OpenAPI (Swagger) specification to docs/openapi.json.

    python -m scripts.export_openapi

The file can be imported into Postman (Import → File) to get a ready-made
collection of every endpoint, or opened in any OpenAPI viewer.
"""

import json
from pathlib import Path

from app.main import app

OUT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def main() -> None:
    OUT.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}")


if __name__ == "__main__":
    main()
