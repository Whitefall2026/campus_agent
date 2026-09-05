import json
from pathlib import Path

from model import UserState


class UserStateMemory:

    def __init__(self, path: str = "user_state_history.json"):
        self.path = Path(path)

    def save(self, state: UserState) -> None:
        history = self.load()

        history.append(state.to_dict())

        self.path.write_text(
            json.dumps(
                history,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []

        return json.loads(
            self.path.read_text(encoding="utf-8")
        )

    def latest(self) -> dict | None:
        history = self.load()

        if not history:
            return None

        return history[-1]
