// Halstead difficulty calculator for Go source files.
//
// Usage: go run scripts/metrics/halstead.go <dir-or-file>...
//
// Prints one JSON array to stdout; each element is {"file": ..., "difficulty": ...}
// for a .go file (test files excluded). Operator/operand classification follows
// the standard token-level approximation: identifiers and literals are
// operands; keywords, operators, and punctuation are operators.
package main

import (
	"encoding/json"
	"fmt"
	"go/scanner"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

type result struct {
	File       string  `json:"file"`
	Difficulty float64 `json:"difficulty"`
}

type tally struct {
	ops   map[string]int // operators
	ands  map[string]int // operands
}

func (t *tally) count() float64 {
	n1 := len(t.ops)
	n2 := len(t.ands)
	N2 := 0
	for _, v := range t.ands {
		N2 += v
	}
	if n2 == 0 {
		return 0
	}
	return float64(n1) / 2 * float64(N2) / float64(n2)
}

func isOperand(tok token.Token) bool {
	switch tok {
	case token.IDENT, token.INT, token.FLOAT, token.IMAG, token.CHAR, token.STRING:
		return true
	}
	return false
}

func measure(path string) (float64, error) {
	src, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	fset := token.NewFileSet()
	var s scanner.Scanner
	file := fset.AddFile(path, fset.Base(), len(src))
	s.Init(file, src, nil, 0)
	t := &tally{ops: map[string]int{}, ands: map[string]int{}}
	for {
		pos, tok, lit := s.Scan()
		if tok == token.EOF {
			break
		}
		_ = pos
		text := lit
		if text == "" {
			text = tok.String()
		}
		if isOperand(tok) {
			t.ands[text]++
		} else {
			t.ops[text]++
		}
	}
	return t.count(), nil
}

func walk(out *[]result, root string) error {
	return filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			if info.Name() == "vendor" || info.Name() == "testdata" || info.Name() == "tmp" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		d, err := measure(path)
		if err != nil {
			return err
		}
		*out = append(*out, result{File: path, Difficulty: d})
		return nil
	})
}

func main() {
	var out []result
	for _, arg := range os.Args[1:] {
		info, err := os.Stat(arg)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		if info.IsDir() {
			if err := walk(&out, arg); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
		} else {
			d, err := measure(arg)
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
			out = append(out, result{File: arg, Difficulty: d})
		}
	}
	enc, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println(string(enc))
}
