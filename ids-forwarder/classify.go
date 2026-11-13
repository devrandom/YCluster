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

// nonRootInvoker reports whether the identity that ran a privilege tool was
// unprivileged, which is what makes its exec a transition rather than root
// doing root things. The parent's uid is the invoker: the tool itself is
// already root when the event is written. When the parent is gone, the login
// session of the event process is the fallback, and an unresolvable invoker
// stays evidence rather than paging on a guess.
func nonRootInvoker(process tetragonProcess, parent *tetragonProc) bool {
	if parent != nil && parent.PID != 0 {
		return parent.UID != 0
	}
	return nonRootSession(process.PID, process.StartTime)
}

// nonRootSession reports a login session owned by an account other than root.
// It is the fallback for an invoker that has left /proc and the only evidence
// available on the Sigma path.
func nonRootSession(pid uint32, startTime string) bool {
	o := processOriginFor(pid, startTime)
	return o.Origin == "interactive" && o.Actor != "" && o.Actor != "root"
}
