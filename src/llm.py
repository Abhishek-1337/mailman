"""OpenAI-backed extractor: classify each email and pull job-application details."""
from __future__ import annotations

import json
import logging
from typing import Optional

from openai import OpenAI
from pydantic import ValidationError

from .models import EmailMessage, JobAction, JobStatus, LLMResult

log = logging.getLogger(__name__)

_MAX_BODY_CHARS = 6000

_SYSTEM_PROMPT = """\
You extract structured data from emails about a user's job application pipeline.

For each email, decide:
1. is_job_related: true if the email is about the user's own job application, interview,
   offer, rejection, assessment, or any recruiter/ATS communication. False for
   newsletters, marketing, social-media notifications unrelated to job hunting, etc.
2. action:
   - "new_application"  -> confirms a brand new application was submitted
                          (e.g. "Thanks for applying to X", "Application received").
   - "status_update"    -> an update on an existing application (shortlist, interview
                          invite, assessment, offer, rejection, withdrawal, etc.).
   - "irrelevant"       -> not a job-application email.
3. Fields to extract when job-related:
   - company: the employer / hiring company (not the job board).
   - role:    the job title the user applied to.
   - platform: the job board or ATS that sent the email (e.g. LinkedIn, Indeed,
              Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Wellfound,
              company-direct, or "Unknown"). Infer from sender domain and content.
   - status: one of Applied, Shortlisted, Interview, Assessment, Offer, Rejected,
            Withdrawn, Unknown.
   - confidence: 0.0-1.0, how confident you are in the extraction.
   - reasoning: one short sentence explaining the call.

Return STRICT JSON matching the schema. No prose.
"""


_USER_TEMPLATE = """\
From: {sender}
Subject: {subject}
Date: {date}

Body (may be truncated):
\"\"\"
{body}
\"\"\"
"""


class JobExtractor:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def extract(self, message: EmailMessage) -> Optional[LLMResult]:
        body = (message.body_text or "")[:_MAX_BODY_CHARS]
        if not body.strip():
            body = "(no plain-text body)"

        user_prompt = _USER_TEMPLATE.format(
            sender=message.sender,
            subject=message.subject,
            date=message.date.isoformat(),
            body=body,
        )

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                temperature=0,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=_response_format(),
            )
        except Exception as exc:
            log.exception("OpenAI call failed for message %s: %s", message.message_id, exc)
            return None

        raw = response.choices[0].message.content or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Model returned non-JSON: %r", raw[:300])
            return None

        try:
            result = LLMResult.model_validate(data)
        except ValidationError as exc:
            log.warning("LLM output failed validation: %s; payload=%s", exc, data)
            return None

        # Final sanity check
        if not result.is_job_related or result.action == JobAction.IRRELEVANT:
            return result
        if not result.company or not result.role:
            log.info("LLM marked job-related but missing company/role: %s", result)
            return None
        return result


def _response_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "job_email_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "is_job_related": {"type": "boolean"},
                    "action": {
                        "type": "string",
                        "enum": [a.value for a in JobAction],
                    },
                    "company": {"type": ["string", "null"]},
                    "role": {"type": ["string", "null"]},
                    "platform": {"type": ["string", "null"]},
                    "status": {
                        "type": "string",
                        "enum": [s.value for s in JobStatus],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "is_job_related",
                    "action",
                    "company",
                    "role",
                    "platform",
                    "status",
                    "confidence",
                    "reasoning",
                ],
            },
        },
    }
