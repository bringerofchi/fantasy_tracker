"""
Deterministic test double for Extractor. Lets the ingestion pipeline be
tested end-to-end (identity resolution, validation, confidence routing,
versioning, review queue) without any network call or API key — proving
the pipeline logic is correct independent of any specific AI backend,
which is the whole point of the extractor being pluggable.
"""
from ingestion.extractor import Extractor, ScreenshotExtraction


class FakeExtractor(Extractor):
    def __init__(self, extraction: ScreenshotExtraction):
        self._extraction = extraction

    def extract(self, image_path, position_hint=None):
        return self._extraction
