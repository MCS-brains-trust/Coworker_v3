import re
import uuid
from dataclasses import dataclass

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

# Australian-specific recognisers
TFN_PATTERN = PatternRecognizer(
    supported_entity="AU_TFN",
    patterns=[Pattern("TFN nine-digit", r"\b\d{3}\s?\d{3}\s?\d{3}\b", 0.6)],
)
ABN_PATTERN = PatternRecognizer(
    supported_entity="AU_ABN",
    patterns=[Pattern("ABN eleven-digit", r"\b\d{2}\s?\d{3}\s?\d{3}\s?\d{3}\b", 0.6)],
)
MEDICARE_PATTERN = PatternRecognizer(
    supported_entity="AU_MEDICARE",
    patterns=[Pattern("Medicare ten or eleven digit",
                      r"\b\d{4}\s?\d{5}\s?\d{1,2}\b", 0.5)],
)
DRIVERS_LICENCE_VIC = PatternRecognizer(
    supported_entity="AU_DL_VIC",
    patterns=[Pattern("VIC drivers licence", r"\b[0-9]{8,10}\b", 0.3)],
)


@dataclass
class ScrubResult:
    text: str
    mapping: dict[str, str]  # placeholder -> original

    def restore(self, text: str) -> str:
        for placeholder, original in self.mapping.items():
            text = text.replace(placeholder, original)
        return text


class PIIScrubber:
    def __init__(self) -> None:
        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
        })
        self.analyzer = AnalyzerEngine(
            nlp_engine=provider.create_engine(),
            supported_languages=["en"],
        )
        for r in (TFN_PATTERN, ABN_PATTERN, MEDICARE_PATTERN, DRIVERS_LICENCE_VIC):
            self.analyzer.registry.add_recognizer(r)

    def scrub(self, text: str, *, entities: list[str] | None = None) -> ScrubResult:
        entities = entities or [
            "AU_TFN", "AU_ABN", "AU_MEDICARE", "AU_DL_VIC",
            "PHONE_NUMBER", "EMAIL_ADDRESS", "CREDIT_CARD",
            "IBAN_CODE", "DATE_TIME", "PERSON",
        ]
        results = self.analyzer.analyze(text=text, language="en", entities=entities)
        mapping: dict[str, str] = {}
        scrubbed = text
        # Replace from end to start to preserve offsets
        for r in sorted(results, key=lambda x: x.start, reverse=True):
            placeholder = f"[{r.entity_type}_{uuid.uuid4().hex[:6]}]"
            original = scrubbed[r.start:r.end]
            mapping[placeholder] = original
            scrubbed = scrubbed[:r.start] + placeholder + scrubbed[r.end:]
        return ScrubResult(text=scrubbed, mapping=mapping)
