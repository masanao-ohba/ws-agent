package main

// Agent Engine REST client.
// AdkApp call convention: {"class_method": ..., "input": {...}} against
// :query / :streamQuery. Sessions are cached per user and validated on reuse
// (state.project_ids must match current membership); stale sessions (empty
// stream or 404) drop the cache and retry once.

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
)

func oauth2Client(ts oauth2.TokenSource) *http.Client {
	return oauth2.NewClient(context.Background(), ts)
}

type AgentClient struct {
	engine Engine
	http   *http.Client

	mu       sync.Mutex
	sessions map[string]string // email/conv -> session id
}

func NewAgentClient(ctx context.Context, engine Engine) (*AgentClient, error) {
	ts, err := google.DefaultTokenSource(ctx, "https://www.googleapis.com/auth/cloud-platform")
	if err != nil {
		return nil, err
	}
	return &AgentClient{
		engine:   engine,
		http:     oauth2Client(ts),
		sessions: map[string]string{},
	}, nil
}

func (c *AgentClient) baseURL() string {
	return fmt.Sprintf("https://%s-aiplatform.googleapis.com/v1/%s",
		c.engine.Region, c.engine.ResourceName)
}

func (c *AgentClient) call(ctx context.Context, method, classMethod string, input map[string]any) (*http.Response, error) {
	body, _ := json.Marshal(map[string]any{"class_method": classMethod, "input": input})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		c.baseURL()+":"+method, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	return c.http.Do(req)
}

// Session returns a session id for one browser conversation (email + client
// conversation id): a reload starts fresh, messages within a page share
// context. Reused sessions are validated against current membership.
func (c *AgentClient) Session(ctx context.Context, email, convID string, projectIDs []string, anchors []Anchor) (string, error) {
	key := email + "/" + convID
	c.mu.Lock()
	cached, ok := c.sessions[key]
	c.mu.Unlock()
	if ok {
		if c.sessionStateValid(ctx, email, cached, projectIDs) {
			return cached, nil
		}
		c.dropSession(key)
	}
	// The in-memory map is per-instance; another Cloud Run instance may
	// already own this conversation. Look it up before creating.
	if id := c.findSession(ctx, email, convID, projectIDs); id != "" {
		c.mu.Lock()
		c.sessions[key] = id
		c.mu.Unlock()
		return id, nil
	}
	resp, err := c.call(ctx, "query", "create_session", map[string]any{
		"user_id": email,
		"state": map[string]any{
			"user_email":  email,
			"project_ids": projectIDs,
			"locale":      "ja",
			"conv_id":     convID,
			"anchors":     anchors,
		},
	})
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var out struct {
		Output struct {
			ID string `json:"id"`
		} `json:"output"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil || out.Output.ID == "" {
		return "", fmt.Errorf("create_session: %v (http %d)", err, resp.StatusCode)
	}
	c.mu.Lock()
	c.sessions[key] = out.Output.ID
	c.mu.Unlock()
	return out.Output.ID, nil
}

// findSession locates an existing session for this conversation via the
// Sessions API, so any instance converges on the same session.
func (c *AgentClient) findSession(ctx context.Context, email, convID string, projectIDs []string) string {
	resp, err := c.call(ctx, "query", "list_sessions", map[string]any{"user_id": email})
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	var out struct {
		Output struct {
			Sessions []struct {
				ID    string `json:"id"`
				State struct {
					ConvID     string   `json:"conv_id"`
					ProjectIDs []string `json:"project_ids"`
				} `json:"state"`
			} `json:"sessions"`
		} `json:"output"`
	}
	if json.NewDecoder(resp.Body).Decode(&out) != nil {
		return ""
	}
	for _, s := range out.Output.Sessions {
		if s.State.ConvID == convID && equalStrings(s.State.ProjectIDs, projectIDs) {
			return s.ID
		}
	}
	return ""
}

func (c *AgentClient) sessionStateValid(ctx context.Context, email, sessionID string, projectIDs []string) bool {
	resp, err := c.call(ctx, "query", "get_session", map[string]any{
		"user_id": email, "session_id": sessionID,
	})
	if err != nil || resp.StatusCode != http.StatusOK {
		if resp != nil {
			resp.Body.Close()
		}
		return false
	}
	defer resp.Body.Close()
	var out struct {
		Output struct {
			State struct {
				ProjectIDs []string `json:"project_ids"`
			} `json:"state"`
		} `json:"output"`
	}
	if json.NewDecoder(resp.Body).Decode(&out) != nil {
		return false
	}
	return equalStrings(out.Output.State.ProjectIDs, projectIDs)
}

func (c *AgentClient) dropSession(key string) {
	c.mu.Lock()
	delete(c.sessions, key)
	c.mu.Unlock()
}

// StreamQuery sends one message and emits the model's reply to onText; the
// tool names of each retrieval round go to onTools instead.
// A stale session (404 or empty model stream) is dropped and retried once.
func (c *AgentClient) StreamQuery(ctx context.Context, email, convID string, projectIDs []string, anchors []Anchor, message string, onText func(string), onTools func([]string), onRecords func([]Anchor)) error {
	for attempt := 0; attempt < 2; attempt++ {
		sessionID, err := c.Session(ctx, email, convID, projectIDs, anchors)
		if err != nil {
			return err
		}
		got, err := c.streamOnce(ctx, email, sessionID, message, onText, onTools, onRecords)
		if err != nil {
			return err
		}
		if got {
			return nil
		}
		c.dropSession(email + "/" + convID) // stale: retry with a fresh session
	}
	return fmt.Errorf("empty model stream after retry")
}

func (c *AgentClient) streamOnce(ctx context.Context, email, sessionID, message string, onText func(string), onTools func([]string), onRecords func([]Anchor)) (bool, error) {
	resp, err := c.call(ctx, "streamQuery", "stream_query", map[string]any{
		"user_id": email, "session_id": sessionID, "message": message,
		"run_config": map[string]any{"streaming_mode": "sse"},
	})
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return false, nil // stale session
	}
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return false, fmt.Errorf("stream_query: http %d: %s", resp.StatusCode, b)
	}
	gotText := false
	sawPartial := false
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		line = strings.TrimPrefix(line, "data: ")
		var ev struct {
			Partial bool `json:"partial"`
			Content struct {
				Role  string `json:"role"`
				Parts []struct {
					Text         string `json:"text"`
					FunctionCall struct {
						Name string `json:"name"`
					} `json:"functionCall"`
					FunctionCallS struct {
						Name string `json:"name"`
					} `json:"function_call"`
					FunctionResponse  json.RawMessage `json:"functionResponse"`
					FunctionResponseS json.RawMessage `json:"function_response"`
				} `json:"parts"`
			} `json:"content"`
		}
		if json.Unmarshal([]byte(line), &ev) != nil {
			continue
		}
		// Tool results carry the records actually read; surface them so the
		// reply can be checked against what was retrieved. Only reads, since
		// a search listing is candidates, not sources.
		if ev.Content.Role != "model" {
			var records []Anchor
			for _, p := range ev.Content.Parts {
				raw := p.FunctionResponse
				if len(raw) == 0 {
					raw = p.FunctionResponseS
				}
				if len(raw) == 0 {
					continue
				}
				var fr struct {
					Name     string `json:"name"`
					Response struct {
						Items []struct {
							URL   string `json:"url"`
							Title string `json:"title"`
						} `json:"items"`
					} `json:"response"`
				}
				if json.Unmarshal(raw, &fr) != nil || strings.HasPrefix(fr.Name, "search") {
					continue
				}
				for _, it := range fr.Response.Items {
					if it.URL != "" {
						records = append(records, Anchor{Name: it.Title, URL: it.URL})
					}
				}
			}
			if len(records) > 0 {
				onRecords(records)
			}
			continue
		}
		var tools []string
		for _, p := range ev.Content.Parts {
			if n := p.FunctionCall.Name + p.FunctionCallS.Name; n != "" {
				tools = append(tools, n)
			}
		}
		// A tool round starts: what streamed so far was narration, not the
		// reply — the frontend clears it on this signal.
		if len(tools) > 0 {
			sawPartial = false
			onTools(tools)
			continue
		}
		for _, p := range ev.Content.Parts {
			if p.Text == "" {
				continue
			}
			if ev.Partial {
				sawPartial = true
				gotText = true
				onText(p.Text)
			} else if !sawPartial {
				// Non-partial text without preceding chunks (non-streaming
				// fallback); with chunks it is the aggregate duplicate.
				gotText = true
				onText(p.Text)
			}
		}
	}
	return gotText, scanner.Err()
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	seen := map[string]bool{}
	for _, x := range a {
		seen[x] = true
	}
	for _, x := range b {
		if !seen[x] {
			return false
		}
	}
	return true
}
