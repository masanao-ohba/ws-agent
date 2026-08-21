package main

// Registry mirrors config/projects.yaml (rendered to WS_PROJECTS by deploy).
// The BFF reads only what it needs: membership for project resolution.
// Engines is the future fan-out seam: currently exactly one entry.

import (
	"encoding/json"
	"fmt"
	"os"
)

type Anchor struct {
	Name string `json:"name"`
	URL  string `json:"url"`
}

type Project struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	Members []string `json:"members"`
	Anchors []Anchor `json:"anchors"`
}

type Registry struct {
	Projects []Project `json:"projects"`
}

// AnchorsFor returns the default anchors across the user's projects.
func (r *Registry) AnchorsFor(projectIDs []string) []Anchor {
	var out []Anchor
	for _, p := range r.Projects {
		if contains(projectIDs, p.ID) {
			out = append(out, p.Anchors...)
		}
	}
	return out
}

// ProjectIDsFor returns the projects the user belongs to. Empty = no access.
func (r *Registry) ProjectIDsFor(email string) []string {
	var ids []string
	for _, p := range r.Projects {
		for _, m := range p.Members {
			if m == email {
				ids = append(ids, p.ID)
				break
			}
		}
	}
	return ids
}

// Engine is one deployed Agent Engine. When GCP projects split per product,
// multiple entries appear here and the BFF fans out queries per engine.
type Engine struct {
	Name         string   `json:"name"`
	ResourceName string   `json:"resource_name"` // projects/.../reasoningEngines/...
	Region       string   `json:"region"`
	ProjectIDs   []string `json:"project_ids"` // registry project ids served by this engine
}

type Config struct {
	Registry Registry
	Engines  []Engine
}

func LoadConfig() (*Config, error) {
	var cfg Config
	if raw := os.Getenv("WS_PROJECTS"); raw != "" {
		if err := json.Unmarshal([]byte(raw), &cfg.Registry); err != nil {
			return nil, fmt.Errorf("WS_PROJECTS: %w", err)
		}
	} else {
		return nil, fmt.Errorf("WS_PROJECTS is not set")
	}
	if raw := os.Getenv("WS_ENGINES"); raw != "" {
		if err := json.Unmarshal([]byte(raw), &cfg.Engines); err != nil {
			return nil, fmt.Errorf("WS_ENGINES: %w", err)
		}
	}
	if len(cfg.Engines) == 0 {
		return nil, fmt.Errorf("WS_ENGINES is not set or empty")
	}
	return &cfg, nil
}

// EnginesFor selects the engines serving any of the user's projects.
// Currently always the single engine. Future: real fan-out.
func (c *Config) EnginesFor(projectIDs []string) []Engine {
	var out []Engine
	for _, e := range c.Engines {
		for _, pid := range e.ProjectIDs {
			if contains(projectIDs, pid) {
				out = append(out, e)
				break
			}
		}
	}
	return out
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
