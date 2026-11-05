# Self-Improving-AI: upstream sync

[Browse project code](https://github.com/rishika1099/Self-Improving-AI/tree/main) | [Upstream](https://github.com/bhuvighosh3/self-improving-ai)

This default branch holds the scheduled synchronization workflow. Project code is on `main` and the other upstream branches.

Every 15 minutes, subject to GitHub scheduling delays, the workflow copies upstream branches. Same-name branches follow upstream; diverged fork commits are preserved under `_sync-backups/` first. Fork-only branches and upstream-deleted branches remain. The `sync` and `_sync-backups/` namespaces are reserved.

Configure the Actions secret `SYNC_TOKEN` with Contents and Workflows write access to this fork. Enable Actions and run **Sync upstream branches** once. Renew the token before expiration. Public-repository schedules can be disabled after 60 days of repository inactivity.
