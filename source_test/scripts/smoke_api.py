"""Synthetic connectivity check; no competition data or reference answers are sent."""

import time
from dataclasses import replace
from pathlib import Path

from bizreason.config import Settings
from bizreason.llm import ModelError, TrammayClient
from bizreason.utils import atomic_json


def main() -> None:
    started = time.monotonic()
    settings = replace(Settings.from_env(), max_output_tokens=1024)
    client = TrammayClient(settings)
    report = {"model": settings.model, "payload": "synthetic_json_healthcheck"}
    try:
        output = client.generate_json(
            "Return exactly one JSON object, without markdown.",
            'Return {"status":"ok"}. This is a synthetic connectivity test.',
            timeout=60,
        )
        report.update({"success": output == {"status": "ok"}, "output": output})
    except ModelError as error:
        report.update({"success": False, "error": str(error)})
    finally:
        client.close()
    report["elapsed_seconds"] = time.monotonic() - started
    atomic_json(Path("artifacts/api_health.json"), report)
    print(report)


if __name__ == "__main__":
    main()
