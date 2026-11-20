package main

import (
	"net/netip"
	"strings"
)

// The threat model treats host root as out of scope: a root attacker stops or
// rewrites the sensor first, so what the sensor records afterwards is a trail,
// not coverage. Every detection is classified once, in the enqueue path, and
// the class decides delivery. See docs/design/ids-coverage-and-evidence.md.
const (
	deliveryEvidence = "evidence"
	deliverySilent   = "silent"
	deliveryPage     = "page"
)

// publicDestination reports whether a destination address is an ordinary
// internet address: not loopback, not link-local, not a private range. It is
// the address half of the tenant-egress rule, for the paths where only an
// address string survives, as in a Sigma detection.
func publicDestination(raw string) bool {
	addr, err := netip.ParseAddr(strings.TrimSpace(raw))
	if err != nil {
		return false
	}
	addr = addr.Unmap()
	return !addr.IsLoopback() && !addr.IsUnspecified() && !addr.IsLinkLocalUnicast() && !addr.IsPrivate()
}

// containerEgress reports whether a detection describes a container's own
// connection to an ordinary internet address, which is the traffic a live
// connect already drops as tenant_egress.
func containerEgress(destination, layer string) bool {
	if layer != "container" && layer != "unverified" {
		return false
	}
	return publicDestination(destination)
}

// sigmaContainerClass separates a rule hit on the container's own internet
// connection, which is the tenant's traffic and never pages, from one that
// reaches into the cluster, which keeps the container page.
func sigmaContainerClass(a alert) string {
	if a.Labels["egress"] == "public" {
		return "tenant_egress"
	}
	return "sigma_nonroot"
}

// pagingClasses are the coverage classes allowed to page today. Everything
// else is recorded and counted as a page candidate, and never posted, until a
// shadow measurement earns it a page.
var pagingClasses = map[string]bool{
	"escalation_nonroot": true,
	"sigma_nonroot":      true,
}

// knownClasses is every class classifyAlert can return. The metrics endpoint
// emits a page-candidate and an evidence series for each at zero, so a quiet
// class reads as zero rather than as absent: absent is also what a binary that
// does not know the class looks like, and that difference is what the
// measurement gate depends on.
var knownClasses = []string{
	"audit",
	"container_other",
	"crowdsec",
	"escalation_nonroot",
	"exec_nonroot",
	"file_nonroot",
	"host_nonroot",
	"lateral_container",
	"lateral_nonroot",
	"root_connect",
	"root_exec",
	"root_file_write",
	"root_module",
	"root_other",
	"root_privilege",
	"root_sigma",
	"sigma_nonroot",
	"tenant_egress",
}

// classifyAlert returns the threat-model class of a detection and whether it
// is evidence, meaning recorded and never delivered.
func classifyAlert(a alert) (class string, evidence bool) {
	rule := a.Labels["rule_id"]
	if rule == "container-api" || rule == "container-exec" || strings.HasPrefix(rule, "vm-bridge-") {
		// The audit trail: root-admin activity by definition, but the only
		// record of who entered a container and when.
		return "audit", false
	}
	// The transition out of a non-root identity is the one point where the
	// sensor still sees the attacker without host root.
	if a.Labels["escalation"] == "nonroot" {
		return "escalation_nonroot", false
	}
	if a.Labels["source"] == sourceSigma {
		switch a.Labels["layer"] {
		case "container", "unverified":
			return sigmaContainerClass(a), false
		}
		if isRootIdentity(a) {
			return "root_sigma", true
		}
		return "sigma_nonroot", false
	}
	if a.Labels["source"] == sourceCrowdSec {
		return "crowdsec", false
	}

	kind := a.Labels["kind"]
	switch a.Labels["layer"] {
	case "container", "unverified":
		// Only connects reach the classifier from a container: the mapping
		// drops every other container event as guest-internal, and the Sigma
		// layer returns above. A connect is the tenant-boundary signal.
		if kind == "connect" {
			return "lateral_container", false
		}
		return "container_other", false
	}
	if isRootIdentity(a) {
		switch kind {
		case "exec":
			return "root_exec", true
		case "connect":
			return "root_connect", true
		case "privilege":
			return "root_privilege", true
		case "module":
			return "root_module", true
		case "write":
			return "root_file_write", true
		}
		return "root_other", true
	}
	switch kind {
	case "exec":
		return "exec_nonroot", false
	case "connect":
		return "lateral_nonroot", false
	case "write":
		return "file_nonroot", false
	case "privilege":
		// A capability gain without uid 0: still an attempt to cross the
		// boundary, so it belongs with the transition class.
		return "escalation_nonroot", false
	}
	return "host_nonroot", false
}

// deliveryFor reports how a classified detection is delivered. Evidence is
// dropped after the record is written; coverage is either paged now or
// recorded while its rate is measured.
func deliveryFor(a alert, class string, evidence bool) string {
	if name := a.Labels["alertname"]; name != "" && name != "IDSAlert" {
		// The forwarder's own coverage alerts (a replay gap) are not detection
		// findings: they are routed on severity by Alertmanager and nothing
		// may decide they are routine. This test comes before the evidence
		// one so a coverage alert cannot be classed out of paging by whatever
		// identity labels its event happens to carry.
		return deliveryPage
	}
	if evidence {
		return deliveryEvidence
	}
	if pageEligible(a, class) {
		return deliveryPage
	}
	return deliverySilent
}

// pageEligible is the whole paging policy. Classes are enabled here, one at a
// time, once their shadow count has been measured.
func pageEligible(a alert, class string) bool {
	if !pagingClasses[class] {
		return false
	}
	if class == "sigma_nonroot" {
		// The engine's own level travels separately because low and medium
		// both map to the cluster's warning. Only medium and above page.
		return sigmaLevelAtLeast(a.Labels["sigma_level"], "medium")
	}
	return true
}

func sigmaLevelAtLeast(level, floor string) bool {
	rank := map[string]int{"informational": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
	have, ok := rank[strings.ToLower(strings.TrimSpace(level))]
	if !ok {
		return false
	}
	return have >= rank[floor]
}

// isRootIdentity reports positive proof of uid 0. A missing uid is not proof,
// so the event stays coverage instead of silently becoming evidence.
func isRootIdentity(a alert) bool {
	uid, ok := a.Labels["uid"]
	return ok && uid == "0"
}

// routineRootHelper lists privileged helpers that unprivileged processes run
// on a normal host and that give root to a fixed program rather than an
// arbitrary one: rootless container id mapping, FUSE mounts, session bus
// activation, host-based SSH auth, polkit prompting and the account tools.
// Their exec is not a boundary crossing worth a page; the escalation class
// covers everything else by exact binary path. A helper added to the host
// without being added here pages the first time it runs, which is the
// direction to err in.
var routineRootHelper = map[string]bool{
	"/usr/bin/chfn":        true,
	"/usr/bin/chsh":        true,
	"/usr/bin/fusermount3": true,
	"/usr/bin/gpasswd":     true,
	"/usr/bin/mount":       true,
	"/usr/bin/newgidmap":   true,
	"/usr/bin/newgrp":      true,
	"/usr/bin/newuidmap":   true,
	// CUDA setup where the toolkit is installed: creates the device nodes
	// a non-root process cannot. Setuid root, and rare.
	"/usr/bin/nvidia-modprobe":                    true,
	"/usr/bin/passwd":                             true,
	"/usr/bin/umount":                             true,
	"/usr/lib/dbus-1.0/dbus-daemon-launch-helper": true,
	"/usr/lib/openssh/ssh-keysign":                true,
	"/usr/lib/polkit-1/polkit-agent-helper-1":     true,
	"/usr/sbin/mount.cifs":                        true,
	"/usr/sbin/mount.nfs":                         true,
	"/usr/sbin/unix_chkpwd":                       true,
}

// parentUID reports the uid of the process that started the event. The event
// carries the parent block when Tetragon still had it, and every event names
// the parent exec, so a parent that has since exited still answers from the
// cache the forwarder built while reading the stream. Neither path reads
// /proc, so the answer does not depend on the invoker still running.
func parentUID(body *tetragonLog) (uint32, bool) {
	if body == nil {
		return 0, false
	}
	if p := body.Parent; p != nil && p.PID != 0 {
		return p.UID, true
	}
	if e, ok := execLookup(body.Process.ParentExecID); ok {
		return e.uid, true
	}
	return 0, false
}

// privilegeTools are the binaries whose exec is the transition itself. The
// paths are exact because a copied binary must not inherit the class.
var privilegeTools = map[string]bool{
	"/usr/bin/sudo":         true,
	"/bin/sudo":             true,
	"/usr/bin/su":           true,
	"/bin/su":               true,
	"/usr/sbin/su":          true,
	"/usr/bin/doas":         true,
	"/usr/local/bin/doas":   true,
	"/usr/bin/pkexec":       true,
	"/usr/local/bin/pkexec": true,
}

// escalationSource reports where the proof that a privilege tool was run by
// an unprivileged identity came from, or "" when there is no proof. The
// sources are ordered by how well they survive the process exiting: the
// parent uid is in the event, the parent exec is in the event stream the
// forwarder already read, the audit uid is in the event, and the login
// session is the only one that needs a live /proc.
func escalationSource(body *tetragonLog) string {
	if p := body.Parent; p != nil && p.PID != 0 {
		// The invoker has to be an unprivileged host identity. An idmapped
		// uid is a container account, which is a tenant boundary, not a host
		// one, and must not turn into a page.
		if nonRootHostUID(p.UID) {
			return "uid"
		}
		// A root parent is a positive answer: root running a privilege tool
		// is not a transition, whatever the audit trail says.
		return ""
	}
	// The parent block can be absent (a replayed line, a trimmed export)
	// while the event still names the parent exec. The cache was filled when
	// that exec was read, so its uid answers without /proc.
	if e, ok := execLookup(body.Process.ParentExecID); ok {
		if nonRootHostUID(e.uid) {
			return "execid"
		}
		return ""
	}
	if body.Process.Auid != auidUnset && body.Process.Auid >= loginUIDBase && body.Process.Auid < incusIDMapBase {
		return "auid"
	}
	if nonRootSession(body.Process.PID, body.Process.StartTime) {
		return "session"
	}
	return ""
}

// nonRootHostUID reports an unprivileged host identity. An idmapped uid is a
// container account, which is a tenant boundary, not a host one.
func nonRootHostUID(uid uint32) bool {
	return uid != 0 && uid < incusIDMapBase
}

// nonRootSession reports a login session owned by an account other than root.
// It is the fallback for an invoker that has left /proc and the only evidence
// available on the Sigma path.
func nonRootSession(pid uint32, startTime string) bool {
	o := processOriginFor(pid, startTime)
	return o.Origin == "interactive" && o.Actor != "" && o.Actor != "root"
}

// rootInvoker reports a positively identified root parent. An absent or
// non-root invoker is not root, so an exemption that depends on it must not
// apply: the argv is attacker-controlled.
func rootInvoker(parent *tetragonProc) bool {
	return parent != nil && parent.PID != 0 && parent.UID == 0
}

// tenantUnverified reports an unverified layer that a cgroup actually
// explained: a user-scope runtime or a pid-namespace sandbox outside any
// runtime root. That is a tenant, not an undecided host process.
func tenantUnverified(source string) bool {
	return source == "cgroup" || source == "namespace+cgroup"
}
