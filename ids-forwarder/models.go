package main

import "encoding/json"

// tetragonEnvelope is the subset of Tetragon's JSON event envelope the
// forwarder needs. Exactly one well-known payload key is present per event
// (process_exec, process_kprobe, ...); envelope fields ride alongside.
type tetragonEnvelope struct {
	NodeName string           `json:"node_name"`
	Time     string           `json:"time"`
	Exec     *tetragonLog     `json:"process_exec"`
	Kprobe   *tetragonLog     `json:"process_kprobe"`
	Exit     *tetragonExit    `json:"process_exit"`
	Connect  *tetragonConnect `json:"process_connect"`
}

type tetragonLog struct {
	PolicyName   string          `json:"policy_name"`
	FunctionName string          `json:"function_name"`
	Message      string          `json:"message"`
	Process      tetragonProcess `json:"process"`
	// Parent is a sibling of process in every Tetragon event payload, not a
	// field of process. Parsing it from inside process silently drops it.
	Parent *tetragonProc   `json:"parent"`
	Args   json.RawMessage `json:"args"`
}

type tetragonExit struct {
	Process tetragonProcess `json:"process"`
	Status  int             `json:"status"`
}

type tetragonConnect struct {
	Process tetragonProcess `json:"process"`
}

type tetragonProcess struct {
	Binary    string `json:"binary"`
	Arguments string `json:"arguments"`
	// Cwd is the working directory at exec time. It is rendered from the
	// exec-time cache, so it answers for a process that has already exited;
	// a staging directory here changes what the exec means.
	Cwd       string `json:"cwd"`
	UID       uint32 `json:"uid"`
	Auid      uint32 `json:"auid"`
	PID       uint32 `json:"pid"`
	StartTime string `json:"start_time"`
	// ExecID names this exec in Tetragon's process cache and ParentExecID
	// names the exec that created it. Together they make ancestry
	// addressable from the event stream, without a /proc read and without
	// depending on the parent still running.
	ExecID       string       `json:"exec_id"`
	ParentExecID string       `json:"parent_exec_id"`
	Pod          *tetragonPod `json:"pod"`
	// NS is the namespace snapshot Tetragon renders from its exec-time
	// process cache. It survives the process exiting, so it is the
	// trustworthy layer source.
	NS *eventNamespaces `json:"ns"`
	// Docker is the container id Tetragon records for a process started by
	// the Docker runtime. It is present on the event itself, so attribution
	// survives a process that exited before the line was read.
	Docker string `json:"docker"`
}

type tetragonProc struct {
	Binary       string           `json:"binary"`
	Arguments    string           `json:"arguments"`
	Cwd          string           `json:"cwd"`
	UID          uint32           `json:"uid"`
	Auid         uint32           `json:"auid"`
	PID          uint32           `json:"pid"`
	StartTime    string           `json:"start_time"`
	ExecID       string           `json:"exec_id"`
	ParentExecID string           `json:"parent_exec_id"`
	Pod          *tetragonPod     `json:"pod"`
	NS           *eventNamespaces `json:"ns"`
	Docker       string           `json:"docker"`
}

// eventNamespaces is what --enable-process-ns adds to every event: namespace
// inums with the kernel's own verdict on whether each is the host's. Only the
// ones a decision reads are decoded: pid and mnt answer the layer question and
// net separates a tenant that shares the host network from one behind its own.
// A zero inum means the field is absent, which is what a replayed pre-flag line
// looks like.
//
// The user namespace is deliberately not decoded. It cannot stand in for the
// layer question: a privileged container shares the host user namespace, and a
// host daemon under systemd's PrivateUsers= has one of its own, so neither
// verdict separates tenants from host processes.
type eventNamespaces struct {
	Pid tetragonNamespace `json:"pid"`
	Mnt tetragonNamespace `json:"mnt"`
	Net tetragonNamespace `json:"net"`
}

type tetragonNamespace struct {
	Inum   uint32 `json:"inum"`
	IsHost bool   `json:"is_host"`
}

// hostNetwork reports a process that shares the host network namespace. The
// snapshot has to be present: a missing field is not a verdict, and "false" is
// the safe default because it keeps a container's traffic on the container
// path.
func hostNetwork(ns *eventNamespaces) bool {
	return ns != nil && ns.Net.Inum != 0 && ns.Net.IsHost
}

// auidUnset is the kernel's "no login uid" value, reported for anything
// systemd starts.
const auidUnset = ^uint32(0)

type tetragonPod struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

// crowdsecAlert mirrors the subset of CrowdSec 1.8.x pkg/models.Alert that
// the notification-http plugin posts ({{.|toJson}}).
type crowdsecAlert struct {
	Scenario string           `json:"scenario"`
	Machine  *crowdsecMachine `json:"machine"`
	Source   *crowdsecSource  `json:"source"`
	Events   []crowdsecEvent  `json:"events"`
}

type crowdsecMachine struct {
	Hostname string `json:"hostname"`
}

type crowdsecSource struct {
	IP    string `json:"ip"`
	Range string `json:"range"`
}

type crowdsecEvent struct {
	Message string `json:"message"`
}
