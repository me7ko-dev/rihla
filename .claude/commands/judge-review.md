---
description: Review Rihla the way the Qloo Agentic Hackathon judges will, then fix what is weak
---

# Judge review — Qloo Agentic Hackathon

You are a demanding hackathon judge **and** the engineer who must fix what you find. Review Rihla against the
official judging criteria, rank the problems by how much they would cost us points, then fix the top ones.

## The hackathon (facts to check against, not to re-research)

- **Qloo Agentic Hackathon — "Agents, but with taste."** https://qloo.devpost.com/
- **Deadline:** Oct 30, 2026, 11:45 pm EDT. Prizes $15K / $6K / $4K; Jason Calacanis invests $25K in his favourite.
- **Build:** an agentic tool, an agent-powered app, or Qloo connected to an existing agent, using the Qloo API
  (hackathon base URL `https://hackathon.api.qloo.com`, header `X-Api-Key`, GET only; `/search`, `/v2/insights`, `/v2/tags`).
- **Submission must have:** a *functional, hosted, end-to-end demo*; a public code repo with a visible open-source
  license; a text description. **Judges use the live demo directly** (a video is optional) — so the first 30 seconds
  on the live site decide most of the score.
- **Judges:** Mike Diolosa (CTO, Qloo), Jason Calacanis, Nicole Seligman (OpenAI board), Todd Boehly,
  Cedric the Entertainer, Michael Abrams (Live Nation). Mostly non-engineers: clarity beats cleverness.
- **Criteria (equal weight unless the rules say otherwise):**
  1. **Technological implementation** — how thoroughly and skilfully does the project use Qloo?
  2. **Design** — a complete, coherent product experience.
  3. **Potential impact** — a real problem for a real audience.
  4. **Quality of the idea** — a creative, non-obvious use of Qloo.

## How to review

Work from the repo (`app/`, `web/`, `tests/`, `README.md`). Run things; do not just read them.

1. **Baseline:** `python -m pyflakes app tests && python -m pytest -q tests`. Must be green before and after.
2. **Run the app** (`uvicorn app.main:app --port 8090`; without keys Qloo runs in mock mode from `fixtures/`).
   Drive it with Playwright (Chromium at `/opt/pw-browsers`) at **390×844 (phone)** and **1440×900 (desktop)**:
   open each example plan, switch days, open the map, try the form with empty / odd / non-English input,
   try a network error. Screenshot each state into the scratchpad and **look at the screenshots**.
3. **Score each criterion 1–10** as each kind of judge would (Qloo CTO; investor; celebrity non-engineer), with
   one sentence of evidence per score.
4. **List findings**, each with: criterion, severity (*blocker / costs points / polish*), the concrete scenario
   a judge would hit, and the fix. Look especially for:
   - **First impression:** does a judge understand in 5 seconds what Rihla does and where Qloo is? Can they see
     a full plan in one click without waiting 30–90 s for the LLM?
   - **Live-demo failure modes:** LLM slow/down, Qloo quota (10,000/month, 5 req/s), rate limit (6/hour/IP —
     a judge testing a lot must never get stuck), Vercel 300 s limit, cold start, OSM/Overpass down,
     unknown city, 1-character input, mixed languages. Every path must end in a usable plan or a kind, clear message.
   - **Qloo depth:** which Qloo features are used (search, insights, signals, audiences, tags, filters, place
     properties, popularity, explainability) and is that *visible in the UI*, not only in code? What does a Qloo
     CTO expect that is missing or misused (wrong entity types, unused affinity scores, ignored `/v2/tags`)?
   - **Honesty:** no invented places, halal levels never overstated, "because you love X" only when true,
     prayer times correct for the date and place, Jumu'ah on Fridays, no alcohol venues.
   - **Design:** mobile layout, overflow, contrast, loading state, empty states, RTL Arabic, map usability,
     accessibility (labels, focus, keyboard).
   - **Repo & submission:** README answers "what / why / how Qloo / how to run / live link" in the first screen;
     license visible; tests badge green; no secrets committed; `.env.example` complete.
5. **Fix** the blockers and the "costs points" findings that are small and safe, one commit per fix, with a test
   where logic changed. Do not rewrite working features, do not add dependencies without need, do not touch the
   cached example plans unless they are wrong. Re-run step 1 and re-screenshot what changed.
6. **Report:** scores before → after, what was fixed (commit list), what is left and why, and what only the
   human can do (deploy, Devpost text, video, API keys).
