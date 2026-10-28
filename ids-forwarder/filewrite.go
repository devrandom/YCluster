package main

import (
	"encoding/json"
	"path"
	"strings"
)

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
		if reason := blackboxTargetsRewrite(body); reason != "" {
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

// blackboxTargetsRewrite suppresses the temp-file half of a managed
// Prometheus file rewrite: the collector writes /etc/prometheus/tmp<random>
// and renames it over the real target. The rename lands on the target path
// and stays alertable, so the write to the temp name adds no signal.
func blackboxTargetsRewrite(body *tetragonLog) string {
	if body.Process.Binary != "/usr/bin/python3" ||
		!strings.Contains(body.Process.Arguments, "/usr/local/lib/cluster/update-blackbox-targets") {
		return ""
	}
	path, ok := fileArgPath(body.Args)
	if ok && strings.HasPrefix(path, "/etc/prometheus/tmp") {
		return "expected_file_write"
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
	switch filePath {
	case "/etc/pcp/pmie/lock":
		if path.Base(body.Process.Binary) == "pmiectl" {
			return "expected_file_write"
		}
	case "/etc/pcp/pmlogger/lock":
		if path.Base(body.Process.Binary) == "pmlogctl" {
			return "expected_file_write"
		}
	case "/etc/iptables/rules.v4":
		if path.Base(body.Process.Binary) == "iptables-save" {
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
