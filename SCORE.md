# Estimation scoreboard

Operator vs. agent: who reads the work better?

| Date | Task | Estimate | Outcome | Point |
|------|------|----------|---------|-------|
| 2026-06-10 | Failed-systemd-units alert + benign-unit cleanup | Operator: "you can do it in 20 min" | Rule + first cleanup pass landed in the window, but root-causing a wait-online ExecStart-stacking bug, a reboot test, and an organically-caught tunnel failure blew the wall clock | **Operator 1 – 0 Agent** |

House rules: wall clock counts, scope creep is the agent's problem, and the
cluster always gets a vote.
