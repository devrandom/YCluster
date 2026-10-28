// ids-sigma-bridge is a spike harness: it converts Tetragon JSON lines to
// the normalized NDJSON consumed by RSigma. It is intentionally separate
// from ids-forwarder until the pilot proves the rule layer.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"ycluster.local/ids-forwarder/sigma"
)

func main() {
	input := flag.String("input", "", "Tetragon JSON-lines file (default stdin)")
	output := flag.String("output", "", "normalized NDJSON file (default stdout)")
	flag.Parse()

	in := os.Stdin
	if *input != "" {
		f, err := os.Open(*input)
		if err != nil {
			fmt.Fprintf(os.Stderr, "ids-sigma-bridge: open input: %v\n", err)
			os.Exit(1)
		}
		defer f.Close()
		in = f
	}
	out := os.Stdout
	if *output != "" {
		f, err := os.Create(*output)
		if err != nil {
			fmt.Fprintf(os.Stderr, "ids-sigma-bridge: create output: %v\n", err)
			os.Exit(1)
		}
		defer f.Close()
		out = f
	}

	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 64*1024), 4<<20)
	enc := json.NewEncoder(out)
	for scanner.Scan() {
		ev, ok, err := sigma.NormalizeLine(scanner.Text())
		if err != nil {
			fmt.Fprintf(os.Stderr, "ids-sigma-bridge: %v\n", err)
			os.Exit(1)
		}
		if !ok {
			continue
		}
		if err := enc.Encode(ev); err != nil {
			fmt.Fprintf(os.Stderr, "ids-sigma-bridge: write output: %v\n", err)
			os.Exit(1)
		}
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "ids-sigma-bridge: read input: %v\n", err)
		os.Exit(1)
	}
}
