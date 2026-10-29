"""Validated public inputs. Credentials and target URLs never enter this schema."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

Backend = Literal["openai", "dashscope", "openrouter"]
Reasoning = Literal["none", "low", "medium", "high", "xhigh", "max"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Settings(StrictModel):
    backend: Backend
    model: str | None = Field(default=None, max_length=200)
    use_tools: bool = True
    reasoning: Reasoning = "low"
    verbosity: Literal["low", "medium", "high"] | None = None
    max_output_tokens: int | None = Field(default=None, gt=0, le=100000)

    @field_validator("model")
    @classmethod
    def clean_model(cls, value):
        return value.strip() or None if value is not None else None


class SessionInput(StrictModel):
    config: Settings


class TurnInput(StrictModel):
    input: str = Field(min_length=1, max_length=20000)

    @field_validator("input")
    @classmethod
    def clean_input(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请输入任务内容")
        return value


class PreviewInput(TurnInput):
    config: Settings | None = None
    session_id: str | None = None

    @model_validator(mode="after")
    def exclusive_source(self):
        if (self.config is None) == (self.session_id is None):
            raise ValueError("config 与 session_id 必须且只能提供一个")
        return self


class ExperimentInput(StrictModel):
    backends: list[Backend] = Field(min_length=1, max_length=3)
    reasoning: Reasoning = "low"

    @field_validator("backends")
    @classmethod
    def unique_backends(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("后端不能重复")
        return value
