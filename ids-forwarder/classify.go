package main

import (
	"strconv"
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

// pagingClasses are the coverage classes allowed to page today. Everything
// else is delivered silent and counted as a page candidate until a shadow
// measurement earns it a page.
var pagingClasses = map[string]bool{
	"escalation_nonroot": true,
	"sigma_nonroot":      true,
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
			// The curated rules are the only path that sees inside a
			// container, and the tenant boundary is in scope whatever the
			// guest uid.
			return "sigma_nonroot", false
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
		// A container process whose host-visible uid is unprivileged is a
		// guest account. Below the idmap base it is host root wearing a
		// tenant name, so host-integration rules see it as root. Connects
		// stay a tenant-boundary signal either way.
		if isPrivilegedContainer(a) && kind != "connect" {
			return "privileged_container", true
		}
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
// delivered silent while its rate is measured.
func deliveryFor(a alert, class string, evidence bool) string {
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

// isPrivilegedContainer reports a container process whose host-visible uid is
// below the idmap base, which is Incus's or the kernel's root for a runtime
// without user namespaces.
func isPrivilegedContainer(a alert) bool {
	if a.Labels["layer"] != "container" {
		// An unverified process may be a rootless container's or a host
		// account's; either way it is not host root, and a login uid is
		// below the idmap base by accident.
		return false
	}
	uid, ok := a.Labels["uid"]
	if !ok {
		return false
	}
	value, err := strconv.ParseUint(uid, 10, 32)
	if err != nil {
		return false
	}
	return value < incusIDMapBase
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
// parent uid is in the event, the audit uid is in the event, and the login
// session needs a live /proc.
func escalationSource(body *tetragonLog) string {
	if p := body.Parent; p != nil && p.PID != 0 {
		// The invoker has to be an unprivileged host identity. An idmapped
		// uid is a container account, which is a tenant boundary, not a host
		// one, and must not turn into a page.
		if p.UID != 0 && p.UID < incusIDMapBase {
			return "uid"
		}
		// A root parent is a positive answer: root running a privilege tool
		// is not a transition, whatever the audit trail says.
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
