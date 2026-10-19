"""
Settings for every phase.

config.toml (project root) holds all tunable settings and is safe to commit.
.env holds secrets (API keys) only and must never be committed.
Anything missing from config.toml falls back to the defaults below, so an old or partial config still works.
"""
from __future__ import annotations

import copy
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # python-dotenv missing: environment variables still work
    pass

DEFAULTS: dict = {
    "pipeline": {"profile": "example", "data_dir": "data"},
    "gates": {"hard_gate_pct": 40.0, "score_gate_pct": 60.0, "hard_weight": 0.70, "preferred_weight": 0.30,
              "partial_credit": 0.5, "variant_tie_pct": 3.0, "min_hard_for_approval": 3, "min_desc_chars": 300, "ground_min_share": 0.5},
    "analysis": {"max_requirements": 15, "max_desc_chars": 9000, "max_attempts": 3, "context_warn_tokens": 14000},
    "drafting": {"max_tailored_bullets": 10, "local_attempts": 3, "cloud_attempts": 2,
                 "min_retention": 0.65, "max_growth": 1.4},
    "local_llm": {"provider": "lmstudio", "base_url": "", "api_key": "local", "model": "",
                  "temperature": 0, "max_tokens": 6144, "prompt_suffix": "", "timeout_s": 900, "model_profiles": []},
    "cloud": {"temperature": 0.3, "providers": []},
}


# Usual addresses of local model servers (all speak the OpenAI-compatible API)
PROVIDER_URLS = {"lmstudio": "http://localhost:1234/v1", "ollama": "http://localhost:11434/v1",
                 "llamacpp": "http://localhost:8080/v1", "mlx": "http://localhost:8080/v1",
                 "jan": "http://localhost:1337/v1", "localai": "http://localhost:8080/v1",
                 "vllm": "http://localhost:8000/v1", "other": "http://localhost:8080/v1"}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Path | None = None) -> dict:
    path = path or Path(os.getenv("PIPELINE_CONFIG", ROOT / "config.toml"))
    cfg = copy.deepcopy(DEFAULTS)
    # config.toml (shared, committed) and then config.local.toml (yours only, never committed) on top of it
    for p in (path, path.with_name("config.local.toml")):
        if p.exists():
            try:
                with open(p, "rb") as f:
                    _merge(cfg, tomllib.load(f))
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"{p.name} has a typing mistake: {e}\nFix that line and run again.")
    lc = cfg["local_llm"]
    lc["provider"] = str(lc.get("provider") or "lmstudio").strip().lower()
    if not str(lc.get("base_url") or "").strip():      # empty = the provider's usual address
        lc["base_url"] = PROVIDER_URLS.get(lc["provider"], PROVIDER_URLS["lmstudio"])
    env_url = (os.getenv("LOCAL_LLM_BASE_URL") or os.getenv("LM_STUDIO_BASE_URL") or "").strip()
    if env_url:
        lc["base_url"] = env_url
    return cfg


CFG = load_config()
DATA_DIR = ROOT / CFG["pipeline"]["data_dir"]
PROFILES_DIR = ROOT / "profiles"


def profile_dir(name: str | None = None) -> Path:
    return PROFILES_DIR / (name or os.getenv("PIPELINE_PROFILE") or CFG["pipeline"]["profile"])


def model_profile(model_name: str) -> dict:
    """Settings for the loaded local model: the [local_llm] defaults plus the first matching model_profiles entry."""
    base = {k: v for k, v in CFG["local_llm"].items() if k != "model_profiles"}
    for p in CFG["local_llm"].get("model_profiles", []):
        if p.get("match") and re.search(p["match"], model_name or "", re.I):
            return {**base, **{k: v for k, v in p.items() if k != "match"}}
    return base
