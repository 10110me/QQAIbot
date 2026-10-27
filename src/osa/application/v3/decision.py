"""Hard gates, a versioned heuristic classifier, and optional LLM fallback."""
import json
from osa.domain.agents import DecisionContext, DecisionResult
from osa.application.prompt_catalog import CATALOG


class RuleClassifier:
    async def predict(self, context: DecisionContext) -> DecisionResult:
        text = str(context.features.get("text", "")).strip()
        recent = context.features.get("recent_conversation", [])
        has_context = any(len(str(row.get("content", "")).strip()) >= 3 for row in recent) or any(row.get('status')=='ready' for row in context.features.get('images',[]))
        if len(text) < 3 and not has_context:
            return DecisionResult("SKIP", "heuristic", "rules-1", "low_information", .99)
        return DecisionResult("DEFER", "heuristic", "rules-1", "ambiguous_context", .5)


class HybridDecision:
    def __init__(self, model, classifier=None, mode="hybrid", threshold=.8):
        self.model = model
        self.classifier = classifier or RuleClassifier()
        self.mode, self.threshold = mode, threshold

    async def predict(self, context: DecisionContext) -> DecisionResult:
        f = context.features
        if f.get("hard_denied"):
            return DecisionResult("SKIP", "policy", "send-gates-3", "hard_gate")
        if f.get("direct"):
            return DecisionResult("THINK", "policy", "send-gates-3", "direct_request")
        if not f.get("direct") and not f.get("initiative_allowed"):
            return DecisionResult("SKIP", "policy", "send-gates-3", "initiative_disabled")
        if self.mode == "local":
            return DecisionResult("THINK", "policy", "send-gates-4", "local_candidate")
        candidate = await self.classifier.predict(context)
        if self.mode == "classifier" or (self.mode == "hybrid" and candidate.confidence is not None and candidate.confidence >= self.threshold):
            return candidate
        result = await self.model.generate([
            {"role": "system", "content": CATALOG['agent_decision']['default']},
            {"role": "user", "content": json.dumps(f, ensure_ascii=False)},
        ], "decision")
        data = json.loads(result.text)
        if data.get("action") not in {"THINK", "SKIP", "DEFER"}:
            raise ValueError("Invalid V3 decision")
        # A self-reported LLM number is never a calibrated probability.
        return DecisionResult(data["action"], result.provider, result.model,
                              str(data.get("reason_code", "llm_fallback"))[:120],
                              wait_seconds=max(1,min(30,float(data['wait_seconds']))) if data.get('wait_seconds') is not None else None)
