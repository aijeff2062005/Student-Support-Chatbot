import json
from pathlib import Path
from typing import Any


def append_benchmark_record(record: dict[str, Any], *, file_path: str = "benchmarks/flow_benchmark.json") -> None:
    """Persist benchmark records as an indented JSON array."""
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    records = []
    if target.exists():
        content = target.read_text(encoding="utf-8").strip()
        if content:
            try:
                records = json.loads(content)
            except json.JSONDecodeError:
                records = []

    records.append(record)
    target.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
