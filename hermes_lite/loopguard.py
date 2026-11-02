"""Thinking-loop guard for small models (Qwen3.6 can think in circles for thousands of tokens).

Layers, all switched by `loop_guard.enabled` (false = exactly the behaviour before the guard):
  1. sampling: a mild presence penalty and a slightly higher temperature (loops grow at low temps)
  2. a hard thinking budget per reply, enforced by llama-server (reasoning_budget_tokens)
  3. LoopDetector below, run on the stream: when the thinking repeats itself, Hermes Lite ends the
     thinking block on the spot (llama-server's /v1/chat/completions/control, recent builds) or
     stops the reply and resends it with a "you were going in circles" note and a smaller budget
  4. one line in the system prompt"""

from __future__ import annotations

import re
import zlib

from .config import Config

DEFAULTS = {
    "enabled": True,
    "thinking_budget": 8192,          # tokens of thinking per reply (0 = no cap)
    "retry_budget": 2048,             # budget for the resend after a loop was cut
    "budget_message": None,           # None = prompts/thinking_budget.md
    "detect": True,                   # watch the stream for repetition
    "max_retries": 2,                 # resends per message after loops
    "presence_penalty": 0.5,          # Qwen allows 0-2 against endless repetition
    "temperature": 0.7,               # between Qwen's coding (0.6) and general (1.0) values
    "dry_multiplier": 0.0,            # DRY sampler (0 = off; code repeats legitimately)
}

def settings(cfg: Config) -> dict:
    """The guard's settings, or {} when it is switched off."""
    user = cfg.get("loop_guard") or {}
    s = {**DEFAULTS, **(user if isinstance(user, dict) else {})}
    return s if s.get("enabled") else {}


def _code_like(line: str) -> bool:
    stripped = line.strip()
    symbols = sum(stripped.count(c) for c in "(){}[]=;<>\"'`_")
    return (line[:1] in " \t" or symbols / max(1, len(stripped)) > 0.06
            or stripped.endswith((":", "{", "}", ")", ";", ",")) or stripped.startswith(("def ", "class ", "return ",
                                                                                        "raise ", "import ", "from ", "#", "//")))


class LoopDetector:
    """Spots thinking that goes in circles. Only the THINKING is ever watched — never the answer and
    never a tool call — and only its prose: code, file contents and tool-call markup are set aside
    first, because code repeats itself legitimately (the same `raise HTTPException(...)` in every
    endpoint). A loop must also still be there on the next check, ~400 characters later, so a model
    that restates something once or twice is never interrupted. feed() returns an excerpt of the
    repeated prose when it is sure, else None."""

    def __init__(self, min_chars: int = 3000, confirm: int = 2):
        self.text = ""
        self.min_chars = min_chars
        self.confirm = confirm
        self.checked = 0
        self.strikes = 0

    def feed(self, chunk: str) -> str | None:
        self.text += chunk
        if len(self.text) < self.min_chars or len(self.text) - self.checked < 400:
            return None
        self.checked = len(self.text)
        hit = self.check(self.text[-10000:])
        self.strikes = self.strikes + 1 if hit else 0
        return hit if self.strikes >= self.confirm else None

    @staticmethod
    def prose(text: str) -> str:
        """The text with code blocks, tool calls and code-like lines removed."""
        text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
        text = re.sub(r"<tool_call>.*?(</tool_call>|$)|<function=.*?(</function>|$)", " ", text, flags=re.S)
        return "\n".join(line for line in text.splitlines() if line.strip() and not _code_like(line))

    @classmethod
    def check(cls, tail: str) -> str | None:
        text = cls.prose(tail)
        flat = re.sub(r"\s+", " ", text)
        if len(flat) < 1500:
            return None  # mostly code: nothing to judge
        # 1. the last stretch of prose already appeared at least twice before, verbatim
        block = flat[-250:]
        if len(block) >= 200 and flat.count(block) >= 3:
            return block.strip()[:160]
        # 2. the same long sentence keeps coming back (5 times)
        seen: dict[str, int] = {}
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            sentence = sentence.strip()
            if len(sentence) < 50:
                continue
            key = re.sub(r"\W+", " ", sentence.lower()).strip()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] >= 5:
                return sentence[:160]
        # 3. the recent prose compresses far better than normal writing: it is mostly repetition
        recent = flat[-4000:]
        if len(recent) >= 3000 and len(zlib.compress(recent.encode(), 6)) / len(recent.encode()) < 0.12:
            return recent[-160:].strip()
        return None
