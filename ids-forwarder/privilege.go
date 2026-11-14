package main

import (
	"encoding/json"
	"strconv"
	"strings"
)

// privilegeTransition renders one privilege-raises hook event as the change it
// observed: which set*id call ran, or how the capability sets moved. The raw
// argument list is a bitmap dump that is unreadable in a notification and
// makes every event a distinct dedup signature.
func privilegeTransition(function string, raw json.RawMessage) string {
	if len(raw) == 0 || string(raw) == "null" {
		return ""
	}
	var args []map[string]json.RawMessage
	if err := json.Unmarshal(raw, &args); err != nil {
		return ""
	}
	var ids, effective []string
	for _, arg := range args {
		for key, value := range arg {
			switch key {
			case "int_arg":
				var n int64
				if json.Unmarshal(value, &n) == nil {
					ids = append(ids, strconv.FormatInt(n, 10))
				}
			case "cap_effective_arg":
				if s, ok := capValue(value); ok {
					effective = append(effective, s)
				}
			}
		}
	}
	var parts []string
	if len(ids) > 0 {
		// The hook name already says which id it sets: __sys_setuid is
		// setuid, __sys_setresgid is setresgid.
		parts = append(parts, strings.TrimPrefix(function, "__sys_")+"("+strings.Join(ids, ",")+")")
	}
	if len(effective) == 2 {
		parts = append(parts, "caps effective "+effective[0]+"->"+effective[1])
	}
	return strings.Join(parts, " ")
}

// capValue renders a kernel capability mask as the shortest hex that still
// shows which bit moved: 0x0->0x400 is readable, sixteen hex digits are not.
func capValue(raw json.RawMessage) (string, bool) {
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return "", false
	}
	s = strings.TrimLeft(strings.ToLower(s), "0")
	if s == "" {
		return "0x0", true
	}
	return "0x" + s, true
}
