from collections import defaultdict
from typing import List, Dict

_history: Dict[str, List[dict]] = defaultdict(list)

MAX_MESSAGES = 20


def get_history(phone: str) -> List[dict]:
    return _history[phone]


def add_message(phone: str, role: str, content: str) -> None:
    _history[phone].append({"role": role, "content": content})
    if len(_history[phone]) > MAX_MESSAGES:
        _history[phone] = _history[phone][-MAX_MESSAGES:]


def clear_history(phone: str) -> None:
    _history[phone] = []
