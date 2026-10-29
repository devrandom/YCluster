// Package sigma turns Tetragon JSON events into the flat, Sysmon-like
// fields used by the SigmaHQ Linux rules.
package sigma

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// Event is the normalized subset the Linux process_creation and
// network_connection rules reference. Field names deliberately match the
// Sigma rules so the RSigma pipeline only has to add the event-type guard.
type Event struct {
	Timestamp         string `json:"@timestamp,omitempty"`
	EventType         string `json:"EventType"`
	Image             string `json:"Image,omitempty"`
	CommandLine       string `json:"CommandLine,omitempty"`
	ParentImage       string `json:"ParentImage,omitempty"`
	ParentCommandLine string `json:"ParentCommandLine,omitempty"`
	User              string `json:"User,omitempty"`
	ProcessId         uint32 `json:"ProcessId,omitempty"`
	ParentProcessId   uint32 `json:"ParentProcessId,omitempty"`
	ContainerName     string `json:"ContainerName,omitempty"`
	ContainerImage    string `json:"ContainerImage,omitempty"`
	StartTime         string `json:"StartTime,omitempty"`
	DestinationIp     string `json:"DestinationIp,omitempty"`
	DestinationPort   uint16 `json:"DestinationPort,omitempty"`
}

type envelope struct {
	Time   string    `json:"time"`
	Exec   *tetragon `json:"process_exec"`
	Kprobe *tetragon `json:"process_kprobe"`
}

type tetragon struct {
	FunctionName string          `json:"function_name"`
	Process      process         `json:"process"`
	Args         json.RawMessage `json:"args"`
}

type process struct {
	PID       uint32   `json:"pid"`
	UID       uint32   `json:"uid"`
	Binary    string   `json:"binary"`
	Arguments string   `json:"arguments"`
	StartTime string   `json:"start_time"`
	Pod       *pod     `json:"pod"`
	Parent    *process `json:"parent"`
}

type pod struct {
	Namespace string     `json:"namespace"`
	Name      string     `json:"name"`
	Container *container `json:"container"`
}

type container struct {
	Name  string `json:"name"`
	Image *image `json:"image"`
}

type image struct {
	Name string `json:"name"`
}

type socketArg struct {
	SockArg *socket `json:"sock_arg"`
}

type socket struct {
	DAddr string `json:"daddr"`
	DPort uint16 `json:"dport"`
}

// NormalizeLine parses one Tetragon JSON line. ok is false for event types
// the Sigma process/network rules do not consume; callers should skip those
// without treating them as errors.
func NormalizeLine(line string) (*Event, bool, error) {
	var env envelope
	if err := json.Unmarshal([]byte(line), &env); err != nil {
		return nil, false, fmt.Errorf("parse tetragon event: %w", err)
	}
	switch {
	case env.Exec != nil:
		return normalizeProcess(env.Time, "process_exec", &env.Exec.Process), true, nil
	case env.Kprobe != nil && env.Kprobe.FunctionName == "tcp_connect":
		ev := normalizeProcess(env.Time, "network_connection", &env.Kprobe.Process)
		if addr, port, ok := destination(env.Kprobe.Args); ok {
			ev.DestinationIp = addr
			ev.DestinationPort = port
		}
		return ev, true, nil
	default:
		return nil, false, nil
	}
}

func normalizeProcess(ts, eventType string, p *process) *Event {
	ev := &Event{
		Timestamp:   ts,
		EventType:   eventType,
		Image:       p.Binary,
		CommandLine: commandLine(p.Binary, p.Arguments),
		User:        user(p.UID),
		ProcessId:   p.PID,
		StartTime:   p.StartTime,
	}
	if p.Parent != nil {
		ev.ParentImage = p.Parent.Binary
		ev.ParentCommandLine = commandLine(p.Parent.Binary, p.Parent.Arguments)
		ev.ParentProcessId = p.Parent.PID
	}
	if p.Pod != nil {
		ev.ContainerName = p.Pod.Name
		if p.Pod.Container != nil {
			if p.Pod.Container.Name != "" {
				ev.ContainerName = p.Pod.Container.Name
			}
			if p.Pod.Container.Image != nil {
				ev.ContainerImage = p.Pod.Container.Image.Name
			}
		}
	}
	return ev
}

func commandLine(binary, args string) string {
	binary = strings.TrimSpace(binary)
	args = strings.TrimSpace(args)
	switch {
	case binary == "":
		return args
	case args == "":
		return binary
	default:
		return binary + " " + args
	}
}

func user(uid uint32) string {
	if uid == 0 {
		return "root"
	}
	return strconv.FormatUint(uint64(uid), 10)
}

func destination(raw json.RawMessage) (string, uint16, bool) {
	if len(raw) == 0 || string(raw) == "null" {
		return "", 0, false
	}
	var args []socketArg
	if err := json.Unmarshal(raw, &args); err != nil {
		return "", 0, false
	}
	for _, arg := range args {
		if arg.SockArg != nil && arg.SockArg.DAddr != "" {
			return arg.SockArg.DAddr, arg.SockArg.DPort, true
		}
	}
	return "", 0, false
}
