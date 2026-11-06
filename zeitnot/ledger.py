"""Running total of API spend, persisted to disk so the cap survives restarts."""

import json
import os
from pathlib import Path


class SpendLedger:
    def __init__(self, path: Path | None):
        self.path = path
        self.spent_usd = 0.0
        if path is not None and path.exists():
            self.spent_usd = float(json.loads(path.read_text())["spent_usd"])

    def add(self, usd: float) -> None:
        if usd == 0:
            return
        self.spent_usd += usd
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"spent_usd": self.spent_usd}))
            os.replace(tmp, self.path)
