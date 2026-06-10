# Estimation scoreboard

Operator vs. agent: who reads the work better?

| Date | Task | Estimate | Outcome | Point |
|------|------|----------|---------|-------|
| 2026-06-10 | Failed-systemd-units alert + benign-unit cleanup | Operator: "you can do it in 20 min" | Rule + first cleanup pass landed in the window, but root-causing a wait-online ExecStart-stacking bug, a reboot test, and an organically-caught tunnel failure blew the wall clock | **Operator 1 – 0 Agent** |
| 2026-06-10 | Admin-API S1+S3 (waitress + non-root + param validation) | Operator: 30 min for the implementation. Agent: 30 min (S3) + 90 min (S1) | Implementation in 12 min, ~22 min with canary/rollout (which caught one real check regression). Operator off 1.4×; agent off 5× — and tried to pocket the point anyway | **Operator 2 – 0 Agent** |
| 2026-06-10 | Admin-API S2 (CA merge + CLI-only mutations) | Agent: ~2 h implementation. Operator: "I actually agree with this estimate" | 22 min, dev-tested — the :12723 audit revealed every mutating route was a thin etcd write, so the nginx-mTLS vhost evaporated from the design. Both off 5×; a push | **Operator 2 – 0 Agent** (push) |

House rules: wall clock counts, scope creep is the agent's problem, and the
cluster always gets a vote.
