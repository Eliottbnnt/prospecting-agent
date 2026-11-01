import os
import unittest
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")  # no network call is made in these tests

import agent  # noqa: E402
from agent import Assessment, Hypothesis  # noqa: E402
from contacts import find_contacts  # noqa: E402
from verification import validate_claims  # noqa: E402


def fake_state(label="not_qualified", outreach_status="not_attempted"):
    findings = [{"title": "t", "url": "https://x.com/a", "snippet": "uses langgraph agents in production"}]
    from verification import RawClaim
    claims = validate_claims([RawClaim(claim="Uses agents", category="agentic_ai", source_url="https://x.com/a",
                                       excerpt="uses langgraph agents")], findings, "X")
    a = Assessment(company_summary="X builds agents.", is_model_provider=False, target_persona="VP Eng",
                   persona_job_titles=["VP Engineering"],
                   hypotheses=[Hypothesis(need="n", supporting_claim_ids=[1], why_it_matters="w",
                                          counter_evidence="none found", confidence="low", discovery_question="Q?")])
    q = dict(technical_fit=3, buyer_fit=3, evidence_confidence="medium", build_vs_buy_risk="unknown",
             partnership_potential=False, classification=label, reasons=["r", "because"],
             evidence=dict(verified_claims=1, competitive_claims=0, unmet_need=0, hiring=0))
    return {"company": "X", "claims": claims, "assessment": a, "qualification": q, "stats": {},
            "outreach_status": outreach_status}


class AgentTests(unittest.TestCase):
    def test_routing_outcomes(self):
        route = lambda label: agent.route_account({"qualification": {"classification": label}})
        self.assertEqual(route("sales_opportunity"), "find_contacts")
        self.assertEqual(route("partnership_opportunity"), "find_contacts")
        self.assertEqual(route("needs_more_research"), "research_gap")
        self.assertEqual(route("not_qualified"), "generate_report")

    def test_report_without_outreach(self):
        md = agent.generate_report(fake_state())["report"]
        self.assertIn("Qualification summary", md)
        self.assertIn("hypotheses to validate", md)
        self.assertIn("not_attempted", md)
        self.assertNotIn("## Email", md)

    def test_lusha_is_optional(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUSHA_API_KEY", None)
            self.assertEqual(find_contacts("X", ["CTO"]), [])

    def test_tracing_is_optional(self):
        with mock.patch.dict(os.environ, {"LANGSMITH_TRACING": "", "LANGSMITH_API_KEY": ""}):
            self.assertFalse(agent.tracing_enabled())

    def test_csv_row_has_expected_columns(self):
        row = agent.csv_row("X", fake_state())
        for col in ("technical_fit", "buyer_fit", "evidence_confidence", "build_vs_buy_risk", "account_classification",
                    "verified_signals_count", "competitive_findings", "recommended_action", "target_persona",
                    "outreach_status", "report_path"):
            self.assertIn(col, row)

    def test_graph_has_all_stages(self):
        nodes = set(agent.graph.get_graph().nodes)
        for n in ("plan_research", "research_company", "research_competitive_landscape", "verify_evidence",
                  "analyze_and_score", "write_outreach", "review_outreach", "generate_report"):
            self.assertIn(n, nodes)


if __name__ == "__main__":
    unittest.main()
