package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// detectionRecord is the durable, human-readable form of one detection.
// It is deliberately flat and stable: the same fields the Alertmanager
// payload carries, minus the routing metadata. Anything that later wants
// to read these (jq, a digest job, a log store) depends on this shape.
type detectionRecord struct {
	Time     string `json:"time"`
	Host     string `json:"host"`
	Source   string `json:"source"`
	RuleID   string `json:"rule_id"`
	Severity string `json:"severity"`
	Layer    string `json:"layer"`
	Summary  string `json:"summary"`
}

func newDetectionRecord(a alert) detectionRecord {
	return detectionRecord{
		Time:     a.eventTimestamp().UTC().Format(time.RFC3339Nano),
		Host:     a.Labels["host"],
		Source:   a.Labels["source"],
		RuleID:   a.Labels["rule_id"],
		Severity: a.Labels["severity"],
		Layer:    a.Labels["layer"],
		Summary:  a.Annotations["summary"],
	}
}

// detectionLog appends one JSON line per detection and rotates by size so
// the file cannot grow without bound on a node with no log shipping.
//
// It is a record, not the paging path: a write failure is counted and
// logged rather than stalling the pipeline, because a full disk must not
// also stop coverage alerts from reaching Alertmanager.
type detectionLog struct {
	path     string
	maxBytes int64
	keep     int

	mu   sync.Mutex
	file *os.File
	size int64
}

func openDetectionLog(path string, maxBytes int64, keep int) (*detectionLog, error) {
	if path == "" {
		return nil, nil
	}
	d := &detectionLog{path: path, maxBytes: maxBytes, keep: keep}
	if err := d.open(); err != nil {
		return nil, err
	}
	return d, nil
}

func (d *detectionLog) open() error {
	if err := os.MkdirAll(filepath.Dir(d.path), 0o750); err != nil {
		return fmt.Errorf("create detections directory: %w", err)
	}
	f, err := os.OpenFile(d.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		return fmt.Errorf("open detections file: %w", err)
	}
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return fmt.Errorf("stat detections file: %w", err)
	}
	d.file = f
	d.size = info.Size()
	return nil
}

func (d *detectionLog) append(rec detectionRecord) error {
	line, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("encode detection: %w", err)
	}
	line = append(line, '\n')

	d.mu.Lock()
	defer d.mu.Unlock()
	if d.file == nil {
		if err := d.open(); err != nil {
			return err
		}
	}
	if d.maxBytes > 0 && d.size+int64(len(line)) > d.maxBytes {
		if err := d.rotate(); err != nil {
			return err
		}
	}
	if _, err := d.file.Write(line); err != nil {
		return fmt.Errorf("write detection: %w", err)
	}
	d.size += int64(len(line))
	return nil
}

// rotate renames the live file aside and opens a fresh one. Older files
// beyond the keep count are removed so a noisy node cannot fill the disk.
func (d *detectionLog) rotate() error {
	if err := d.file.Close(); err != nil {
		return fmt.Errorf("close detections file: %w", err)
	}
	d.file = nil
	if d.keep > 0 {
		oldest := fmt.Sprintf("%s.%d", d.path, d.keep)
		if err := os.Remove(oldest); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove old detections file: %w", err)
		}
	}
	for i := d.keep - 1; i >= 1; i-- {
		from := fmt.Sprintf("%s.%d", d.path, i)
		to := fmt.Sprintf("%s.%d", d.path, i+1)
		if err := os.Rename(from, to); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("rotate detections file: %w", err)
		}
	}
	if err := os.Rename(d.path, d.path+".1"); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("rotate detections file: %w", err)
	}
	return d.open()
}

func (d *detectionLog) close() error {
	if d == nil {
		return nil
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.file == nil {
		return nil
	}
	err := d.file.Close()
	d.file = nil
	return err
}
