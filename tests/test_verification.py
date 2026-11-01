import unittest

from verification import RawClaim, review_outreach, validate_claims, verified

FINDINGS = [{"title": "Mistral docs", "url": "https://docs.mistral.ai/studio/observability",
             "snippet": "Mistral Studio collects traces using OpenTelemetry. Released in May."}]


def raw(**kw):
    base = dict(claim="Mistral collects traces", category="internal_tooling",
                source_url="https://docs.mistral.ai/studio/observability", excerpt="collects traces using OpenTelemetry")
    return RawClaim(**{**base, **kw})


class VerificationTests(unittest.TestCase):
    def test_supported_when_excerpt_is_verbatim(self):
        c = validate_claims([raw()], FINDINGS, "Mistral AI")[0]
        self.assertEqual(c.status, "supported")
        self.assertEqual(c.source_type, "official")

    def test_unsupported_claim_is_rejected(self):
        c = validate_claims([raw(excerpt="launched a billion-dollar observability suite")], FINDINGS, "Mistral AI")[0]
        self.assertEqual(c.status, "unsupported")
        self.assertEqual(verified([c]), [])

    def test_url_not_retrieved_is_not_verified(self):  # a plausible URL alone is not proof
        c = validate_claims([raw(source_url="https://example.com/made-up")], FINDINGS, "Mistral AI")[0]
        self.assertEqual(c.status, "unsupported")

    def test_empty_excerpt_is_unverifiable(self):
        c = validate_claims([raw(excerpt="")], FINDINGS, "Mistral AI")[0]
        self.assertEqual(c.status, "unverifiable")

    def test_event_date_not_in_excerpt_is_dropped(self):  # never infer a launch date
        c = validate_claims([raw(event_date="March 2026")], FINDINGS, "Mistral AI")[0]
        self.assertIsNone(c.event_date)

    def test_event_date_kept_when_stated_in_excerpt(self):
        f = [{**FINDINGS[0], "snippet": "Launched in May 2026: collects traces using OpenTelemetry"}]
        c = validate_claims([raw(excerpt="Launched in May 2026: collects traces", event_date="May 2026")], f, "Mistral AI")[0]
        self.assertEqual(c.event_date, "May 2026")


class OutreachReviewTests(unittest.TestCase):
    def claims(self):
        return validate_claims([raw(claim="Mistral launched Vibe with 3 coding agents")], FINDINGS[:0] or [
            {"title": "t", "url": "https://docs.mistral.ai/studio/observability",
             "snippet": "collects traces using OpenTelemetry"}], "Mistral AI")

    BODY = ("Hi, I saw Mistral collects traces with OpenTelemetry in Studio, so you already own tracing. "
            "We build LangSmith and LangChain, which many teams use alongside their own tooling. "
            "I would like to understand how your developers combine open standards with agent frameworks today, "
            "and whether an integration conversation could be useful for both sides in the coming quarter. "
            "Would you be open to a short chat about that?\n\nEliott Bonnet")

    def test_clean_message_passes(self):
        self.assertEqual(review_outreach("Tracing and LangChain", self.BODY, "Short note", [1], self.claims()), [])

    def test_invented_number_is_flagged(self):
        body = self.BODY.replace("many teams", "73% of teams")
        issues = review_outreach("s", body, "n", [1], self.claims())
        self.assertTrue(any("73" in i for i in issues))

    def test_unverified_claim_citation_is_flagged(self):
        issues = review_outreach("s", self.BODY, "n", [9], self.claims())
        self.assertTrue(any("not verified" in i for i in issues))

    def test_generic_pitch_is_flagged(self):
        body = self.BODY.replace("many teams", "observability becomes critical as you scale, and teams")
        self.assertTrue(any("becomes critical" in i for i in review_outreach("s", body, "n", [1], self.claims())))


if __name__ == "__main__":
    unittest.main()
