from pydantic import BaseModel, Field


class Evaluation(BaseModel):
    """Structured verdict an evaluator agent returns, so workflows can branch on it."""

    score: int = Field(ge=1, le=10)
    """Overall quality from 1 (unusable) to 10 (excellent)."""

    passed: bool
    """Whether the output is good enough to ship as is."""

    strengths: list[str]
    """What the output does well."""

    issues: list[str]
    """Concrete problems that should be fixed, most important first."""

    suggestions: str
    """Actionable revision instructions for the generator."""
