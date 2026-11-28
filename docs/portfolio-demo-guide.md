# Portfolio Agent demonstration guide

In about seven minutes, show how routine portfolio work updates a client-specific risk willingness score, how the score changes the next analysis, and how an advisor can trace or override it. Use Garcia Family (CLI-001), a synthetic household, for the figures below.

## Before the demo

- Open the dashboard and select **Portfolio Analysis**. The [checkout setup](#start-with-a-fresh-local-learning-store) serves it at [localhost:8000](http://localhost:8000/). Use a fresh learning store for the example scores, or note the starting score if the household already has records.
- In **Client preference learning**, check that **Learning module connected** is visible and pending records have finished. The main demonstration uses **Portfolio calculations** and a clean queue. These calculations and the learning from allocation trials can run without a Gemini answer.
- For the optional agent and client-note examples, open **Connections**, check the configured Gemini model, and test it before the presentation. Leave keys out of the screen share.
- Use the demo's synthetic holdings and the stated stock and bond assumptions throughout.

## Live walkthrough

### 1. Run a calculation and show what the system learns

1. Set **Workflow** to **Portfolio calculations**, **Client** to **Garcia Family**, and **Analysis** to **Allocation drift**.
2. Point out **Client risk willingness**. A fresh household starts at **50.0 / 100**. Lower scores favor limiting volatility. Higher scores indicate a greater willingness to take risk.
3. In **Stock target (%)**, enter **60** and select **Run analysis**. Expect a synthetic portfolio value of **$146,750**, a current stock allocation of about **79.86%**, and a hypothetical **$29,150 reduction** in stocks to reach the 60% target.
4. Wait for the score update. A fresh Garcia record should show about **48.1**. Open **View score drivers** to point out **Allocation trials**. The requested 60% stock target is weak directional evidence because it is below the existing stock weight.
5. If time allows, try **100%**, then **60%** again in the same analysis round. The latest trial changes the direction within the daily limits. With the fresh demo data the score moves to about **52.0** and then back to **48.1**.

**What this demonstrates.** A routine calculation contributes evidence without an extra survey. The score shows the direction and magnitude of the update. A trial records a scenario explored by the advisor. Confirm client approval separately.

> “I asked for a normal calculation. The agent learned a little from the direction I explored, and I can see exactly how much it changed the score.”

### 2. Show how the score changes the analysis

1. Keep **Stock target (%)** at **60** and find **Allocation stress test** in the result. The score used for the run is captured before the calculation's new observation is applied. If you want the newly updated score to drive the comparison, run the same analysis once more.
2. With the fresh demo score near neutral, find the **55%** lower-stock and **65%** higher-stock scenarios around the 60% target. Keep **Stock shock** at **20%** and enter **17000** in **Loss limit**.
3. Point out that the 60% target shows **$17,610** of downside, **$610 over** the limit. The 55% scenario shows **$16,142.50** of downside and **$857.50** of room below the limit. Under the symmetric upside shock, it also earns **$1,467.50 less** than the target.
4. Open **Adjust score**, set the household score to **75**, leave **Lock applied score** off, and select **Save score**. Run the same **60%** analysis again. The run shows the advisor-adjusted score and tests the higher-stock alternative. Point out the original 60% comparison target and the change in the supplemental scenario.
5. If asked, show **Lock applied score**. With the switch off, the advisor's setting is a starting point for future updates. With it on, the applied score stays fixed while the underlying system score continues to update. Use **Remove manual adjustment** to return to automatic learning.

**What this demonstrates.** The client score drives an actual scenario calculation. The advisor can compare downside and upside in dollars, choose a loss limit for the exercise, and keep control over how the score is used. Verify financial capacity for losses separately.

> “The trade-off is visible in dollars. I can ask whether this amount of downside is acceptable for the extra upside without changing the requested target.”

### 3. Trace the learning back to an operation

1. Open **Client preference learning → Operation history**. Find the latest allocation trial and show the before-and-after applied score. Copy its **UNI-** operation ID.
2. Paste the ID into **Find by operation ID** and select **Find**. The page opens the relevant record, including on an earlier page. Expand **View inputs and learning details** to see the stored inputs and interpretation.
3. Open **Scoring evidence**. Use **View criteria and calculation** for the evidence breakdown. Where a source is cited, **View original record** shows the same operation ID and opens the underlying record.
4. If your demo includes a mistaken test entry, show **Withdraw this record** on that synthetic entry and check its effect in the history. A withdrawn record retains its operation ID for traceability.

**What this demonstrates.** Advisors can explain a score change, return to the input that caused it, and correct the history.

### Optional two-minute AI example

- Select **Ask Portfolio Agent (Gemini)** and submit “What changes would bring the selected client's portfolio to 60% stocks and 40% bonds? Summarize the trade-offs in plain English.” Point out the English answer and the calculation under **Execution evidence**.
- Alternatively, open **Client overview**, enter “The client is very conservative overall and wants to avoid large swings in portfolio value.” Select **Save client note**, then inspect its interpretation once processing finishes. Confirm the original wording, scope, and score effect in **Operation history**. An explicit preference is stronger evidence than a ratio trial. Avoid promising an exact point movement for a live model interpretation.
- If Gemini is unavailable, continue the demonstration in **Portfolio calculations**. Pending inputs remain visible in **Operation history** with their UNI IDs. Use **Retry pending records** after the connection recovers.

## Choose your emphasis

| Audience | Emphasize |
| --- | --- |
| Teammates | Run the same target twice with different applied scores. Show the extra scenario changing and the UNI links tying the score to its source. |
| Professor | Start from 50 and show the score moving up and down as weak evidence changes. Explain why stronger statements, scope, and bounded daily influence matter. |
| Company lead | Start with the loss-limit example. Show what an advisor can discuss with a client and how a decision can be reviewed later. |

## Start with a fresh local learning store

From the repository root, use Node 20, an active Python 3.11 environment, and a running Firestore emulator populated with the team's synthetic households. Set the emulator address and project to match your environment. The local workspace setting stores calendar and inbox state locally. The client list still comes from Firestore.

```powershell
python -m pip install -e .
npm --prefix dashboard ci
npm --prefix dashboard run build

$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8088'
$env:SIA_GOOGLE_CLOUD_PROJECT = 'demo-self-improving-ai-local'
$env:GOOGLE_CLOUD_PROJECT = $env:SIA_GOOGLE_CLOUD_PROJECT
$env:SIA_WORKSPACE_STORE = 'local'
$env:GOOGLE_GENAI_USE_VERTEXAI = 'FALSE'
$env:SIA_OTEL_EXPORTER = 'console'
$env:SIA_PORTFOLIO_LEARNER_FACTORY = 'self_improving_ai.agents.portfolio.preference_learning.adapter:create_adapter'
$demoDirectory = Join-Path $PWD ('.local-settings/portfolio-demo-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $demoDirectory | Out-Null
$env:SIA_PORTFOLIO_LEARNING_DIR = Join-Path $demoDirectory 'learning'
$env:SIA_DB_PATH = Join-Path $demoDirectory 'workspace.sqlite3'
python -m uvicorn self_improving_ai.dashboard.app:app --host 127.0.0.1 --port 8000
```

Open [localhost:8000](http://localhost:8000/). To use the prepared history below, stop the server and run the seed in the same PowerShell window before restarting it. For the exact neutral-score examples, leave the learning directory empty. A deployment that enables preference learning needs durable storage for the journal and learner state.

## Load a prepared rehearsal with no Gemini key

The repository includes an optional seed for a local Firestore emulator with the synthetic households loaded. Run it from the repository root with the project Python environment active. Use the same learning directory as your preview.

```powershell
python scripts/seed_portfolio_demo.py --learning-dir "$env:SIA_PORTFOLIO_LEARNING_DIR" --emulator "$env:FIRESTORE_EMULATOR_HOST" --project "$env:SIA_GOOGLE_CLOUD_PROJECT"
```

The seed adds seven Garcia Family operations and three Thompson Trust operations. Garcia demonstrates same-round trials, a conservative statement, a recorded loss constraint, and an exploratory trial that points in the opposite direction. Thompson provides a separate, growth-oriented profile.

To use this saved starting point, run a 60% allocation calculation for each household. Garcia's score produces a lower-stock scenario. Thompson's score produces a higher-stock scenario. Then open **Scoring evidence** and follow a UNI link to its original record. Use an empty learning store for the neutral-score walkthrough and the exact figures in steps 1 and 2.

The sample profiles use scripted interpretations of their synthetic notes. The application runs the calculations, score updates, and source links. Rerunning the seed in the same directory will keep the same operations.
