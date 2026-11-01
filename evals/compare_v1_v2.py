"""
Live comparison on the same companies: V1 (snapshot in v1/) vs V2 (current agent). Uses your API keys.

    python evals/compare_v1_v2.py "Mistral AI" "Qonto"
"""

import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")


def run(script: str, company: str) -> str:
    subprocess.run([sys.executable, "-W", "ignore", script, company], cwd=ROOT, capture_output=True, text=True, timeout=600)
    path = os.path.join(ROOT, "output", company.replace(" ", "_") + ".md")
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def main():
    print(f"{'company':20} | {'V1 (single fit score)':32} | V2 (technical / buyer / class / outreach)")
    for company in sys.argv[1:] or ["Mistral AI"]:
        v1 = run("v1/agent_v1.py", company)
        out = os.path.join(ROOT, "output", company.replace(" ", "_") + ".md")
        if os.path.exists(out):
            shutil.copy(out, os.path.join(ROOT, "output", "v1_" + company.replace(" ", "_") + ".md"))
        v2 = run("agent.py", company)
        s1 = re.search(r"Fit score:\*\* (\d)/5", v1)
        t = re.search(r"Technical fit:\*\* (\d)", v2); b = re.search(r"Buyer fit:\*\* (\d)", v2)
        c = re.search(r"Classification:\*\* `(\w+)`", v2)
        s1txt = f"{s1.group(1)}/5, email: {'yes' if '## Email' in v1 else 'no'}" if s1 else "n/a"
        s2txt = (f"{t.group(1) if t else '?'} / {b.group(1) if b else '?'} / {c.group(1) if c else '?'} / "
                 f"email: {'yes' if '## Email' in v2 else 'no'}")
        print(f"{company:20} | {s1txt:32} | {s2txt}")
    print("\nV1 reports saved as output/v1_<company>.md; V2 as output/<company>.md. Search results vary between runs.")


if __name__ == "__main__":
    main()
