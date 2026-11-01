# 5-minute demo guide

Prepare: `source .venv/bin/activate`, LangSmith open on *Tracing Projects > prospecting-agent*, and these files ready:
`output/v1_Mistral_AI.md` (v1 result), `output/Mistral_AI.md` (v2 result), `scoring.py`.

**Minute 1 - The problem.** "Most AI prospecting confuses activity with buying intent. A company can use AI heavily and still never buy my product."

**Minute 2 - The workflow (LangGraph).** Show the Mermaid diagram in the README. Point at: competitive research looks for reasons NOT to sell; evidence is verified *before* scoring; four routing outcomes.

**Minute 3 - The v1 failure.** Open `output/v1_Mistral_AI.md`: 4/5 and a sales email. "Mistral builds LLMs and agents, but also ships its own tracing and evaluation. One score hid that."

**Minute 4 - v2 live + LangSmith.** Run `python agent.py "Mistral AI"`. Show the console line (technical / buyer / build-vs-buy / classification), then the trace in LangSmith: search -> claim extraction -> verification -> routing. Open `output/Mistral_AI.md`: qualification summary, existing tools, hypotheses with discovery questions, partnership email, and the sources table with claim-level status.

**Minute 5 - Impact.** Run `python evals/run_eval.py` (8/8 on synthetic cases) and say plainly it is a regression suite. "An SDR gets fewer wrong emails and better discovery questions."

**Fallback if live APIs fail:** use the saved `output/Mistral_AI.md` and `output/v1_Mistral_AI.md`, and run `python -m unittest discover -s tests -t .` (offline).

## Likely technical questions
- *Why not let the LLM score?* Scores must be reproducible and explainable; the LLM interprets evidence, rules compute numbers.
- *How do you stop hallucinated facts?* Each claim needs a verbatim excerpt that code finds in the retrieved text; unsupported claims never reach scoring or email; numbers in the email must come from cited claims.
- *What would you improve?* Full-page fetching, a bigger human-labelled dataset, a human approval step with LangGraph interrupts, cost tracking.

## Likely sales questions
- *Who would you contact at Mistral?* Not a buyer pitch: a partnerships / developer-relations or platform lead, around the existing integration.
- *When do you not email?* No verified evidence, no defensible angle, or an internal tool that already covers the need.
- *How do you use this day to day?* It prepares the research and a draft; I validate the hypotheses on the call and decide what to send.
