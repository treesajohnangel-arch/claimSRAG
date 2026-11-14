import { Fragment, useId, useRef, useState, type ReactNode } from "react";
import type { ChatResponse, Citation, EvidenceHit } from "../../api/types";
import { EvidenceViewer, ProvenanceTrail } from "../../components/Evidence";
import { formatPages, formatSeconds } from "../../components/format";
import { markAnswerSentences } from "../../components/highlight";
import { Icon, type IconName } from "../../components/Icon";
import type { Directory } from "../../components/useDirectory";
import styles from "./Chat.module.css";

const STATUS: Record<ChatResponse["status"], { title: string; icon: IconName; chip: string }> = {
  ANSWERED: { title: "Answered from policy evidence", icon: "shield", chip: "Grounded answer" },
  CONFLICT: { title: "Conflicting policy evidence", icon: "conflict", chip: "Conflict reported" },
  ABSTAINED: { title: "Insufficient evidence", icon: "abstained", chip: "No answer generated" },
};

function explanation(response: ChatResponse): string {
  if (response.status === "CONFLICT") {
    return "The cited passages make different statements about this. ClaimsRAG doesn't choose between them: review each source before relying on either.";
  }
  if (response.status === "ABSTAINED") {
    return "ClaimsRAG couldn't find enough information in your company's policy documents to answer this reliably. No unsupported answer was generated.";
  }
  const count = response.citations.length;
  return `Every statement rests on ${count === 1 ? "the cited passage" : `the ${count} cited passages`} below, checked against your company's policy documents.`;
}

/** The answer text with each "[k]" the server placed turned into a link to source k. Text that is not a valid
 * reference to a returned citation stays plain text: a reference is never invented on the client. */
function AnswerText({ text, citations, onReference }: {
  text: string;
  citations: Citation[];
  onReference: (index: number) => void;
}) {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(/\[(\d+)\]/g)) {
    const number = Number(match[1]);
    const start = match.index ?? 0;
    parts.push(text.slice(last, start));
    const citation = citations[number - 1];
    if (citation) {
      parts.push(
        <button key={start} type="button" className={styles.reference} onClick={() => onReference(number - 1)}
          aria-label={`Source ${number}, ${formatPages(citation.page_start, citation.page_end).toLowerCase()}`}>
          {number}
        </button>,
      );
    } else {
      parts.push(match[0]);
    }
    last = start + match[0].length;
  }
  parts.push(text.slice(last));
  return <p className={styles.answerText}>{parts.map((part, index) => <Fragment key={index}>{part}</Fragment>)}</p>;
}

function CitationCard({ index, citation, passage, answer, directory, highlighted, cardRef }: {
  index: number;
  citation: Citation;
  passage: EvidenceHit | undefined;
  answer: string;
  directory: Directory;
  highlighted: boolean;
  cardRef: (element: HTMLElement | null) => void;
}) {
  return (
    <article ref={cardRef} tabIndex={-1} className={`${styles.citation} ${highlighted ? styles.citationActive : ""}`}
      aria-label={`Source ${index + 1}`}>
      <div className={styles.citationHead}>
        <span className={styles.citationNumber} aria-hidden="true">{index + 1}</span>
        <span className="data-label">Source</span>
      </div>
      <ProvenanceTrail source={citation} directory={directory} stacked />
      {passage ? (
        <EvidenceViewer hit={{ ...citation, chunk_text: passage.chunk_text }} defaultOpen={index === 0}
          segments={markAnswerSentences(passage.chunk_text, answer)} />
      ) : (
        <p className={styles.muted}>The passage text isn't available.</p>
      )}
    </article>
  );
}

function ReviewedPassages({ hits, directory, summary, note }: {
  hits: EvidenceHit[];
  directory: Directory;
  summary: string;
  note: string;
}) {
  return (
    <details className={styles.reviewed}>
      <summary>{summary} ({hits.length})</summary>
      <p className={styles.muted}>{note}</p>
      <ul className={styles.reviewedList}>
        {hits.map((hit) => (
          <li key={hit.chunk_id}>
            <ProvenanceTrail source={hit} directory={directory} />
            <EvidenceViewer hit={hit} />
          </li>
        ))}
      </ul>
    </details>
  );
}

export function AnswerCard({ response, directory }: { response: ChatResponse; directory: Directory }) {
  const status = STATUS[response.status];
  const passages = new Map(response.evidence.map((hit) => [hit.chunk_id, hit]));
  const cited = new Set(response.citations.map((citation) => citation.chunk_id));
  const reviewed = response.evidence.filter((hit) => !cited.has(hit.chunk_id));
  const answerWithoutReferences = response.answer.replace(/\[\d+\]/g, " ");
  const cards = useRef<(HTMLElement | null)[]>([]);
  const [highlighted, setHighlighted] = useState<number | null>(null);
  const titleId = useId();

  function showSource(index: number) {
    const card = cards.current[index];
    setHighlighted(index);
    card?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    card?.focus({ preventScroll: true });
  }

  return (
    <section className={`${styles.answer} ${styles[response.status]}`} aria-labelledby={titleId}>
      <header className={styles.statusBar}>
        <Icon name={status.icon} size={20} />
        <div className={styles.statusText}>
          <h3 id={titleId} className={styles.statusTitle}>{status.title}</h3>
          <p className={styles.statusExplain}>{explanation(response)}</p>
        </div>
        <span className={styles.chip}>{status.chip}</span>
      </header>

      <div className={styles.answerBody}>
        {response.status === "ABSTAINED" ? (
          <div className={styles.abstained}>
            <p className={styles.systemLine}>{response.answer}</p>
            <p className={styles.muted}>
              {response.evidence.length === 0
                ? "No passage in your company's active policy documents matched this question."
                : "Try naming the benefit, limit or section, or choose the policy it is in."}
            </p>
          </div>
        ) : (
          <div className={styles.answerBlock}>
            <span className="data-label">Answer</span>
            <AnswerText text={response.answer} citations={response.citations} onReference={showSource} />
          </div>
        )}

        <p className={styles.meta}>
          {response.generated
            ? `Written by ${response.model} from ${response.evidence.length} ${response.evidence.length === 1 ? "passage" : "passages"} in ${formatSeconds(response.retrieval_ms + response.generation_ms)}.`
            : "No passages were found, so no answer was written."}
        </p>

        {response.citations.length > 0 ? (
          <aside className={styles.sources} aria-label="Sources">
            <h4 className={styles.sourcesTitle}>
              {response.citations.length === 1 ? "1 source" : `${response.citations.length} sources`}
            </h4>
            <div className={styles.citationGrid}>
              {response.citations.map((citation, index) => (
                <CitationCard key={citation.chunk_id} index={index} citation={citation}
                  passage={passages.get(citation.chunk_id)} answer={answerWithoutReferences} directory={directory}
                  highlighted={highlighted === index} cardRef={(element) => { cards.current[index] = element; }} />
              ))}
            </div>
          </aside>
        ) : null}

        {reviewed.length > 0 ? (
          response.status === "ABSTAINED" ? (
            <ReviewedPassages hits={reviewed} directory={directory} summary="Passages reviewed"
              note="These passages were retrieved for the question but don't support an answer." />
          ) : (
            <ReviewedPassages hits={reviewed} directory={directory} summary="Other passages reviewed"
              note="These passages were retrieved and read, but the answer doesn't rest on them." />
          )
        ) : null}
      </div>
    </section>
  );
}
