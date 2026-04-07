import re
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class AssistResult:
    intent: str
    suggested_reply: str


class AgentAssistEngine:
    def __init__(self, logger):
        self._logger = logger

    def analyze(self, text: str) -> Optional[AssistResult]:
        normalized = (text or "").strip()
        if not normalized:
            return None

        t = normalized.lower()

        rules: Tuple[Tuple[str, str, str], ...] = (
            ("cancellation", r"\b(cancel|terminate|close)\b.*\b(subscription|plan|account)\b|\bcancel\b", "I can help with that—may I know the reason you’d like to cancel?"),
            ("refund", r"\b(refund|chargeback)\b", "I can help—could you share the order/transaction ID and what went wrong?"),
            ("billing", r"\b(bill|billing|charged|charge|invoice|payment)\b", "I can check your billing—what’s the email/account ID and the date of the charge?"),
            ("technical_support", r"\b(not working|error|issue|problem|unable to|can'?t)\b", "I’m here to help—what error message do you see and what steps have you tried so far?"),
            ("complaint", r"\b(complain|unhappy|frustrated|angry|disappointed)\b", "I’m sorry about that—let me help. Could you tell me what happened so I can resolve it quickly?"),
            ("speak_to_agent", r"\b(supervisor|manager|human|agent)\b", "I can connect you—before I do, can you briefly tell me what you need help with?"),
        )

        for intent, pattern, reply in rules:
            if re.search(pattern, t):
                return AssistResult(intent=intent, suggested_reply=reply)

        return None

