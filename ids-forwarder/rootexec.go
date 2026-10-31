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
