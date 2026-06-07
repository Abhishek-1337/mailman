"""Pydantic models describing job application records and LLM outputs."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobAction(str, Enum):
    NEW_APPLICATION = "new_application"
    STATUS_UPDATE = "status_update"
    IRRELEVANT = "irrelevant"


class JobStatus(str, Enum):
    APPLIED = "Applied"
    SHORTLISTED = "Shortlisted"
    INTERVIEW = "Interview"
    ASSESSMENT = "Assessment"
    OFFER = "Offer"
    REJECTED = "Rejected"
    WITHDRAWN = "Withdrawn"
    UNKNOWN = "Unknown"


class LLMResult(BaseModel):
    is_job_related: bool
    action: JobAction
    company: Optional[str] = None
    role: Optional[str] = None
    platform: Optional[str] = None
    status: JobStatus = JobStatus.UNKNOWN
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class JobApplication(BaseModel):
    """One row in the Google Sheet."""
    date_applied: str
    company: str
    role: str
    platform: str
    status: JobStatus
    last_update: str
    notes: str
    email_subject: str


class EmailMessage(BaseModel):
    """A normalized Gmail message we feed to the LLM."""
    message_id: str
    thread_id: str
    sender: str
    subject: str
    date: datetime
    body_text: str
