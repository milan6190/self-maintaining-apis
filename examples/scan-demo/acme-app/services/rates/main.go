package main

import (
	"encoding/json"
	"net/http"
)

// Rates are cached hourly. See https://pkg.go.dev/net/http
func latestRates() (map[string]float64, error) {
	resp, err := http.Get("https://api.fxrates-provider.net/v1/latest?base=USD")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out struct{ Rates map[string]float64 `json:"rates"` }
	return out.Rates, json.NewDecoder(resp.Body).Decode(&out)
}
