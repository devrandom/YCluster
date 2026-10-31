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
	PID       uint32       `json:"pid"`
	StartTime string       `json:"start_time"`
	Pod       *tetragonPod `json:"pod"`
}

type tetragonProc struct {
	Binary    string       `json:"binary"`
	Arguments string       `json:"arguments"`
	UID       uint32       `json:"uid"`
	PID       uint32       `json:"pid"`
	StartTime string       `json:"start_time"`
	Pod       *tetragonPod `json:"pod"`
}

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
