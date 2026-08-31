package main

// BFF entrypoint. Responsibilities: IAP identity extraction, SSE relay to
// Agent Engine. Anyone past IAP has access to all registry projects. No tool
// logic, no credentials other than the service account's own Google auth.

import (
	"context"
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"os"
	"strings"
)

//go:embed static
var staticFS embed.FS

const iapEmailHeader = "X-Goog-Authenticated-User-Email"

// identity extracts the IAP-verified user email. Invariants:
//
//   - run.invoker is held by the IAP service agent alone.
//   - Ingress is open; IAP is the gate, not the network.
//   - X-Goog-Authenticated-User-Email is set by Google, never by the client.
//
// WS_DEV_USER bypasses IAP for local development.
func identity(r *http.Request) (string, error) {
	if dev := os.Getenv("WS_DEV_USER"); dev != "" {
		return dev, nil
	}
	v := r.Header.Get(iapEmailHeader)
	// IAP format: "accounts.google.com:user@example.com"
	if _, email, ok := strings.Cut(v, ":"); ok && email != "" {
		return email, nil
	}
	return "", fmt.Errorf("no IAP identity")
}

type server struct {
	cfg     *Config
	clients map[string]*AgentClient // engine name -> client
}

func (s *server) handleChat(w http.ResponseWriter, r *http.Request) {
	email, err := identity(r)
	if err != nil {
		http.Error(w, "unauthenticated", http.StatusUnauthorized)
		return
	}
	projectIDs := s.cfg.Registry.AllProjectIDs()
	if len(projectIDs) == 0 {
		http.Error(w, "registry has no projects", http.StatusInternalServerError)
		return
	}
	var body struct {
		Message string   `json:"message"`
		Conv    string   `json:"conv"`
		Anchors []Anchor `json:"anchors"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Message == "" {
		http.Error(w, "message required", http.StatusBadRequest)
		return
	}
	if len(body.Anchors) > 12 {
		body.Anchors = body.Anchors[:12]
	}
	for i, a := range body.Anchors {
		if len(a.Name) > 100 {
			body.Anchors[i].Name = a.Name[:100]
		}
		if len(a.URL) > 500 {
			body.Anchors[i].URL = a.URL[:500]
		}
	}

	engines := s.cfg.EnginesFor(projectIDs)
	if len(engines) == 0 {
		http.Error(w, "no engine serves these projects", http.StatusInternalServerError)
		return
	}
	// Single engine today; multi-engine fan-out is a later change here only.
	client := s.clients[engines[0].Name]

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	flusher, _ := w.(http.Flusher)
	emit := func(payload any) {
		data, _ := json.Marshal(payload)
		fmt.Fprintf(w, "data: %s\n\n", data)
		if flusher != nil {
			flusher.Flush()
		}
	}
	send := func(text string) { emit(map[string]string{"text": text}) }
	tools := func(names []string) { emit(map[string][]string{"tools": names}) }
	records := func(rs []Anchor) { emit(map[string][]Anchor{"records": rs}) }
	if err := client.StreamQuery(r.Context(), email, body.Conv, projectIDs, body.Anchors, body.Message, send, tools, records); err != nil {
		log.Printf("stream_query user=%s: %v", email, err)
		data, _ := json.Marshal(map[string]string{"error": "query failed"})
		fmt.Fprintf(w, "data: %s\n\n", data)
	}
	fmt.Fprint(w, "data: [DONE]\n\n")
}

func (s *server) handleMe(w http.ResponseWriter, r *http.Request) {
	email, err := identity(r)
	if err != nil {
		http.Error(w, "unauthenticated", http.StatusUnauthorized)
		return
	}
	ids := s.cfg.Registry.AllProjectIDs()
	names := make([]string, 0, len(ids))
	for _, p := range s.cfg.Registry.Projects {
		names = append(names, p.Name)
	}
	w.Header().Set("Content-Type", "application/json")
	anchors := s.cfg.Registry.AnchorsFor(ids)
	if err := json.NewEncoder(w).Encode(map[string]any{"email": email, "projects": names, "anchors": anchors}); err != nil {
		log.Printf("encode /api/me: %v", err)
	}
}

func main() {
	ctx := context.Background()
	cfg, err := LoadConfig()
	if err != nil {
		log.Fatal(err)
	}
	clients := make(map[string]*AgentClient, len(cfg.Engines))
	for _, e := range cfg.Engines {
		c, err := NewAgentClient(ctx, e)
		if err != nil {
			log.Fatalf("engine %s: %v", e.Name, err)
		}
		clients[e.Name] = c
	}
	s := &server{cfg: cfg, clients: clients}

	static, _ := fs.Sub(staticFS, "static")
	mux := http.NewServeMux()
	mux.Handle("GET /", http.FileServer(http.FS(static)))
	mux.HandleFunc("GET /api/me", s.handleMe)
	mux.HandleFunc("POST /api/chat", s.handleChat)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	log.Printf("listening on :%s (%d engines, %d projects)",
		port, len(cfg.Engines), len(cfg.Registry.Projects))
	log.Fatal(http.ListenAndServe(":"+port, mux))
}
