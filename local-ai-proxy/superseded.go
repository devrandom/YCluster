package main

import "fmt"

// SupersededError is returned when a request names a model the
// operator has marked obsolete. It names the configured replacement so
// clients (and humans reading logs) can migrate.
type SupersededError struct {
	Model        string
	SupersededBy string
}

func (e *SupersededError) Error() string {
	return fmt.Sprintf("model %q has been superseded by %q", e.Model, e.SupersededBy)
}
