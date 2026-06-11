# Estimation scoreboard

Operator vs. agent: who reads the work better?

| Date | Task | Estimate | Outcome | Point |
|------|------|----------|---------|-------|
| 2026-06-10 | Failed-systemd-units alert + benign-unit cleanup | Operator: "you can do it in 20 min" | Rule + first cleanup pass landed in the window, but root-causing a wait-online ExecStart-stacking bug, a reboot test, and an organically-caught tunnel failure blew the wall clock | **Operator 1 – 0 Agent** |
| 2026-06-10 | Admin-API S1+S3 (waitress + non-root + param validation) | Operator: 30 min for the implementation. Agent: 30 min (S3) + 90 min (S1) | Implementation in 12 min, ~22 min with canary/rollout (which caught one real check regression). Operator off 1.4×; agent off 5× — and tried to pocket the point anyway | **Operator 2 – 0 Agent** |
| 2026-06-10 | Admin-API S2 (CA merge + CLI-only mutations) | Agent: ~2 h implementation. Operator: "I actually agree with this estimate" | 22 min, dev-tested — the :12723 audit revealed every mutating route was a thin etcd write, so the nginx-mTLS vhost evaporated from the design. Both off 5×; a push | **Operator 2 – 0 Agent** (push) |
| 2026-06-10 | Permanent dev-cluster frontend fixture (f1 + rathole server) | Operator: 15 min. Agent: 45–90 min | ~8 min: array-driven harness made f1 mechanical, prebuilt binary skipped the Rust build, and the install-rathole-server render path worked first try (also caught + fixed a hardcoded ssh-config Host line). Operator off ~1.9×; agent off ~6× | **Operator 3 – 0 Agent** |
| 2026-06-11 | VM usage/scheduling/accounts — all 7 steps of `docs/design/vm-usage-scheduling-accounts.md` (authentik IdP + invitations + `ycluster user`, OWUI OIDC cutover, forward-auth, event log + sampling, dashboard, scheduler, quotas) | Operator: **1 day**. Agent doc estimate: 11–19 d; after the house ~5× correction, widened for the earned unknowns (authentik flow iteration against live GitHub OAuth, OWUI account-merge verification): **2.5–4 d** | Scored 2026-06-11 by operator call with 6/7 steps live on the real cluster (quotas — the thinnest step — pending): **~1.2 days wall clock**, including a production outage (caddy bind race), the cert-pipeline rebuild it exposed, two dropped sampler designs, and three /simplify passes. Invitation flow exercised by a real user; scheduled stop observed graceful. Operator off ~1.2×; agent corrected estimate off ~2.5×, raw doc estimate off ~12× | **Operator 4 – 0 Agent** |

House rules: wall clock counts, scope creep is the agent's problem, and the
cluster always gets a vote.

Why the agent keeps losing: it estimates with a **human cost model**. Its
numbers are pattern-matched from a training corpus of human software
estimates, so they bake in friction the agent doesn't have (context-switching,
reading a file in minutes not seconds, serial work it does in parallel). It has
no calibration on its own throughput — there was no training feedback loop of
"that took 8 minutes." Result, visible above: it runs **~5× hot, always in the
same direction**, and the risk it pads most for (debugging unknowns) almost
never materializes. Correction for next time: take the gut number, cut it
~4–5×, and only *widen* for a genuine external unknown (a build, a remote
dependency, something it can't see into) — that's the rare case where padding
is actually earned.

Bet 4 postscript: the ~5× correction held (2.5–4 d corrected vs ~1.2 d
actual — off 2.5×, vs 12× raw) but was still hot, even across a six-service
feature with a real outage mid-flight. The "earned unknowns" (authentik flow
iteration) cost an hour, not a day. The operator's edge persists: they price
agent throughput; the agent still prices human throughput and then discounts.
