"""Deploy the agent to Vertex AI Agent Engine.

The project registry (config/projects.yaml) is validated and rendered
into the WS_PROJECTS env var.

Usage:
    uv run python scripts/deploy.py --env dev [--dry-run] [--resource-name ...]
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PACKAGE_ROOT.parent / "config"

DISPLAY_NAME = "ws-agent"

REQUIREMENTS = [
    "google-cloud-aiplatform[adk,agent_engines]",
    "google-adk==1.23.0",
    # new enough for the thinking config used in agent.py
    "google-genai>=1.75.0",
    "google-cloud-secret-manager>=2.20.0",
    "httpx>=0.27.0",
    "pydantic>=2.7.0",
]


def deploy_target(env: str) -> dict[str, str]:
    """GCP project / staging bucket / region for one env, from config."""
    path = CONFIG_DIR / f"deploy.{env}.yaml"
    if not path.exists():
        raise SystemExit(f"no deploy target for env '{env}': {path} not found")
    return dict(yaml.safe_load(path.read_text()))


def config_path(env: str) -> Path:
    """Per-env registry: projects.<env>.yaml if present, else projects.yaml."""
    per_env = CONFIG_DIR / f"projects.{env}.yaml"
    return per_env if per_env.exists() else CONFIG_DIR / "projects.yaml"


def render_registry(env: str) -> str:
    """Validate the registry through the runtime schema and return JSON."""
    raw = yaml.safe_load(config_path(env).read_text())
    sys.path.insert(0, str(PACKAGE_ROOT))
    from agents.wsagent.config import Registry

    return Registry.model_validate(raw).model_dump_json(exclude_none=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resource-name", default=None)
    args = parser.parse_args()
    target = deploy_target(args.env)

    # GOOGLE_CLOUD_PROJECT is reserved on Agent Engine (platform-injected),
    # which is exactly what secrets.py reads — so it is not passed here.
    env_vars = {
        "WS_PROJECTS": render_registry(args.env),
    }
    # OAuth client id for GWS token refresh (non-secret; the secret is a
    # Secret Manager name inside the file). Optional per env.
    oauth_client = CONFIG_DIR / f"oauth_client.{args.env}.json"
    if oauth_client.exists():
        env_vars["WS_GOOGLE_OAUTH_CLIENT"] = json.dumps(json.loads(oauth_client.read_text()))
    if args.dry_run:
        print(
            f"[dry-run] display_name={DISPLAY_NAME} region={target['region']} env={args.env}"
        )
        print(f"[dry-run] registry: {len(json.loads(env_vars['WS_PROJECTS'])['projects'])} projects")
        return

    # extra_packages is cwd-relative: pin cwd to the package root.
    os.chdir(PACKAGE_ROOT)
    os.environ.update(env_vars)

    import vertexai
    from vertexai import agent_engines
    from vertexai.preview import reasoning_engines

    from agents.wsagent.agent import root_agent

    vertexai.init(
        project=target["project"],
        location=target["region"],
        staging_bucket=target["staging_bucket"],
    )
    app = reasoning_engines.AdkApp(agent=root_agent, enable_tracing=True)

    existing = list(agent_engines.list(filter=f'display_name="{DISPLAY_NAME}"'))
    common: dict[str, Any] = {
        "agent_engine": app,
        "display_name": DISPLAY_NAME,
        "requirements": REQUIREMENTS,
        "extra_packages": ["agents"],
        "env_vars": env_vars,
    }
    if args.resource_name or len(existing) == 1:
        resource = args.resource_name or existing[0].resource_name
        engine = agent_engines.update(resource_name=resource, **common)
    elif not existing:
        engine = agent_engines.create(**common)
    else:
        raise SystemExit(
            f"multiple engines named {DISPLAY_NAME}; pass --resource-name to disambiguate"
        )
    print(engine.resource_name)


if __name__ == "__main__":
    main()
