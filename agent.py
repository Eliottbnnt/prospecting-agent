"""
Prospecting Agent v2 — built with LangChain + LangGraph (+ LangSmith tracing)
-----------------------------------------------------------------------------
Decides whether a company is likely to NEED and BUY an AI-agent platform like LangSmith,
not just whether it uses AI. Technical fit and buyer fit are scored separately, from verified evidence.

Usage:
    python agent.py "Mistral AI"
    python agent.py --file companies.txt          # batch mode -> output/results.csv

Graph:
    plan_research -> research_company -> research_competitive_landscape -> verify_evidence
      -> analyze_and_score -> route_account
           sales / partnership -> find_contacts -> write_outreach -> review_outreach -> generate_report
           needs_more_research -> research_gap -> generate_report
           not_qualified       -> generate_report
"""

import argparse
import csv
import os
import sys
import time
from dataclasses import asdict
from datetime import date
from typing import List, Literal, Optional, TypedDict

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

load_dotenv()

from contacts import find_contacts as lusha_find_contacts  # noqa: E402
from prompts import (ASSESS_PROMPT, EMAIL_PROMPT, EXTRACT_PROMPT, MODE_RULES, PROMPT_VERSION,  # noqa: E402
                     QUERIES_PROMPT, REVISE_PROMPT, SELLER_CONTEXT)
from scoring import Evidence, classify, prioritisation_key  # noqa: E402
from sources import search  # noqa: E402
from verification import Claim, RawClaims, review_outreach as check_outreach, validate_claims, verified  # noqa: E402

WORKFLOW_VERSION = "v2"
MODEL = os.getenv("MODEL", "anthropic:claude-haiku-4-5-20251001")
llm = init_chat_model(MODEL, max_retries=2, timeout=90)


# --------------------------------------------------------------------------
# Structured outputs
# --------------------------------------------------------------------------
class SearchQueries(BaseModel):
    queries: List[str] = Field(description="3 short web search queries", min_length=1, max_length=4)


class Hypothesis(BaseModel):
    need: str = Field(description="A potential need (NOT confirmed)")
    supporting_claim_ids: List[int] = Field(description="Ids of verified claims that make it plausible")
    why_it_matters: str = Field(description="Why this could matter commercially")
    counter_evidence: str = Field(description="Existing solutions or counter-evidence from the claims, else 'none found'")
    confidence: Literal["low", "medium", "high"]
    discovery_question: str = Field(description="One question that could validate it with the prospect")


class Assessment(BaseModel):
    company_summary: str = Field(description="2 sentences, from verified claims only")
    existing_tools: List[str] = Field(default_factory=list)
    competitive_alternatives: List[str] = Field(default_factory=list)
    is_model_provider: bool = Field(description="True if the company builds foundation models")
    hypotheses: List[Hypothesis] = Field(default_factory=list, max_length=3)
    unmet_needs: List[str] = Field(default_factory=list)
    commercial_blockers: List[str] = Field(default_factory=list)
    target_persona: str
    persona_job_titles: List[str] = Field(max_length=3)


class Outreach(BaseModel):
    outreach_possible: bool = Field(description="False if there is no defensible angle")
    email_subject: str = Field(default="", description="Max 7 words")
    email_body: str = Field(default="", description="70-120 words, plain text, one CTA question")
    linkedin_note: str = Field(default="", description="Max 300 characters")
    claims_used: List[int] = Field(default_factory=list, description="Ids of the verified claims used")


# --------------------------------------------------------------------------
# Graph state
# --------------------------------------------------------------------------
class State(TypedDict, total=False):
    company: str
    queries: List[str]
    findings: List[dict]
    competitive_findings: List[dict]
    claims: List[Claim]
    assessment: Assessment
    qualification: dict
    research_gaps: List[str]
    contacts: List[dict]
    mode: str  # sales | partnership
    outreach: Optional[Outreach]
    outreach_status: str  # drafted | draft_needs_edit | outreach_not_recommended | not_attempted
    outreach_issues: List[str]
    stats: dict
    report: str


def _claims_text(claims: List[Claim]) -> str:
    return "\n".join(f"[{c.id}] ({c.category}) {c.claim} — source: {c.source_url}"
                     + (f" — event date: {c.event_date}" if c.event_date else "") for c in claims) or "None."


def _dedupe(findings: List[dict]) -> List[dict]:
    seen, unique = set(), []
    for f in findings:
        if f["url"] and f["url"] not in seen:
            seen.add(f["url"])
            unique.append(f)
    return unique


def _bump(state: State, key: str, n: int = 1) -> dict:
    stats = dict(state.get("stats", {}))
    stats[key] = stats.get(key, 0) + n
    return stats


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------
def plan_research(state: State) -> State:
    out = llm.with_structured_output(SearchQueries).invoke(
        QUERIES_PROMPT.format(company=state["company"], today=date.today().isoformat()))
    print(f"  > searching: {out.queries}")
    return {"queries": out.queries}


def research_company(state: State) -> State:
    findings = []
    for q in state["queries"]:
        findings.extend(search(q))
    unique = _dedupe(findings)
    print(f"  > {len(unique)} company sources")
    return {"findings": unique, "stats": _bump(state, "searches", len(state["queries"]))}


def research_competitive_landscape(state: State) -> State:
    """Look for reasons NOT to sell: internal tooling, existing vendors, integrations. Fixed queries, no LLM."""
    c = state["company"]
    queries = [f"{c} LLM observability tracing evaluation platform built in-house",
               f"{c} LangSmith LangChain integration",
               f"{c} Langfuse OR Arize OR Braintrust OR Datadog LLM observability customer"]
    found = []
    for q in queries:
        found.extend(search(q))
    known = {x["url"] for x in state["findings"]}
    unique = _dedupe([f for f in found if f["url"] not in known])
    print(f"  > {len(unique)} competitive-landscape sources")
    return {"competitive_findings": unique, "stats": _bump(state, "searches", len(queries))}


def verify_evidence(state: State) -> State:
    """Extract claims with verbatim excerpts, then check them in code against what was retrieved."""
    findings = _dedupe(state["findings"] + state.get("competitive_findings", []))
    evidence = "\n".join(f"- {f['title']} ({f['url']}): {f['snippet']}" for f in findings)
    if not findings:
        print("  ! no sources retrieved")
        return {"claims": []}
    try:
        raw = llm.with_structured_output(RawClaims).invoke(
            EXTRACT_PROMPT.format(company=state["company"], evidence=evidence)).claims
    except Exception as e:  # invalid structured output must not kill the run
        print(f"  ! claim extraction failed: {e}")
        return {"claims": [], "stats": _bump(state, "verification_failures")}
    claims = validate_claims(raw, findings, state["company"])
    ok = len(verified(claims))
    print(f"  > claims: {ok} verified, {len(claims) - ok} rejected")
    stats = _bump(state, "claims_verified", ok)
    stats["claims_rejected"] = len(claims) - ok
    return {"claims": claims, "stats": stats}


def analyze_and_score(state: State) -> State:
    """LLM interprets VERIFIED claims; scores and classification are computed by rules (scoring.py)."""
    good = verified(state["claims"])
    count = lambda cat: sum(1 for c in good if c.category == cat)  # noqa: E731
    a = None
    if good:
        try:
            a = llm.with_structured_output(Assessment).invoke(
                ASSESS_PROMPT.format(company=state["company"], seller=SELLER_CONTEXT, claims=_claims_text(good)))
        except Exception as e:
            print(f"  ! assessment failed: {e}")
    if a is None:
        a = Assessment(company_summary="Not enough verified evidence.", is_model_provider=False,
                       target_persona="Unknown", persona_job_titles=["VP Engineering"])
    ev = Evidence(agentic=count("agentic_ai"), production=count("production"), hiring=count("hiring"),
                  unmet_need=count("unmet_need"), internal_tooling=count("internal_tooling"),
                  external_tool=count("external_tool"), integration=count("integration"),
                  verified_claims=len(good),
                  competitive_claims=count("internal_tooling") + count("external_tool") + count("integration"),
                  model_provider=a.is_model_provider)
    q = classify(ev)
    print(f"  > technical {q.technical_fit}/5 | buyer {q.buyer_fit}/5 | confidence {q.evidence_confidence} | "
          f"build-vs-buy {q.build_vs_buy_risk} | {q.classification}")
    return {"assessment": a, "qualification": {**asdict(q), "evidence": asdict(ev)},
            "mode": "partnership" if q.classification == "partnership_opportunity" else "sales"}


def route_account(state: State) -> str:
    """Four outcomes instead of one threshold."""
    label = state["qualification"]["classification"]
    if label in ("sales_opportunity", "partnership_opportunity"):
        return "find_contacts"
    return "research_gap" if label == "needs_more_research" else "generate_report"


def research_gap(state: State) -> State:
    """No pitch: say what is missing and how to validate it."""
    ev, gaps = state["qualification"]["evidence"], []
    if ev["verified_claims"] < 3:
        gaps.append("Fewer than 3 verified claims: more research is needed before any judgement.")
    if ev["competitive_claims"] == 0:
        gaps.append("Nothing verified about existing or internal observability / evaluation tooling (build-vs-buy unknown).")
    if ev["unmet_need"] == 0:
        gaps.append("No verified evidence of an unmet need around agent observability, evaluation or deployment.")
    if ev["hiring"] == 0:
        gaps.append("No verified AI/ML hiring signal.")
    return {"research_gaps": gaps}


def find_contacts_node(state: State) -> State:
    contacts = lusha_find_contacts(state["company"], state["assessment"].persona_job_titles, state["mode"])
    if os.getenv("LUSHA_API_KEY"):
        print(f"  > Lusha: {len(contacts)} senior contact(s)")
    return {"contacts": contacts}


def write_outreach(state: State) -> State:
    a = state["assessment"]
    hyp = "\n".join(f"- {h.discovery_question}" for h in a.hypotheses) or "None."
    out = llm.with_structured_output(Outreach).invoke(
        EMAIL_PROMPT.format(company=state["company"], seller=SELLER_CONTEXT, mode=state["mode"],
                            mode_rules=MODE_RULES[state["mode"]],
                            claims=_claims_text(verified(state["claims"])) + f"\n\nHypotheses to ask about (not facts):\n{hyp}"))
    return {"outreach": out}


def review_outreach_node(state: State) -> State:
    """Deterministic review; at most ONE targeted revision."""
    out = state["outreach"]
    if not out.outreach_possible:
        return {"outreach_status": "outreach_not_recommended", "outreach_issues": ["No defensible angle."]}
    check = lambda o: check_outreach(o.email_subject, o.email_body, o.linkedin_note, o.claims_used, state["claims"])  # noqa: E731
    issues = check(out)
    if issues:
        print(f"  > outreach review: {len(issues)} issue(s), one revision")
        out = llm.with_structured_output(Outreach).invoke(REVISE_PROMPT.format(
            issues="\n".join(f"- {i}" for i in issues), claims=_claims_text(verified(state["claims"])),
            subject=out.email_subject, body=out.email_body, linkedin=out.linkedin_note))
        issues = check(out) if out.outreach_possible else ["No defensible angle."]
    status = "outreach_not_recommended" if not out.outreach_possible else ("drafted" if not issues else "draft_needs_edit")
    return {"outreach": out, "outreach_status": status, "outreach_issues": issues}


RECOMMENDATION = {
    "sales_opportunity": "Reach out with a buyer-focused message; use the discovery questions to test the hypotheses.",
    "partnership_opportunity": "Open an ecosystem / integration conversation rather than a direct LangSmith pitch.",
    "needs_more_research": "Do not reach out yet: close the research gaps below first.",
    "not_qualified": "No outreach recommended.",
}


def generate_report(state: State) -> State:
    q, a, claims = state["qualification"], state["assessment"], state["claims"]
    good = verified(claims)
    by = lambda *cats: [c for c in good if c.category in cats]  # noqa: E731
    cite = lambda c: (f"- {c.claim} ([source]({c.source_url}); "  # noqa: E731
                      + (f"event date: {c.event_date}" if c.event_date else "date not stated in source") + ")")
    buy = [cite(c) for c in by("unmet_need")] + [cite(c) for c in by("production", "agentic_ai")][:2]
    nobuy = [cite(c) for c in by("internal_tooling", "external_tool")] + [f"- {b}" for b in a.commercial_blockers]
    md = [f"# {state['company']} — Account brief", "",
          "## Company overview", a.company_summary, "",
          "## Qualification summary",
          f"- **Technical fit:** {q['technical_fit']}/5",
          f"- **Buyer fit:** {q['buyer_fit']}/5",
          f"- **Evidence confidence:** {q['evidence_confidence']}",
          f"- **Build-vs-buy risk:** {q['build_vs_buy_risk']}",
          f"- **Partnership potential:** {'yes' if q['partnership_potential'] else 'no'}",
          f"- **Classification:** `{q['classification']}`",
          f"- **Recommendation:** {RECOMMENDATION[q['classification']]}",
          "- **Why:** " + " ".join(q["reasons"][1:]), "",
          "## Verified AI signals", *([cite(c) for c in by("agentic_ai", "production", "hiring")] or ["_None verified._"]), "",
          "## Existing tools and competitive landscape",
          *([cite(c) for c in by("internal_tooling", "external_tool", "integration")] or ["_Nothing verified either way._"]),
          *([f"- Tools mentioned: {', '.join(a.existing_tools + a.competitive_alternatives)}"]
            if a.existing_tools or a.competitive_alternatives else []), "",
          "## Potential needs / hypotheses to validate (not confirmed)"]
    for h in a.hypotheses:
        md += [f"- **{h.need}** (confidence: {h.confidence})",
               f"  - Why it could matter: {h.why_it_matters}",
               f"  - Supporting verified claims: {', '.join(map(str, h.supporting_claim_ids)) or 'none'}",
               f"  - Counter-evidence / existing solutions: {h.counter_evidence}",
               f"  - Discovery question: _{h.discovery_question}_"]
    if not a.hypotheses:
        md.append("_None._")
    md += ["", "## Why this account may buy", *(buy or ["_No evidence-backed argument._"]), "",
           "## Why this account may not buy",
           *(nobuy or ["_No counter-evidence found (this is not proof there is none)._"]), "",
           "## Recommended approach", RECOMMENDATION[q["classification"]], ""]
    if state.get("research_gaps"):
        md += ["### Research gaps", *[f"- {g}" for g in state["research_gaps"]], ""]
    md += ["## Target personas", a.target_persona]
    if state.get("contacts"):
        md += ["", "Verified contacts (Lusha):",
               *[f"- {c['name']} — {c['title']} {c['linkedin']}".rstrip() for c in state["contacts"]]]
    md += ["", "## Suggested discovery questions", *([f"- {h.discovery_question}" for h in a.hypotheses] or ["_None._"]), ""]
    o, status = state.get("outreach"), state.get("outreach_status", "not_attempted")
    if o and status in ("drafted", "draft_needs_edit"):
        md += ["## Email", f"**Subject:** {o.email_subject}", "", o.email_body, "", "## LinkedIn note", o.linkedin_note, ""]
        if status == "draft_needs_edit":
            md += ["_Review flagged: " + "; ".join(state["outreach_issues"]) + "_", ""]
    else:
        why = "No defensible angle." if status == "outreach_not_recommended" else "Not recommended for this classification."
        md += ["## Outreach", f"_Status: `{status}`. {why}_", ""]
    md += ["## Sources and verification",
           "| # | Status | Category | Claim | Source (type) | Retrieved |", "|---|---|---|---|---|---|"]
    md += [f"| {c.id} | {c.status} | {c.category} | {c.claim[:90]} | [{c.source_type}]({c.source_url}) | {c.retrieved_at[:10]} |"
           for c in claims]
    md += ["", "_Publication dates are not available from search snippets; event dates are shown only when a source states them. "
               "Rejected claims are listed above but never used for scoring or outreach._",
           f"_Run stats: {state.get('stats', {})}_"]
    try:  # make traces easy to read in LangSmith (silently skipped if tracing is off)
        from langsmith.run_helpers import get_current_run_tree
        rt = get_current_run_tree()
        if rt:
            rt.metadata.update({"classification": q["classification"], "technical_fit": q["technical_fit"],
                                "buyer_fit": q["buyer_fit"], "build_vs_buy_risk": q["build_vs_buy_risk"]})
    except Exception:
        pass
    return {"report": "\n".join(md)}


# --------------------------------------------------------------------------
# Build the graph
# --------------------------------------------------------------------------
def build_graph():
    g = StateGraph(State)
    for name, fn in [("plan_research", plan_research), ("research_company", research_company),
                     ("research_competitive_landscape", research_competitive_landscape),
                     ("verify_evidence", verify_evidence), ("analyze_and_score", analyze_and_score),
                     ("research_gap", research_gap), ("find_contacts", find_contacts_node),
                     ("write_outreach", write_outreach), ("review_outreach", review_outreach_node),
                     ("generate_report", generate_report)]:
        g.add_node(name, fn)
    g.add_edge(START, "plan_research")
    g.add_edge("plan_research", "research_company")
    g.add_edge("research_company", "research_competitive_landscape")
    g.add_edge("research_competitive_landscape", "verify_evidence")
    g.add_edge("verify_evidence", "analyze_and_score")
    g.add_conditional_edges("analyze_and_score", route_account, ["find_contacts", "research_gap", "generate_report"])
    g.add_edge("find_contacts", "write_outreach")
    g.add_edge("write_outreach", "review_outreach")
    g.add_edge("review_outreach", "generate_report")
    g.add_edge("research_gap", "generate_report")
    g.add_edge("generate_report", END)
    return g.compile()


graph = build_graph()


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
def tracing_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").lower() == "true" and bool(os.getenv("LANGSMITH_API_KEY"))


def run_one(company: str) -> State:
    print(f"\n=== {company} ===")
    t0 = time.time()
    state = graph.invoke(
        {"company": company, "stats": {}},
        config={"run_name": f"prospect: {company}", "tags": ["prospecting-agent", WORKFLOW_VERSION],
                "metadata": {"company": company, "workflow_version": WORKFLOW_VERSION,
                             "prompt_version": PROMPT_VERSION, "model": MODEL}})
    state["stats"] = {**state.get("stats", {}), "seconds": round(time.time() - t0, 1)}
    return state


def report_path(company: str) -> str:
    return f"output/{company.replace(' ', '_')}.md"


def csv_row(company: str, s: State) -> dict:
    q, a = s["qualification"], s["assessment"]
    return {"company": company, "technical_fit": q["technical_fit"], "buyer_fit": q["buyer_fit"],
            "evidence_confidence": q["evidence_confidence"], "build_vs_buy_risk": q["build_vs_buy_risk"],
            "account_classification": q["classification"], "verified_signals_count": q["evidence"]["verified_claims"],
            "competitive_findings": q["evidence"]["competitive_claims"],
            "recommended_action": RECOMMENDATION[q["classification"]], "target_persona": a.target_persona,
            "contacts": "; ".join(f"{c['name']} ({c['title']})" for c in s.get("contacts", [])),
            "outreach_status": s.get("outreach_status", "not_attempted"), "report_path": report_path(company),
            "low_confidence": "yes" if q["evidence_confidence"] == "low" else ""}


def main():
    p = argparse.ArgumentParser(description="LangGraph prospecting agent v2")
    p.add_argument("company", nargs="?", help="Company name")
    p.add_argument("--file", help="Text file with one company per line (batch mode)")
    args = p.parse_args()
    os.makedirs("output", exist_ok=True)
    if not tracing_enabled():
        print("! LangSmith tracing is OFF (set LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env to see every step).")

    if args.file:
        rows = []
        for c in [l.strip() for l in open(args.file, encoding="utf-8") if l.strip()]:
            s = run_one(c)
            rows.append(csv_row(c, s))
            open(report_path(c), "w", encoding="utf-8").write(s["report"])
        rows.sort(key=prioritisation_key)  # commercial priority, not technical fit alone
        with open("output/results.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        print("\nDone -> output/results.csv (sorted by commercial priority)")
    elif args.company:
        s = run_one(args.company)
        open(report_path(args.company), "w", encoding="utf-8").write(s["report"])
        print("\n" + s["report"] + f"\n\nSaved -> {report_path(args.company)}")
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
