package main

import "strings"

// rootExecUserPath reports whether path lives in a tree that non-root users
// can write and that package management never installs into. /tmp and
// /dev/shm are excluded because the upstream Sigma rules already cover any
// execution from those directories.
//
// This is a local rule: SigmaHQ has no upstream counterpart. It encodes the
// cluster invariant that root does not execute payloads staged in an
// operator's home or the persistent temporary directory.
func rootExecUserPath(path string) bool {
	switch {
	case strings.HasPrefix(path, "/home/"):
		return true
	case strings.HasPrefix(path, "/var/tmp/"):
		return true
	}
	return false
}

// worldWritableCwd reports a working directory inside a tree any local
// account can write to. It is the only working directory worth putting on an
// alert: a service runs from / or from its unit's WorkingDirectory, so a
// payload started from a staging directory is the shape triage wants to see.
func worldWritableCwd(dir string) bool {
	for _, prefix := range []string{"/tmp", "/var/tmp", "/dev/shm", "/run/user"} {
		if dir == prefix || strings.HasPrefix(dir, prefix+"/") {
			return true
		}
	}
	return false
}
