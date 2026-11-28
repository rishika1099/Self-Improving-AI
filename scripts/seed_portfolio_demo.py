"""Populate a local Portfolio learning store with labeled synthetic operations."""

import argparse
import os
from pathlib import Path
from urllib.parse import urlsplit


CONSERVATIVE = "The client is very conservative overall and is unwilling to accept large losses."
LOSS_LIMIT = "Keep the hypothetical portfolio loss below $17,000 under a 20% stock shock."
GROWTH = "The client is willing to accept more volatility in exchange for higher long-term returns."

# Operations in the same group share a round. The recorded notes have exact,
# scripted interpretations so this rehearsal requires no model credentials.
DEMO = (
    ("CLI-001", "garcia-trial-60", "garcia-ratio", "allocation", 0.60),
    ("CLI-001", "garcia-trial-95", "garcia-ratio", "allocation", 0.95),
    ("CLI-001", "garcia-trial-60-again", "garcia-ratio", "allocation", 0.60),
    ("CLI-001", "garcia-willingness", "garcia-note", "note", CONSERVATIVE),
    ("CLI-001", "garcia-loss-limit", "garcia-budget", "note", LOSS_LIMIT),
    ("CLI-001", "garcia-explores-growth", "garcia-second-round", "allocation", 0.95),
    ("CLI-001", "garcia-holdings", "garcia-second-round", "holdings", None),
    ("CLI-002", "thompson-holdings", "thompson-round", "holdings", None),
    ("CLI-002", "thompson-willingness", "thompson-note", "note", GROWTH),
    ("CLI-002", "thompson-risk", "thompson-round", "risk", 0.65),
)
PREFIX = "portfolio-demo-v1-"
FACTORY = "self_improving_ai.agents.portfolio.preference_learning.adapter:create_adapter"


def interpretation(text, kind, dimension, value, reason, **extra):
    return {"kind": kind, "dimension": dimension, "value": value, "assertion": "affirmed",
            "scope": None, "evidence_text": text, "reason": reason, **extra}


def seed():
    from self_improving_ai.agents.portfolio.application import calculate_for_client, save_client_note
    from self_improving_ai.agents.portfolio.learning import Journal, adapter_for, workspace
    from self_improving_ai.agents.portfolio.preference_learning.parser import FixtureParser

    journal = Journal()
    expected = {
        client: {PREFIX + key for owner, key, *_ in DEMO if owner == client}
        for client in ("CLI-001", "CLI-002")
    }
    for client, demo_ids in expected.items():
        if any(row["event"] not in demo_ids for row in journal.rows(client)):
            raise ValueError(f"{client} already has other history. Choose an empty demo learning directory.")
    adapter = adapter_for(FACTORY, str(workspace()))
    adapter.learner.parser = FixtureParser({
        CONSERVATIVE: [interpretation(CONSERVATIVE, "explicit_statement", "risk_tilt", "conservative",
                                     "The advisor directly describes the client's overall willingness to take risk.",
                                     risk_basis="declared_attitude", risk_intensity="strong")],
        LOSS_LIMIT: [interpretation(LOSS_LIMIT, "constraint", "constraint", "max_loss_limit",
                                   "The advisor supplied an explicit hypothetical loss limit.")],
        GROWTH: [interpretation(GROWTH, "explicit_statement", "risk_tilt", "growth",
                               "The advisor describes the client's trade-off between risk and expected return.",
                               risk_basis="risk_return_tradeoff")],
    })
    for client, key, group, task, value in DEMO:
        meta = {"event_id": PREFIX + key, "evidence_group_id": PREFIX + group}
        if task == "note":
            run = save_client_note(value, client, {**meta, "text_source": "manual"})
        else:
            arguments = {"client_id": client}
            if value is not None:
                arguments["target_stock_weight"] = value
                meta["parameter_sources"] = {"target_stock_weight": "manual"}
            run = calculate_for_client(task, arguments, client, meta)
        if run["status"] != "success" or run["learning"]["status"] != "connected":
            raise RuntimeError(f"The synthetic operation {key} did not complete. Check the local demo setup.")
    for client, demo_ids in expected.items():
        view = journal.view(client)
        if view["total_events"] != len(demo_ids) or view["review_count"]:
            raise RuntimeError(f"The synthetic records for {client} did not all process cleanly.")
        print(f"{client}: {view['total_events']} synthetic operations, {view['pending_count']} pending.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--learning-dir", required=True, type=Path, help="Local Portfolio learning directory.")
    parser.add_argument("--emulator", default="127.0.0.1:8088", help="Local Firestore emulator host and port.")
    parser.add_argument("--project", default="demo-self-improving-ai-local", help="Project used by the emulator.")
    args = parser.parse_args()
    endpoint = urlsplit(f"//{args.emulator}")
    if endpoint.hostname not in {"127.0.0.1", "localhost", "::1"} or not endpoint.port:
        parser.error("Use a loopback Firestore emulator address with a port.")
    os.environ.update(FIRESTORE_EMULATOR_HOST=args.emulator, GOOGLE_CLOUD_PROJECT=args.project,
                      SIA_GOOGLE_CLOUD_PROJECT=args.project, SIA_WORKSPACE_STORE="local",
                      SIA_PORTFOLIO_LEARNING_DIR=str(args.learning_dir.resolve()),
                      SIA_PORTFOLIO_LEARNER_FACTORY=FACTORY, SIA_PORTFOLIO_API_KEY="")
    seed()


if __name__ == "__main__":
    main()
