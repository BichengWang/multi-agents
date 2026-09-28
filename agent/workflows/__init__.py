"""Reusable multi-agent workflow patterns (SDK-agnostic; inject a runner to test offline)."""

from .base import AgentRunner, StepRecord, Verdict, VerdictLike, WorkflowResult, default_runner
from .parallel import BestOfN
from .refine import RefineLoop, default_revision_prompt
from .router import Route, RouteDecision, RouteHandler, Router, default_classifier_prompt
from .sequential import SequentialWorkflow, Step

__all__ = [
    "AgentRunner",
    "BestOfN",
    "RefineLoop",
    "Route",
    "RouteDecision",
    "RouteHandler",
    "Router",
    "SequentialWorkflow",
    "Step",
    "StepRecord",
    "Verdict",
    "VerdictLike",
    "WorkflowResult",
    "default_classifier_prompt",
    "default_revision_prompt",
    "default_runner",
]
