import asyncio

import pytest

from agent.workflows import Route, RouteDecision, Router, SequentialWorkflow, Step

from .fakes import FakeAgent, fake_runner


def run(coro):
    return asyncio.run(coro)


def classifier(route, confidence=0.9):
    return FakeAgent("Classifier", lambda _: RouteDecision(route=route, confidence=confidence, reason="r"))


def handler(tag, calls):
    async def handle(query):
        calls.append((tag, query))
        return f"{tag}:{query}"

    return handle


def make_router(decision_route, confidence=0.9, **kw):
    calls = []
    routes = [Route("a", "does A", handler("a", calls)), Route("b", "does B", handler("b", calls))]
    clf = classifier(decision_route, confidence)
    return Router(clf, routes, runner=fake_runner, **kw), clf, calls


def test_routes_to_classified_handler_and_records_decision():
    router, clf, calls = make_router("b")
    result = run(router.run("hello"))
    assert result.final_output == "b:hello"
    assert calls == [("b", "hello")]
    assert result.meta == {
        "route": "b", "classified_as": "b", "confidence": 0.9, "reason": "r", "fallback_used": False,
    }
    # The classifier sees every route's description plus the request.
    assert "- a: does A" in clf.calls[0] and "- b: does B" in clf.calls[0] and "hello" in clf.calls[0]
    assert [s.name for s in result.steps] == ["classify"]


@pytest.mark.parametrize("route,confidence", [("nope", 0.9), ("b", 0.2)])
def test_unknown_or_unsure_uses_fallback(route, confidence):
    router, _, calls = make_router(route, confidence, fallback="a", min_confidence=0.5)
    result = run(router.run("q"))
    assert calls == [("a", "q")]
    assert result.meta["fallback_used"] is True and result.meta["classified_as"] == route


def test_unknown_route_without_fallback_raises():
    router, _, calls = make_router("nope")
    with pytest.raises(ValueError, match="expected one of"):
        run(router.run("q"))
    assert calls == []


def test_force_skips_classifier():
    router, clf, calls = make_router("b")
    result = run(router.run("q", force="a"))
    assert clf.calls == [] and calls == [("a", "q")]
    assert result.meta == {"route": "a", "forced": True}
    with pytest.raises(ValueError):
        run(router.run("q", force="zzz"))


def test_merges_workflow_result_from_handler():
    upper = FakeAgent("Upper", str.upper)
    wf = SequentialWorkflow([Step("upper", upper)], runner=fake_runner)
    router = Router(classifier("up"), [Route("up", "uppercase", wf.run)], runner=fake_runner)
    result = run(router.run("hi"))
    assert result.final_output == "HI"
    assert [s.name for s in result.steps] == ["classify", "upper"]
    assert result.meta["route"] == "up" and "route_meta" in result.meta


def test_rejects_unstructured_classifier():
    clf = FakeAgent("Plain", lambda _: "a")
    router = Router(clf, [Route("a", "A", handler("a", []))], runner=fake_runner)
    with pytest.raises(TypeError, match="route/confidence/reason"):
        run(router.run("q"))


def test_validates_configuration():
    noop = handler("x", [])
    with pytest.raises(ValueError):
        Router(classifier("a"), [])
    with pytest.raises(ValueError, match="Duplicate"):
        Router(classifier("a"), [Route("a", "", noop), Route("a", "", noop)])
    with pytest.raises(ValueError, match="fallback"):
        Router(classifier("a"), [Route("a", "", noop)], fallback="b")
