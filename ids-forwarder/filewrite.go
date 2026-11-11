package main

import (
	"encoding/json"
	"regexp"
	"strconv"
	"strings"
)

var verifyrestorePathRe = regexp.MustCompile(`^/(?:var/lib|etc)/postgresql/([0-9]+)/verifyrestore/`)

type tetragonFileArg struct {
	FileArg *tetragonFile `json:"file_arg"`
}

type tetragonFile struct {
	Path string `json:"path"`
}

func routineFileWriteReason(body *tetragonLog) string {
	if body == nil ||
		body.PolicyName != "exec-and-sensitive-file-writes" ||
		body.FunctionName != "security_file_permission" {
		return ""
	}
	if body.Process.Binary != "/usr/local/bin/ycluster" ||
		!strings.Contains(body.Process.Arguments, "wg reconcile --up") {
		if reason := backupVerifyReason(body); reason != "" {
			return reason
		}
		if reason := blackboxTargetsRewrite(body); reason != "" {
			return reason
		}
		if reason := zulipPushHostsWrite(body); reason != "" {
			return reason
		}
		return daemonConfigWrite(body)
	}
	path, ok := fileArgPath(body.Args)
	if ok && path == "/etc/wireguard/wg0.conf" {
		return "expected_file_write"
	}
	return ""
}

// backupVerifyReason covers setup-backup-verify.yml. It builds a throwaway
// Postgres cluster under <data>/<version>/verifyrestore, restores the nightly
// dump into it, checks it and drops it again on every storage node. The
// scratch directory is named exactly once, so match it rather than the
// binaries that happen to write there.
func backupVerifyReason(body *tetragonLog) string {
	filePath, ok := fileArgPath(body.Args)
	if !ok {
		return ""
	}
	match := verifyrestorePathRe.FindStringSubmatch(filePath)
	if match == nil {
		return ""
	}
	switch body.Process.Binary {
	case "/bin/install", "/usr/bin/install":
		// pg_createcluster's verifyrestore drill installs the copied cluster
		// files with the system install binary; Debian's PATH resolves it to
		// /usr/bin/install, and usrmerge systems also expose /bin/install.
		return "backup_verify"
	}
	if body.Process.Binary != "/usr/bin/pg_createcluster" {
		return ""
	}
	fields := strings.Fields(body.Process.Arguments)
	for i, field := range fields {
		if field != "verifyrestore" || i == 0 {
			continue
		}
		if _, err := strconv.Atoi(fields[i-1]); err == nil && fields[i-1] == match[1] {
			return "backup_verify"
		}
	}
	return ""
}

// blackboxTargetsRewrite suppresses the temp-file half of a managed
// Prometheus file rewrite: the collector writes /etc/prometheus/tmp<random>
// and renames it over the real target. The rename lands on the target path
// and stays alertable, so the write to the temp name adds no signal.
func blackboxTargetsRewrite(body *tetragonLog) string {
	if body.Process.Binary != "/usr/bin/python3" ||
		!invokesScript(body.Process.Arguments, "/usr/local/lib/cluster/update-blackbox-targets") {
		return ""
	}
	path, ok := fileArgPath(body.Args)
	if ok && strings.HasPrefix(path, "/etc/prometheus/tmp") {
		return "expected_file_write"
	}
	return ""
}

// zulipPushHostsWrite suppresses the managed egress-allowlist update: the
// helper writes a temp file, reads it back through cat and renames it over
// /etc/zulip/push-hosts. Any other writer touching that path stays alertable.
func zulipPushHostsWrite(body *tetragonLog) string {
	filePath, ok := fileArgPath(body.Args)
	if !ok {
		return ""
	}
	switch body.Process.Binary {
	case "/usr/local/lib/cluster/setup-zulip-egress":
		// The helper writes /etc/zulip/push-hosts.<random> and renames it
		// over the target.
		if filePath == "/etc/zulip/push-hosts" ||
			strings.HasPrefix(filePath, "/etc/zulip/push-hosts.") {
			return "expected_file_write"
		}
	case "/usr/bin/cat", "/bin/cat":
		if filePath == "/etc/zulip/push-hosts" &&
			strings.HasPrefix(strings.TrimSpace(body.Process.Arguments), "/etc/zulip/push-hosts.") {
			return "expected_file_write"
		}
	}
	return ""
}

// daemonConfigWrite covers writers that persist a fixed config file as their
// normal operation: the PCP collectors take a lock file per sample, and
// netfilter-persistent saves the running ruleset. Any other writer touching
// these paths stays alertable.
func daemonConfigWrite(body *tetragonLog) string {
	filePath, ok := fileArgPath(body.Args)
	if !ok {
		return ""
	}
	if body.Process.Binary == "/usr/sbin/cupsd" &&
		strings.HasPrefix(filePath, "/etc/cups/") {
		return "expected_file_write"
	}
	switch filePath {
	case "/etc/pcp/pmie/lock":
		if body.Process.Binary == "/usr/bin/pmiectl" {
			return "expected_file_write"
		}
	case "/etc/pcp/pmlogger/lock":
		if body.Process.Binary == "/usr/bin/pmlogctl" {
			return "expected_file_write"
		}
	case "/etc/iptables/rules.v4":
		if body.Process.Binary == "/usr/sbin/iptables-save" {
			return "expected_file_write"
		}
	}
	return ""
}

func fileArgPath(raw json.RawMessage) (string, bool) {
	if len(raw) == 0 || string(raw) == "null" {
		return "", false
	}
	var args []tetragonFileArg
	if err := json.Unmarshal(raw, &args); err != nil {
		return "", false
	}
	for _, arg := range args {
		if arg.FileArg != nil && arg.FileArg.Path != "" {
			return arg.FileArg.Path, true
		}
	}
	return "", false
}
