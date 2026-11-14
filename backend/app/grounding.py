"""Evidence sufficiency, abstention and citations (Phase 9, D-031), on top of the Phase 8 answer engine.

    RetrievalResult ── no hits ──→ ABSTAINED (the model is not called)
        → the Phase 8 prompt: the same evidence blocks, tag, budget and grounding rules 1-5, plus a JSON verdict rule
        → the local LLM, its output constrained to {"answer", "evidence": ["E1", ...], "status"}
        → deterministic verification → ANSWERED + citations | CONFLICT + citations | ABSTAINED

- **The model judges and points; the server proves and cites.** The model only says whether the passages answer the
  question and which passage labels (E1, E2, …) support its answer. It never supplies provenance. Each citation is
  built from the server-side hit behind a label the server itself issued for this prompt.
- **Verification**, where any failure means ABSTAINED:
  - the verdict parses;
  - every label is one issued for this prompt (no unknown or repeated label);
  - ANSWERED cites at least one passage, and CONFLICT at least two;
  - the answer is not the insufficiency sentence;
  - the answer contains no page numbers, passage ids or citation markers (those come from the server, never from
    text);
  - every number in the answer appears in the cited passages or the question, so an amount, period or limit can
    never be invented.
- **Relevance is not sufficiency:** retrieval scores never decide. Only the verified verdict does.
- **Abstention** returns the fixed sentence and no citations. The internal reason is logged, never returned.
- **Conflicts** are reported with every conflicting passage cited, and never resolved.
- **One model call per question**, the same as Phase 8. Logs record status, reason, counts and timings only.
"""

import json
import logging
import re
import time
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from app.answering import (GROUNDING_RULES, INSUFFICIENT_EVIDENCE_ANSWER, MAX_ANSWER_CHARS, AnswerError, build_prompt,
                           evidence_tag)
from app.llm import LLMProvider
from app.retrieval import RetrievalHit, RetrievalResult

logger = logging.getLogger(__name__)

ABSTENTION_ANSWER = INSUFFICIENT_EVIDENCE_ANSWER
CONFLICT_NOTICE = "The available policy evidence contains conflicting information."

# Replaces Phase 8's rule 6 (plain prose): the model returns a verdict the server can verify. The answer comes first
# and the status last, so the model writes what the passages say before judging it. Chosen by measurement on a fixed
# set of real-model cases (docs/IMPLEMENTATION_LOG.md, Phase 9): status-first wording passed 7/11, this one 10/11.
VERDICT_RULE = """
6. Reply with one JSON object and nothing else, with these keys in this order:
- "answer": the direct, concise answer from the passages, in plain prose. If passages give different answers, what \
each passage says. If no passage answers the question, the sentence of rule 2. Never put page numbers or citations in it.
- "evidence": the ids (such as "E1") of the passages that state the answer, or of every passage that gives a different \
answer. An empty list if no passage answers.
- "status": "ANSWERED" if the passages state one answer, "CONFLICT" if passages state different answers to it, \
"INSUFFICIENT" only if no passage states it."""
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"},
                   "evidence": {"type": "array", "items": {"type": "string"}},
                   "status": {"type": "string", "enum": ["ANSWERED", "INSUFFICIENT", "CONFLICT"]}},
    "required": ["answer", "evidence", "status"],
}

_LABEL = re.compile(r"E([1-9][0-9]*)(?:\s+([0-9a-f]{24}))?")  # "E2", or "E2 <tag>" as on the marker line
_ANSWER_LABEL = re.compile(r"\b(?:passages?\s+)?E([1-9][0-9]*)\b(?:\s+[0-9a-f]{24}\b)?", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# Page numbers, numbered references and source lines in the answer: provenance comes from the server, never from text.
_CITATION_TEXT = re.compile(r"\b(?:pages?|pp?\.)\s*\d|\[\s*\d+\s*\]|\bsources?\s*:", re.IGNORECASE)


class AnswerStatus(StrEnum):
    ANSWERED = "ANSWERED"
    ABSTAINED = "ABSTAINED"
    CONFLICT = "CONFLICT"


class AbstentionReason(StrEnum):
    """Why an answer was withheld. Internal: logged, never returned to the client."""

    NO_EVIDENCE = "NO_EVIDENCE"
    MODEL_INSUFFICIENT = "MODEL_INSUFFICIENT"
    MALFORMED_VERDICT = "MALFORMED_VERDICT"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    NO_SUPPORTING_EVIDENCE = "NO_SUPPORTING_EVIDENCE"
    INSUFFICIENCY_TEXT = "INSUFFICIENCY_TEXT"
    CITATION_TEXT = "CITATION_TEXT"
    UNGROUNDED_NUMBER = "UNGROUNDED_NUMBER"


class Citation(BaseModel):
    """Where a supporting passage comes from: built only from the stored provenance of a retrieved chunk."""

    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    document_id: UUID
    policy_id: UUID
    document_version: int
    page_start: int
    page_end: int
    section_heading: str | None


class CitedAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str
    status: AnswerStatus
    answer: str
    citations: tuple[Citation, ...]
    model: str
    evidence_ids: tuple[UUID, ...]  # every passage the model was shown, in order
    generated: bool  # whether the model was called
    output_tokens: int | None
    generation_ms: int
    abstention_reason: AbstentionReason | None  # internal; never part of an API response


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    status: str
    answer: str
    evidence: list[str]


def _numbers(text: str) -> set[str]:
    return {number.replace(",", "") for number in _NUMBER.findall(text)}


def _citation(hit: RetrievalHit) -> Citation:
    return Citation(chunk_id=hit.chunk_id, document_id=hit.document_id, policy_id=hit.policy_id,
                    document_version=hit.document_version, page_start=hit.page_start, page_end=hit.page_end,
                    section_heading=hit.section_heading)


def verify(output: str, question: str, shown: tuple[RetrievalHit, ...]) -> tuple[AnswerStatus, str, tuple[RetrievalHit, ...],
                                                                             AbstentionReason | None]:
    """Check the model's verdict against the passages it was shown. Returns (status, answer, cited hits, reason)."""

    def abstain(reason: AbstentionReason) -> tuple[AnswerStatus, str, tuple[RetrievalHit, ...], AbstentionReason]:
        return AnswerStatus.ABSTAINED, ABSTENTION_ANSWER, (), reason

    try:
        verdict = _Verdict.model_validate(json.loads(output))
    except (ValueError, ValidationError):
        return abstain(AbstentionReason.MALFORMED_VERDICT)
    answer = verdict.answer.strip()
    if verdict.status == "INSUFFICIENT":
        return abstain(AbstentionReason.MODEL_INSUFFICIENT)
    if verdict.status not in ("ANSWERED", "CONFLICT") or not answer or len(answer) > MAX_ANSWER_CHARS:
        return abstain(AbstentionReason.MALFORMED_VERDICT)

    tag = evidence_tag(shown)
    cited: list[RetrievalHit] = []
    for label in verdict.evidence:
        match = _LABEL.fullmatch(label.strip())
        if (match is None or match.group(2) not in (None, tag) or not 1 <= int(match.group(1)) <= len(shown)
                or shown[int(match.group(1)) - 1] in cited):
            return abstain(AbstentionReason.UNKNOWN_EVIDENCE)  # unknown, foreign tag, out of range or repeated
        cited.append(shown[int(match.group(1)) - 1])
    if len(cited) < (2 if verdict.status == "CONFLICT" else 1):
        return abstain(AbstentionReason.NO_SUPPORTING_EVIDENCE)
    if INSUFFICIENT_EVIDENCE_ANSWER.lower().rstrip(".") in answer.lower():
        return abstain(AbstentionReason.INSUFFICIENCY_TEXT)
    if _CITATION_TEXT.search(answer):
        return abstain(AbstentionReason.CITATION_TEXT)
    # Passage labels in the answer ("Passage E2 says ...") must name cited passages; they become references to the
    # server-built citations ("[2]" = the second citation). Any other label means the answer leans on uncited text.
    position = {shown.index(hit) + 1: number for number, hit in enumerate(cited, start=1)}
    if any(int(label) not in position for label in _ANSWER_LABEL.findall(answer)):
        return abstain(AbstentionReason.CITATION_TEXT)
    supported = _numbers(question).union(*(_numbers(hit.chunk_text) for hit in cited))
    if not _numbers(_ANSWER_LABEL.sub(" ", answer)) <= supported:  # labels removed: only the claims' numbers count
        return abstain(AbstentionReason.UNGROUNDED_NUMBER)
    answer = _ANSWER_LABEL.sub(lambda m: f"[{position[int(m.group(1))]}]", answer)

    if verdict.status == "CONFLICT":
        return AnswerStatus.CONFLICT, f"{CONFLICT_NOTICE} {answer}", tuple(cited), None
    return AnswerStatus.ANSWERED, answer, tuple(cited), None


def answer_with_citations(llm: LLMProvider, question: str, retrieval: RetrievalResult) -> CitedAnswer:
    """Answer ``question`` from ``retrieval`` with verified citations, or abstain. Raises ``AnswerError`` for invalid
    input and ``app.llm.LLMError`` when the model fails (the caller answers 503; nothing unverified is returned)."""
    if not isinstance(getattr(llm, "model_id", None), str):
        raise AnswerError("The LLM provider has no model id")
    if not isinstance(retrieval, RetrievalResult):
        raise AnswerError("Evidence must be the RetrievalResult of server-side retrieval")
    text = question.strip() if isinstance(question, str) else ""
    if not text:
        raise AnswerError("The question is empty")

    if not retrieval.hits:
        result = CitedAnswer(question=text, status=AnswerStatus.ABSTAINED, answer=ABSTENTION_ANSWER, citations=(),
                             model=llm.model_id, evidence_ids=(), generated=False, output_tokens=None,
                             generation_ms=0, abstention_reason=AbstentionReason.NO_EVIDENCE)
        _log(result, 0)
        return result

    prompt = build_prompt(text, retrieval)  # the Phase 8 evidence blocks, labels E1..En, tag and budget
    by_id = {hit.chunk_id: hit for hit in retrieval.hits}
    shown = tuple(by_id[chunk_id] for chunk_id in prompt.evidence_ids)
    system = GROUNDING_RULES.format(insufficient=INSUFFICIENT_EVIDENCE_ANSWER, tag=evidence_tag(shown)) + VERDICT_RULE

    started = time.monotonic()
    output = llm.generate(system, prompt.user, json_schema=VERDICT_SCHEMA)
    elapsed = int((time.monotonic() - started) * 1000)
    status, answer, cited, reason = verify(output.text if isinstance(output.text, str) else "", text, shown)

    result = CitedAnswer(question=text, status=status, answer=answer, citations=tuple(_citation(hit) for hit in cited),
                         model=llm.model_id, evidence_ids=prompt.evidence_ids, generated=True,
                         output_tokens=output.output_tokens, generation_ms=elapsed, abstention_reason=reason)
    _log(result, len(shown))
    return result


def _log(result: CitedAnswer, shown: int) -> None:
    logger.info("cited answer status=%s reason=%s model=%s evidence=%d citations=%d generation_ms=%d",
                result.status, result.abstention_reason or "none", result.model, shown, len(result.citations),
                result.generation_ms)
