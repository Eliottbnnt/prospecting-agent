import unittest

from scoring import Evidence, classify, prioritisation_key


def ev(**kw):
    return Evidence(**kw)


# Mistral regression fixture: strong AI relevance, internal tooling, documented integration, no unmet need.
MISTRAL = dict(agentic=2, production=2, hiring=2, internal_tooling=2, integration=2, verified_claims=10,
               competitive_claims=4, model_provider=True)


class ScoringTests(unittest.TestCase):
    def test_mistral_regression(self):
        q = classify(ev(**MISTRAL))
        self.assertGreaterEqual(q.technical_fit, 4)
        self.assertLessEqual(q.buyer_fit, 2)
        self.assertEqual(q.build_vs_buy_risk, "high")
        self.assertEqual(q.classification, "partnership_opportunity")

    def test_technical_high_buyer_low(self):
        q = classify(ev(agentic=3, production=2, hiring=1, internal_tooling=1, verified_claims=8, competitive_claims=1))
        self.assertGreaterEqual(q.technical_fit, 4)
        self.assertLess(q.buyer_fit, 3)

    def test_internal_tooling_raises_risk(self):
        base = dict(agentic=1, production=1, verified_claims=5, competitive_claims=1)
        self.assertEqual(classify(ev(**base, integration=1)).build_vs_buy_risk, "low")
        self.assertEqual(classify(ev(**base, internal_tooling=1)).build_vs_buy_risk, "high")

    def test_high_technical_fit_does_not_trigger_sales(self):
        q = classify(ev(**{**MISTRAL, "integration": 0}))
        self.assertNotEqual(q.classification, "sales_opportunity")

    def test_sales_opportunity(self):
        q = classify(ev(agentic=2, production=2, hiring=1, unmet_need=1, verified_claims=8, competitive_claims=1,
                        external_tool=0, integration=1))
        self.assertEqual(q.classification, "sales_opportunity")

    def test_low_confidence_routes_to_research(self):
        q = classify(ev(agentic=1, production=1, hiring=1, verified_claims=2))
        self.assertEqual(q.evidence_confidence, "low")
        self.assertEqual(q.classification, "needs_more_research")

    def test_no_evidence_is_research_not_rejection(self):
        self.assertEqual(classify(ev()).classification, "needs_more_research")

    def test_missing_information_is_not_negative(self):
        q = classify(ev(agentic=1, production=1, verified_claims=4))
        self.assertEqual(q.buyer_fit, 3)  # neutral
        self.assertEqual(q.build_vs_buy_risk, "unknown")

    def test_weak_technical_fit_not_qualified(self):
        q = classify(ev(verified_claims=5, competitive_claims=1))
        self.assertEqual(q.classification, "not_qualified")

    def test_rejected_evidence_not_counted(self):  # scoring only sees verified counts
        self.assertEqual(classify(ev(verified_claims=0)).classification, "needs_more_research")

    def test_prioritisation_puts_buyers_before_technical_fit(self):
        rows = [
            dict(account_classification="partnership_opportunity", buyer_fit=2, technical_fit=5, evidence_confidence="high"),
            dict(account_classification="sales_opportunity", buyer_fit=4, technical_fit=3, evidence_confidence="medium"),
        ]
        self.assertEqual(sorted(rows, key=prioritisation_key)[0]["account_classification"], "sales_opportunity")


if __name__ == "__main__":
    unittest.main()
