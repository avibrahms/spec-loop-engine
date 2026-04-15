SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "name", "workspace", "phases"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "name": {"type": "string", "minLength": 1},
        "workspace": {"type": "string", "minLength": 1},
        "run_root": {"type": "string"},
        "vars": {"type": "object"},
        "defaults": {"type": "object"},
        "phases": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "title", "run", "verify"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "max_attempts": {"type": "integer", "minimum": 1},
                    "vars": {"type": "object"},
                    "run": {"type": "object"},
                    "verify": {"type": "object"},
                },
            },
        },
    },
}

RUNNER_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "artifacts", "resume_hint", "notes"],
    "properties": {
        "status": {"type": "string", "enum": ["success", "retry", "blocked"]},
        "summary": {"type": "string", "minLength": 1},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "resume_hint": {"type": "string"},
        "notes": {"type": "string"},
    },
}

VERIFIER_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "issues", "repair_hint", "confidence"],
    "properties": {
        "status": {"type": "string", "enum": ["passed", "retry", "blocked"]},
        "summary": {"type": "string", "minLength": 1},
        "issues": {"type": "array", "items": {"type": "string"}},
        "repair_hint": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}
