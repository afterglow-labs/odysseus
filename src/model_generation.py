"""Validated, per-model generation preferences for foreground chat."""
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class GenerationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, ge=0, le=1)
    top_k: int | None = Field(None, ge=0, le=100000)
    min_p: float | None = Field(None, ge=0, le=1)
    typical_p: float | None = Field(None, ge=0, le=1)
    max_tokens: int | None = Field(None, ge=0, le=1000000)
    seed: int | None = Field(None, ge=-1, le=4294967295)
    repeat_penalty: float | None = Field(None, ge=0, le=5)
    repeat_last_n: int | None = Field(None, ge=-1, le=1000000)
    presence_penalty: float | None = Field(None, ge=-2, le=2)
    frequency_penalty: float | None = Field(None, ge=-2, le=2)
    thinking: Literal["auto", "on", "off"] | None = None
    reasoning_effort: Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    reasoning_budget: int | None = Field(None, ge=-1, le=1000000)
    stop: list[str] | None = Field(None, max_length=16)
    mirostat: int | None = Field(None, ge=0, le=2)
    mirostat_tau: float | None = Field(None, ge=0, le=100)
    mirostat_eta: float | None = Field(None, ge=0, le=1)
    dynatemp_range: float | None = Field(None, ge=0, le=2)
    dynatemp_exponent: float | None = Field(None, gt=0, le=100)
    xtc_probability: float | None = Field(None, ge=0, le=1)
    xtc_threshold: float | None = Field(None, ge=0, le=1)
    dry_multiplier: float | None = Field(None, ge=0, le=100)
    dry_base: float | None = Field(None, ge=0, le=100)
    dry_allowed_length: int | None = Field(None, ge=0, le=1000000)
    dry_penalty_last_n: int | None = Field(None, ge=-1, le=1000000)
    samplers: list[str] | None = Field(None, max_length=24)


def validate_generation_options(value):
    try:
        if isinstance(value, str):
            value = json.loads(value)
        options = GenerationOptions.model_validate(value or {}).model_dump(exclude_none=True)
        if any(len(s) > 1024 for s in options.get("stop", [])):
            raise ValueError("Stop sequences must be at most 1024 characters")
        return options
    except (ValidationError, ValueError, TypeError) as exc:
        # Values never need to be echoed in a validation error.
        detail = "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors()) if isinstance(exc, ValidationError) else "Invalid model generation options"
        raise HTTPException(422, detail) from exc


def generation_key(url, model):
    return json.dumps([str(url or "").rstrip("/"), str(model or "")], ensure_ascii=False, separators=(",", ":"))


def capture_generation_options(session, owner, value=None):
    if value is None:
        from routes.prefs_routes import _load_for_user
        profiles = _load_for_user(owner).get("model-generation") or {}
        value = profiles.get(generation_key(session.endpoint_url, session.model), {})
    return validate_generation_options(value)


def apply_generation_options(payload, options, *, provider, local=False, max_token_key="max_tokens", temperature_allowed=True):
    """Translate user controls after automatic defaults have been applied."""
    if not options:
        return
    options = validate_generation_options(options)
    if provider == "chatgpt-subscription":
        # Its existing picker performs model/account capability validation.
        return
    if provider == "ollama":
        target = payload.setdefault("options", {})
        for key in ("temperature", "top_p", "top_k", "min_p", "typical_p", "seed", "repeat_penalty", "repeat_last_n", "presence_penalty", "frequency_penalty", "stop", "mirostat", "mirostat_tau", "mirostat_eta"):
            if key in options:
                target[key] = options[key]
        if "max_tokens" in options:
            target["num_predict"] = options["max_tokens"] or -1
        if options.get("thinking") in {"on", "off"}:
            payload["think"] = options["thinking"] == "on"
        if options.get("reasoning_effort") == "none":
            payload["think"] = False
        elif options.get("reasoning_effort") in {"low", "medium", "high"}:
            payload["think"] = options["reasoning_effort"]
        return
    if "temperature" in options and temperature_allowed:
        payload["temperature"] = min(options["temperature"], 1) if provider == "anthropic" else options["temperature"]
    if "max_tokens" in options:
        payload.pop("max_tokens", None)
        payload.pop("max_completion_tokens", None)
        if options["max_tokens"]:
            payload[max_token_key] = options["max_tokens"]
        elif provider == "anthropic":
            payload["max_tokens"] = 4096
    if provider == "anthropic":
        for key in ("top_p", "top_k"):
            if key in options and temperature_allowed:
                payload[key] = options[key]
        if "stop" in options:
            payload["stop_sequences"] = options["stop"]
        if options.get("thinking") == "off" or options.get("reasoning_effort") == "none":
            payload["thinking"] = {"type": "disabled"}
        elif options.get("thinking") == "on":
            budget = max(1024, options.get("reasoning_budget", 1024))
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = max(payload.get("max_tokens", 4096), budget + 1)
            payload.pop("temperature", None)
        return
    for key in ("top_p", "seed", "presence_penalty", "frequency_penalty", "stop"):
        if key in options:
            payload[key] = options[key]
    if local:
        for key in ("top_k", "min_p", "typical_p", "repeat_penalty", "repeat_last_n", "mirostat", "mirostat_tau", "mirostat_eta", "dynatemp_range", "dynatemp_exponent", "xtc_probability", "xtc_threshold", "dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n", "samplers"):
            if key in options:
                payload[key] = options[key]
        template = dict(payload.get("chat_template_kwargs") or {})
        if options.get("thinking") in {"on", "off"}:
            template["enable_thinking"] = options["thinking"] == "on"
        effort = options.get("reasoning_effort")
        if effort and effort != "default":
            payload["reasoning_effort"] = effort
            template["reasoning_effort"] = effort
            if effort == "none":
                template["enable_thinking"] = False
        if template:
            payload["chat_template_kwargs"] = template
        if "reasoning_budget" in options:
            payload["reasoning_budget_tokens"] = options["reasoning_budget"]
        if template.get("enable_thinking") is False:
            payload["reasoning_budget_tokens"] = 0
            payload["reasoning_effort"] = "none"
            if "think" in payload:
                payload["think"] = False
    else:
        effort = options.get("reasoning_effort")
        if effort and effort != "default":
            payload["reasoning_effort"] = effort
        if options.get("thinking") == "off":
            payload["reasoning_effort"] = "none"
