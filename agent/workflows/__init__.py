"""Reusable multi-agent workflow patterns (SDK-agnostic; inject a runner to test offline)."""

from .base import AgentRunner, StepRecord, Verdict, VerdictLike, WorkflowResult, default_runner
from .parallel import BestOfN
from .refine import RefineLoop, default_revision_prompt
from .sequential import SequentialWorkflow, Step

__all__ = [
    "AgentRunner",
    "BestOfN",
    "RefineLoop",
    "SequentialWorkflow",
    "Step",
    "StepRecord",
    "Verdict",
    "VerdictLike",
    "WorkflowResult",
    "default_revision_prompt",
    "default_runner",
]
