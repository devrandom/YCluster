package main

import (
	"os"
	"syscall"
)

func fileIdentity(info os.FileInfo) (uint64, uint64) {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, 0
	}
	return st.Dev, st.Ino
}

func sameIdentity(dev, ino uint64, info os.FileInfo) bool {
	d, i := fileIdentity(info)
	return d == dev && i == ino && d != 0
}
