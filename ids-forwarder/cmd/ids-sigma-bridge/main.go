// ids-sigma-bridge is a spike harness: it converts Tetragon JSON lines to
// the normalized NDJSON consumed by RSigma. It is intentionally separate
// from ids-forwarder until the pilot proves the rule layer.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"ycluster.local/ids-forwarder/sigma"
)

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "ids-sigma-bridge: %v\n", err)
		os.Exit(1)
	}
}

// run parses the flags, opens the streams and converts the input. Splitting
// it from main keeps the flag and file handling testable.
func run(args []string, stdin io.Reader, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("ids-sigma-bridge", flag.ContinueOnError)
	flags.SetOutput(stderr)
	input := flags.String("input", "", "Tetragon JSON-lines file (default stdin)")
	output := flags.String("output", "", "normalized NDJSON file (default stdout)")
	if err := flags.Parse(args); err != nil {
		return err
	}
	in, closeIn, err := openInput(*input, stdin)
	if err != nil {
		return fmt.Errorf("open input: %w", err)
	}
	defer closeIn()
	out, closeOut, err := openOutput(*output, stdout)
	if err != nil {
		return fmt.Errorf("create output: %w", err)
	}
	defer closeOut()
	return bridge(in, out)
}

// openInput opens path for reading, or returns fallback when path is empty.
func openInput(path string, fallback io.Reader) (io.Reader, func(), error) {
	if path == "" {
		return fallback, func() {}, nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}
	return f, func() { _ = f.Close() }, nil
}

// openOutput creates path, or returns fallback when path is empty.
func openOutput(path string, fallback io.Writer) (io.Writer, func(), error) {
	if path == "" {
		return fallback, func() {}, nil
	}
	f, err := os.Create(path)
	if err != nil {
		return nil, nil, err
	}
	return f, func() { _ = f.Close() }, nil
}

// bridge converts every Tetragon line to normalized NDJSON. Lines the
// normalizer skips produce no output; a malformed line aborts the run.
func bridge(in io.Reader, out io.Writer) error {
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 64*1024), 4<<20)
	enc := json.NewEncoder(out)
	for scanner.Scan() {
		ev, ok, err := sigma.NormalizeLine(scanner.Text())
		if err != nil {
			return err
		}
		if !ok {
			continue
		}
		if err := enc.Encode(ev); err != nil {
			return fmt.Errorf("write output: %w", err)
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("read input: %w", err)
	}
	return nil
}
