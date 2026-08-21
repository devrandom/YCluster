# Alternative / redundant alert delivery paths

All notifications currently rely on a single path: Alertmanager → local
ntfy-webhook (`config/ansible/monitoring/files/ntfy-webhook.py`, `:9095`) →
direct HTTPS to the ntfy.sh topic. If cluster egress to ntfy.sh is
intermittently unreachable, **every** alert delivery (node-down, node-recovery,
CVE, …) silently degrades together, and the flood of 503 retries masks the
real problem. The second configured Alertmanager receiver
(`http://localhost:12723/api/alert-webhook`) is an ack-only endpoint in
`config/ansible/admin/files/app.py` and does not forward notifications, so it
is not a real fallback today.

Priority rationale: **HIGH**. A transient egress failure caused a node
down/recovery pair to go entirely unnotified (only a ~late down alert ever
landed; the recovery never did) while unrelated alerts were still arriving —
an operator-visible delivery blind spot on the alerting path itself.

## Work

- **Add a redundant/secondary delivery channel.** If ntfy.sh is down or
  unreachable, fall back to a second route (SMTP, a second ntfy instance /
  topic reachable on a different path, a hosted status endpoint, etc.) so a
  single unreachable egress path can't drop node-down / node-recovery
  notifications.
- **Make `ntfy-webhook` durable.** Buffer/resume failed sends in a small local
  queue with backoff so a dead egress window doesn't lose alerts or turn into
  an unrestrained retry flood; retry and flush once the path comes back.
- **Either make the ack-only `12723` webhook a real fallback, or add a
  distinct one.** It currently accepts alerts but only acks — wire it to
  actually forward (or add a separate fallback receiver) so the "second"
  receiver is genuinely redundant.
- **Add a delivery-failure metric + alert.** A gauge/counter of failed
  notification sends (exposed per receiver) with an alert when the failure
  rate spikes, so a dead alerting path pages on its own instead of silently
  swallowing alerts — same class of blind spot as the
  cluster-heartbeat false-HEALTHY follow-up.
- **Control the retry volume.** Once a durable queue exists, add
  rate-limiting/backoff so a dead upstream doesn't produce thousands of
  503-failure log lines per node per hour.

Related: `todo/monitoring/ntfy-topic-out-of-repo.md` (move the hardcoded
`NTFY_TOPIC` out of the repo) — do that alongside if the webhook is being
touched.
