package main

import (
	"bufio"
	"compress/gzip"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// scanSource reads one retained file. The held live file is read through
// the caller's descriptor so a rename during replay still yields the same
// inode; everything else is opened by path.
func scanSource(src replaySource, held *os.File, fn func(string) error) (int64, error) {
	if src.held && held != nil {
		if _, err := held.Seek(0, io.SeekStart); err != nil {
			return 0, err
		}
		return scanLines(held, fn)
	}
	rc, err := openRetained(src.path)
	if err != nil {
		return 0, err
	}
	if rc == nil {
		// The entry vanished between listing and opening. Tetragon
		// compresses a rotated file in place, and retention can delete
		// the oldest backup; neither is a coverage gap on its own. The
		// next pass sees the new name, and a real loss still surfaces
		// when the cursor boundary cannot be found.
		return 0, nil
	}
	defer rc.Close()
	return scanLines(rc, fn)
}

// openRetained opens one retained file. When Tetragon compresses a
// rotated file it renames "<name>.log" to "<name>.log.gz", so the
// uncompressed name listed a moment earlier may already be gone.
func openRetained(path string) (io.ReadCloser, error) {
	rc, err := openLogFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		rc, err = openLogFile(path + ".gz")
	}
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	return rc, err
}

// scanLines streams complete lines and reports how many bytes they
// occupy. A trailing partial line is left unconsumed so the live tailer
// can finish it once Tetragon writes the newline.
func scanLines(r io.Reader, fn func(string) error) (int64, error) {
	br := bufio.NewReaderSize(r, 256*1024)
	var consumed int64
	for {
		line, err := br.ReadString('\n')
		if strings.HasSuffix(line, "\n") {
			consumed += int64(len(line))
			if trimmed := strings.TrimSpace(line); trimmed != "" {
				if ferr := fn(trimmed); ferr != nil {
					return consumed, ferr
				}
			}
		}
		if err == io.EOF {
			return consumed, nil
		}
		if err != nil {
			return consumed, err
		}
	}
}

func openLogFile(path string) (io.ReadCloser, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	if strings.HasSuffix(path, ".gz") {
		gz, err := gzip.NewReader(f)
		if err != nil {
			f.Close()
			return nil, err
		}
		return &compoundCloser{Reader: gz, closers: []io.Closer{gz, f}}, nil
	}
	return f, nil
}

type compoundCloser struct {
	io.Reader
	closers []io.Closer
}

func (c *compoundCloser) Close() error {
	var first error
	for _, closer := range c.closers {
		if err := closer.Close(); err != nil && first == nil {
			first = err
		}
	}
	return first
}

type logFile struct {
	path string
	mod  time.Time
	dev  uint64
	ino  uint64
}

// tetragonLogFiles lists the live file plus its compressed backups in
// rotation order. The filter is by name and type only: an mtime check
// could skip a backup that still holds post-cursor events.
func tetragonLogFiles(path string) ([]logFile, error) {
	dir := filepath.Dir(path)
	base := strings.TrimSuffix(filepath.Base(path), ".log")
	matches, err := filepath.Glob(filepath.Join(dir, base+"*"))
	if err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	var files []logFile
	for _, match := range matches {
		if match != path {
			name := filepath.Base(match)
			if !(strings.HasSuffix(name, ".log") || strings.HasSuffix(name, ".log.gz") || strings.HasSuffix(name, ".gz")) {
				continue
			}
		}
		info, err := os.Stat(match)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		seen[match] = true
		dev, ino := fileIdentity(info)
		files = append(files, logFile{path: match, mod: info.ModTime(), dev: dev, ino: ino})
	}
	if !seen[path] {
		if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() {
			dev, ino := fileIdentity(info)
			files = append(files, logFile{path: path, mod: info.ModTime(), dev: dev, ino: ino})
		}
	}
	sort.Slice(files, func(i, j int) bool {
		if !files[i].mod.Equal(files[j].mod) {
			return files[i].mod.Before(files[j].mod)
		}
		return files[i].path < files[j].path
	})
	return files, nil
}

// replaySources orders the live file and its backups. If the file was
// rotated between opening it and listing the directory, the held inode is
// inserted first: it predates the new current file.
func (f *forwarder) replaySources(held *os.File) []replaySource {
	files, err := tetragonLogFiles(f.cfg.tetragonLog)
	if err != nil {
		f.cfg.logf("list tetragon logs: %v", err)
	}
	heldDev, heldIno, haveHeld := heldIdentity(held)
	sources := make([]replaySource, 0, len(files)+1)
	found := false
	for _, file := range files {
		src := replaySource{path: file.path, dev: file.dev, ino: file.ino}
		if haveHeld && !found && file.dev == heldDev && file.ino == heldIno {
			src.held = true
			found = true
		}
		sources = append(sources, src)
	}
	if haveHeld && !found {
		sources = append([]replaySource{{
			path: f.cfg.tetragonLog,
			dev:  heldDev,
			ino:  heldIno,
			held: true,
		}}, sources...)
	}
	return sources
}

func heldIdentity(held *os.File) (uint64, uint64, bool) {
	if held == nil {
		return 0, 0, false
	}
	info, err := held.Stat()
	if err != nil {
		return 0, 0, false
	}
	dev, ino := fileIdentity(info)
	return dev, ino, dev != 0 || ino != 0
}

// handoffOffset returns how far the live tailer may skip into the held
// file. An unknown identity falls back to the file's current size.
func handoffOffset(consumed map[[2]uint64]int64, held *os.File) int64 {
	dev, ino, ok := heldIdentity(held)
	if ok {
		if offset, seen := consumed[[2]uint64{dev, ino}]; seen {
			return offset
		}
	}
	return openFileSize(held)
}
