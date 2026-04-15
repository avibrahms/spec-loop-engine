from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .schemas import SPEC_SCHEMA
from .utils import deep_merge, read_text, render_value, resolve_path, slugify


@dataclass(slots=True)
class StepConfig:
    type: str
    config: dict[str, Any]


@dataclass(slots=True)
class PhaseConfig:
    id: str
    title: str
    max_attempts: int
    vars: dict[str, Any]
    run: StepConfig
    verify: StepConfig


@dataclass(slots=True)
class SpecConfig:
    path: Path
    spec_dir: Path
    name: str
    workspace: Path
    run_root: Path
    vars: dict[str, Any]
    defaults: dict[str, Any]
    phases: list[PhaseConfig]


def _load_raw(path: Path) -> dict[str, Any]:
    text = read_text(path)
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _normalize_step(defaults: dict[str, Any], raw_step: dict[str, Any]) -> StepConfig:
    merged = deep_merge(defaults, raw_step)
    step_type = merged.get("type", "shell")
    return StepConfig(type=step_type, config=merged)


def load_spec(path: Path, overrides: dict[str, str] | None = None) -> SpecConfig:
    spec_path = path.resolve()
    spec_dir = spec_path.parent
    raw = _load_raw(spec_path)
    overrides = overrides or {}

    base_context: dict[str, Any] = {
        "spec_name": raw.get("name", spec_path.stem),
        "spec_path": str(spec_path),
        "spec_dir": str(spec_dir),
    }
    base_context.update(raw.get("vars", {}))
    base_context.update(overrides)

    first_pass = render_value(raw, base_context)
    workspace = resolve_path(spec_dir, first_pass["workspace"])

    context = dict(base_context)
    context["workspace"] = str(workspace)
    if "run_root" in first_pass:
        context["run_root"] = first_pass["run_root"]
    second_pass = render_value(first_pass, context)

    if "run_root" not in second_pass or not second_pass["run_root"]:
        second_pass["run_root"] = str(workspace / ".spec-loop" / "runs" / slugify(second_pass["name"]))

    jsonschema.validate(second_pass, SPEC_SCHEMA)

    defaults = second_pass.get("defaults", {})
    default_runner = defaults.get("runner", {"type": "shell"})
    default_verifier = defaults.get("verifier", {"type": "shell"})
    default_max_attempts = int(defaults.get("max_attempts", 3))

    phases = []
    for raw_phase in second_pass["phases"]:
        phase = PhaseConfig(
            id=raw_phase["id"],
            title=raw_phase["title"],
            max_attempts=int(raw_phase.get("max_attempts", default_max_attempts)),
            vars=raw_phase.get("vars", {}),
            run=_normalize_step(default_runner, raw_phase["run"]),
            verify=_normalize_step(default_verifier, raw_phase["verify"]),
        )
        phases.append(phase)

    return SpecConfig(
        path=spec_path,
        spec_dir=spec_dir,
        name=second_pass["name"],
        workspace=workspace,
        run_root=resolve_path(spec_dir, second_pass["run_root"]),
        vars=second_pass.get("vars", {}),
        defaults=defaults,
        phases=phases,
    )
