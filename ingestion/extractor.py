"""
Pluggable screenshot extraction layer.

The pipeline (ingestion/pipeline.py) depends only on the Extractor interface
below, not on any specific AI provider. This is what "make the AI extraction
layer replaceable" means in practice: swapping AnthropicVisionExtractor for
a different vision model, a local OCR tool, or (in tests) a deterministic
fake requires no change to the pipeline, validation, identity resolution,
confidence routing, or versioning logic downstream.
"""
import base64
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ExtractedField:
    stat_name: str
    value: Optional[float]
    confidence: float  # 0.0-1.0, PER FIELD


@dataclass
class ScreenshotExtraction:
    player_name_raw: str
    player_name_confidence: float
    position_hint: Optional[str] = None
    week_hint: Optional[int] = None
    team_hint: Optional[str] = None
    source_context: Optional[str] = None  # e.g. "looks like an ESPN app screenshot", informational only
    fields: List[ExtractedField] = field(default_factory=list)


class Extractor:
    """Interface every extraction backend implements."""
    def extract(self, image_path: str, position_hint: Optional[str] = None) -> ScreenshotExtraction:
        raise NotImplementedError


class AnthropicVisionExtractor(Extractor):
    """
    Real extraction backend. Requires the `anthropic` package and an
    ANTHROPIC_API_KEY environment variable (this is a standalone local app;
    it does not ship with or embed a key). If neither is available, callers
    should catch ExtractorUnavailable and route the upload straight to the
    Review Queue rather than failing the whole request.
    """
    MODEL = "claude-sonnet-4-6"

    def extract(self, image_path: str, position_hint: Optional[str] = None) -> ScreenshotExtraction:
        try:
            import anthropic
        except ImportError as e:
            raise ExtractorUnavailable("The 'anthropic' package is not installed (pip install anthropic).") from e

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ExtractorUnavailable("ANTHROPIC_API_KEY is not set.")

        with open(image_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")
        media_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"

        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "This is a screenshot of NFL fantasy football data (a stat line, projection, "
            "or ranking). Extract the player name, position (QB/RB/WR/TE) if visible, week "
            "number if visible, team if visible, and every individual stat value you can read. "
            "For EACH extracted field (including the player name), give a confidence from 0.0 to "
            "1.0 reflecting how legible/certain that specific field is — confidence must be "
            "per-field, not a single score for the whole image. "
            "Respond with ONLY JSON, no other text, in this exact shape:\n"
            '{"player_name": "...", "player_name_confidence": 0.0, "position_hint": "WR", '
            '"week_hint": 4, "team_hint": "MIN", "source_context": "...", '
            '"fields": [{"stat_name": "receiving_yards", "value": 95, "confidence": 0.9}]}\n'
            "Only use these stat_name keys where applicable: pass_yards, pass_tds, interceptions, "
            "rush_yards, rush_tds, pass_completions, pass_attempts, receptions, receiving_yards, receiving_tds, "
            "fumbles_lost. Omit any field you cannot find in the image rather than guessing a value."
        )
        response = client.messages.create(
            model=self.MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(text)

        return ScreenshotExtraction(
            player_name_raw=parsed.get("player_name", ""),
            player_name_confidence=float(parsed.get("player_name_confidence", 0.0)),
            position_hint=parsed.get("position_hint") or position_hint,
            week_hint=parsed.get("week_hint"),
            team_hint=parsed.get("team_hint"),
            source_context=parsed.get("source_context"),
            fields=[
                ExtractedField(stat_name=f["stat_name"], value=f.get("value"), confidence=float(f.get("confidence", 0.0)))
                for f in parsed.get("fields", [])
            ],
        )


class ExtractorUnavailable(Exception):
    """Raised when the configured extraction backend can't run (e.g. no API key)."""
    pass
