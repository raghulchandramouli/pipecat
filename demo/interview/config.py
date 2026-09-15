"""Validated configuration for the conversational interview prototype.

This module describes application policy and provider choices. It does not create
provider clients or read environment variables, so deterministic and offline tests
can use the same configuration as a live session.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class InterviewModel(BaseModel):
    """Common safe validation settings for interview configuration."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class Difficulty(StrEnum):
    """Interview difficulty offered to a candidate."""

    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"


class ProviderMode(StrEnum):
    """Whether a provider is deliberately disabled or has live credentials."""

    OFFLINE = "offline"
    LIVE = "live"


class ProviderCredentials(InterviewModel):
    """Credentials supplied to a provider at application startup.

    Offline settings intentionally carry no credential requirement. ``SecretStr``
    redacts model representations, and the common configuration model hides input
    values in rendered validation errors. Structured validation errors retain their
    raw inputs and must not be logged.
    """

    mode: ProviderMode = ProviderMode.OFFLINE
    api_key: SecretStr | None = Field(default=None, repr=False)

    @classmethod
    def for_live(cls, api_key: str | SecretStr) -> ProviderCredentials:
        """Build live credentials while retaining Pydantic validation."""
        return cls(mode=ProviderMode.LIVE, api_key=api_key)

    @model_validator(mode="after")
    def _require_live_api_key(self) -> ProviderCredentials:
        if self.mode is ProviderMode.LIVE and (
            self.api_key is None or not self.api_key.get_secret_value().strip()
        ):
            raise ValueError("live provider credentials require a non-blank API key")
        return self


class RoleConfig(InterviewModel):
    """The role for which the candidate is practising."""

    title: str = Field(min_length=1, max_length=120)
    focus: str | None = Field(default=None, max_length=500)

    @field_validator("title", "focus")
    @classmethod
    def _strip_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class QuestionRubric(InterviewModel):
    """One competency against which an interview answer can be discussed."""

    competency: str = Field(min_length=1, max_length=160)
    guidance: str = Field(min_length=1, max_length=1_000)
    weight: float = Field(default=1.0, gt=0.0, le=10.0)

    @field_validator("competency", "guidance")
    @classmethod
    def _strip_rubric_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("rubric text must not be blank")
        return value


class InterviewDeadlines(InterviewModel):
    """Application-owned timing policy, expressed in seconds.

    The controller, not this configuration model, coordinates cancellation and
    rescheduling when the candidate starts speaking or requests thinking time.
    ``transcript_final_timeout`` is an unmeasured prototype starting value.
    """

    candidate_pause: float = Field(default=2.5, gt=0.0)
    transcript_final_timeout: float = Field(default=10.0, gt=0.0)
    incomplete_short_retry: float = Field(default=8.0, gt=0.0)
    incomplete_long_retry: float = Field(default=30.0, gt=0.0)
    user_turn_stop_timeout: float = Field(default=45.0, gt=0.0)
    user_idle_timeout: float = Field(default=15.0, gt=0.0)
    thinking_grace: float = Field(default=30.0, gt=0.0)

    @model_validator(mode="after")
    def _validate_deadline_order(self) -> InterviewDeadlines:
        if self.incomplete_short_retry >= self.incomplete_long_retry:
            raise ValueError("incomplete_short_retry must be less than incomplete_long_retry")
        if self.user_turn_stop_timeout <= max(
            self.candidate_pause, self.transcript_final_timeout, self.thinking_grace
        ):
            raise ValueError(
                "user_turn_stop_timeout must exceed candidate_pause, "
                "transcript_final_timeout, and thinking_grace"
            )
        return self


class SarvamSTTConfig(InterviewModel):
    """Pinned Sarvam realtime STT choices for the initial en-IN prototype."""

    credentials: ProviderCredentials = Field(default_factory=ProviderCredentials)
    model: str = "saaras:v3-realtime"
    endpointing: str = "manual"
    mode: Literal["transcribe", "codemix", "translit"] = "transcribe"
    language_code: str = "en-IN"
    sample_rate: int = Field(default=16_000, ge=8_000, le=48_000)

    @model_validator(mode="after")
    def _validate_design_contract(self) -> SarvamSTTConfig:
        if not self.model.strip():
            raise ValueError("Sarvam model must not be blank")
        if self.endpointing != "manual":
            raise ValueError("the interview prototype requires Sarvam manual endpointing")
        if self.language_code not in {"en-IN", "hi-IN", "ta-IN", "auto"}:
            raise ValueError("the demo supports en-IN, hi-IN, ta-IN, or auto STT")
        if self.sample_rate != 16_000:
            raise ValueError("the interview prototype requires a 16000 Hz Sarvam input")
        return self


class GeminiLLMConfig(InterviewModel):
    """Pinned Gemini text-reasoning choices for interview control."""

    credentials: ProviderCredentials = Field(default_factory=ProviderCredentials)
    model: str = "gemini-3.8-flash"
    thinking_level: str = "low"

    @model_validator(mode="after")
    def _validate_design_contract(self) -> GeminiLLMConfig:
        if self.model != "gemini-3.8-flash":
            raise ValueError("the interview prototype requires gemini-3.8-flash")
        if self.thinking_level != "low":
            raise ValueError("the interview prototype requires Gemini low thinking")
        return self


class RumikTTSConfig(InterviewModel):
    """Provisional Rumik serving selection pending checkpoint verification."""

    checkpoint: str = "rumik-ai/rumik-oss-1"
    sample_rate: int = Field(default=24_000, ge=8_000, le=48_000)
    inference_url: AnyHttpUrl | None = None
    provisional: bool = True

    @field_validator("checkpoint")
    @classmethod
    def _require_checkpoint(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Rumik checkpoint must not be blank")
        return value

    @field_validator("inference_url")
    @classmethod
    def _reject_url_credentials(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and (value.username is not None or value.password is not None):
            raise ValueError("Rumik inference_url must not contain credentials")
        return value


class SarvamTTSConfig(InterviewModel):
    """Hosted streaming speech for the English practice interview."""

    credentials: ProviderCredentials = Field(default_factory=ProviderCredentials)
    model: str = "bulbul:v3"
    speaker: str = Field(default="shubh", min_length=1)
    language_code: str = "en-IN"
    sample_rate: int = 24_000

    @model_validator(mode="after")
    def _validate_stream(self):
        if (
            self.model != "bulbul:v3"
            or self.language_code not in {"en-IN", "hi-IN", "ta-IN"}
            or self.sample_rate != 24_000
        ):
            raise ValueError("the demo requires bulbul:v3, en-IN, hi-IN, or ta-IN, and 24000 Hz")
        return self


class ProviderConfig(InterviewModel):
    """Provider settings retained separately from interview policy."""

    sarvam: SarvamSTTConfig = Field(default_factory=SarvamSTTConfig)
    gemini: GeminiLLMConfig = Field(default_factory=GeminiLLMConfig)
    tts: SarvamTTSConfig = Field(default_factory=SarvamTTSConfig)
    rumik: RumikTTSConfig = Field(default_factory=RumikTTSConfig)


class InterviewConfig(InterviewModel):
    """Complete configuration needed to construct an interview session."""

    role: RoleConfig
    difficulty: Difficulty
    duration_minutes: int = Field(ge=5, le=180)
    question_rubric: tuple[QuestionRubric, ...] = Field(min_length=1)
    deadlines: InterviewDeadlines = Field(default_factory=InterviewDeadlines)
    providers: ProviderConfig = Field(default_factory=ProviderConfig)
