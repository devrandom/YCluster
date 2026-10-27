package main

import (
	"errors"
	"os"
	"time"
)

// replayAfterBoundary skips up to and including the cursor line, then
// forwards every later line in the same pass.
func (f *forwarder) replayAfterBoundary(sources []replaySource, held *os.File, hash string) (map[[2]uint64]int64, bool, bool, error) {
	consumed := map[[2]uint64]int64{}
	within := false
	unreadable := false
	found := false
	for _, src := range sources {
		start := src
		n, err := scanSource(start, held, func(line string) error {
			_, env, _ := eventType(line)
			if env == nil {
				return nil
			}
			if !within {
				if lineHash(line) != hash {
					return nil
				}
				within = true
				found = true
				return nil
			}
			return f.handleLine(line)
		})
		consumed[[2]uint64{start.dev, start.ino}] = n
		if err != nil {
			if isSpoolError(err) {
				return consumed, found, unreadable, err
			}
			unreadable = true
			f.cfg.logf("replay %s: %v", start.path, err)
			continue
		}
	}
	return consumed, found, unreadable, nil
}

// replayAll forwards every retained line newer than floor. A zero floor
// means no filter.
func (f *forwarder) replayAll(sources []replaySource, held *os.File, floor time.Time) (map[[2]uint64]int64, bool, error) {
	consumed := map[[2]uint64]int64{}
	unreadable := false
	for _, src := range sources {
		start := src
		n, err := scanSource(start, held, func(line string) error {
			_, env, _ := eventType(line)
			if env == nil {
				return nil
			}
			if !floor.IsZero() && parseEventMeta(line, env).time.Before(floor) {
				return nil
			}
			return f.handleLine(line)
		})
		consumed[[2]uint64{start.dev, start.ino}] = n
		if err != nil {
			if isSpoolError(err) {
				return consumed, unreadable, err
			}
			unreadable = true
			f.cfg.logf("replay %s: %v", start.path, err)
		}
	}
	return consumed, unreadable, nil
}

// isSpoolError distinguishes a failed durable write (fatal) from a bad
// or unreadable input file (logged and reported as a gap).
func isSpoolError(err error) bool {
	var se *spoolError
	return errors.As(err, &se)
}
