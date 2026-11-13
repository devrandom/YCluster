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
	Binary    string       `json:"binary"`
	Arguments string       `json:"arguments"`
	UID       uint32       `json:"uid"`
	Auid      uint32       `json:"auid"`
	PID       uint32       `json:"pid"`
	StartTime string       `json:"start_time"`
	Pod       *tetragonPod `json:"pod"`
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
	Binary    string           `json:"binary"`
	Arguments string           `json:"arguments"`
	UID       uint32           `json:"uid"`
	Auid      uint32           `json:"auid"`
	PID       uint32           `json:"pid"`
	StartTime string           `json:"start_time"`
	Pod       *tetragonPod     `json:"pod"`
	NS        *eventNamespaces `json:"ns"`
	Docker    string           `json:"docker"`
}

// eventNamespaces is what --enable-process-ns adds to every event: the pid and
// mount namespace inums with the kernel's own verdict on whether each is the
// host's. A zero inum means the field is absent, which is what a replayed
// pre-flag line looks like.
type eventNamespaces struct {
	Pid tetragonNamespace `json:"pid"`
	Mnt tetragonNamespace `json:"mnt"`
}

type tetragonNamespace struct {
	Inum   uint32 `json:"inum"`
	IsHost bool   `json:"is_host"`
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
