"""Jev (TypeSafe System One) pre-filter: cheap classification before the LLM.

Jev is a decision model, not a text generator: it returns a typed label +
probabilities, never prose. We use a single `choice` question with the same
three outcomes the LLM uses (`new_application` | `status_update` |
`irrelevant`), then only call OpenAI for extraction on likely job mail.

No new dependencies: plain stdlib `urllib` POST to
`POST {base_url}` with `Authorization: Bearer <key>`.
See https://docs.typesafe.ai/introduction/quickstart
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

from .models import EmailMessage, JobAction

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
MAX_STATE_CHARS = 6000

_TRIAGE_INSTRUCTIONS = (
    "Classify this email by its relationship to the recipient's own job search. "
    "A job-application email is a confirmation of a submitted application, a recruiter "
    "or ATS message about an interview, offer, rejection, assessment, or shortlisting, "
    "or any hiring-platform notification about the user's candidacy. "
    "Newsletters, marketing, social notifications, and other people's job posts are irrelevant."
)

_CRITERIA = {
    JobAction.NEW_APPLICATION.value: (
        "Confirms a brand new job application was just submitted "
        "(e.g. 'Thanks for applying', 'Application received')."
    ),
    JobAction.STATUS_UPDATE.value: (
        "An update on an existing application: shortlist, interview invite, "
        "assessment, offer, rejection, or withdrawal."
    ),
    JobAction.IRRELEVANT.value: (
        "Not about the recipient's own job application: newsletters, marketing, "
        "social notifications, spam, or unrelated mail."
    ),
}


@dataclass(frozen=True)
class JevDecision:
    action: JobAction
    probability: float
    confidence: float
    is_job_related: bool


class JevClassifier:
    """Thin wrapper over the TypeSafe System One API (choice primitive)."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_s: int = 30,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def classify(self, message: EmailMessage) -> JevDecision | None:
        """Return a triage decision, or None on transport/shape errors (caller falls back to LLM)."""
        state = _build_state(message)
        payload = {
            "state": state,
            "model": self._model,
            "questions": {
                "triage": {
                    "type": "choice",
                    "instructions": _TRIAGE_INSTRUCTIONS,
                    "criteria": _CRITERIA,
                }
            },
        }
        try:
            raw = _post_json(self._endpoint, payload, self._api_key, self._timeout_s)
        except Exception as exc:
            log.warning("Jev call failed for message %s: %s", message.message_id, exc)
            return None

        try:
            answer = raw["answers"]["triage"]
            label = answer["choice"]
            probs = answer.get("probabilities", {})
            confidence = float(answer.get("confidence", probs.get(label, 0.0) or 0.0))
            probability = float(probs.get(label, confidence))
            action = JobAction(label)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Jev returned unexpected shape: %s (%s)", str(raw)[:300], exc)
            return None

        return JevDecision(
            action=action,
            probability=probability,
            confidence=confidence,
            is_job_related=action != JobAction.IRRELEVANT,
        )


def _build_state(message: EmailMessage) -> str:
    body = (message.body_text or "(no plain-text body)")[:MAX_STATE_CHARS]
    return (
        f"From: {message.sender}\nSubject: {message.subject}\nBody:\n{body}"
    )


def _post_json(endpoint: str, payload: dict, api_key: str, timeout_s: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))
