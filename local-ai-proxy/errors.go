package main

import (
	"encoding/json"
	"net/http"
)

// openAIError is the standard OpenAI error envelope. Matches
// https://platform.openai.com/docs/guides/error-codes/api-errors.
type openAIError struct {
	Error openAIErrorDetail `json:"error"`
}

type openAIErrorDetail struct {
	Message string `json:"message"`
	Type    string `json:"type"`
	Code    string `json:"code,omitempty"`
	Param   string `json:"param,omitempty"`
}

// writeOpenAIError emits a JSON error response in the OpenAI shape.
// Headers must not have been written yet — callers bail out before
// WriteHeader on upstream failures.
func writeOpenAIError(w http.ResponseWriter, status int, errType, message string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(openAIError{
		Error: openAIErrorDetail{
			Message: message,
			Type:    errType,
		},
	})
}
