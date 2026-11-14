import { useId, useState, type ReactNode } from "react";
import type { Citation, EvidenceHit } from "../api/types";
import { formatPages } from "./format";
import type { Segment } from "./highlight";
import { Icon } from "./Icon";
import type { Directory } from "./useDirectory";
import styles from "./Evidence.module.css";

type Provenance = Pick<Citation, "policy_id" | "document_id" | "document_version" | "page_start" | "page_end" |
  "section_heading">;

/** Where a passage comes from, as the chain it really is: policy › document version › page › section. Every link
 * comes from the stored provenance the API returned; names are looked up from the tenant's own policies. */
export function ProvenanceTrail({ source, directory, stacked = false }: {
  source: Provenance;
  directory: Directory;
  stacked?: boolean;
}) {
  const policy = directory.policies.get(source.policy_id);
  const document = directory.documents.get(source.document_id);
  const steps: { label: string; value: ReactNode; className?: string }[] = [
    { label: "Policy", value: policy ? `${policy.policy_number} ${policy.name}` : "Policy", className: styles.policy },
    {
      label: "Document",
      value: `${document ? document.original_filename : "Policy document"}, version ${source.document_version}`,
    },
    { label: "Location", value: formatPages(source.page_start, source.page_end), className: styles.page },
  ];
  if (source.section_heading) steps.push({ label: "Section", value: source.section_heading });
  return (
    <ol className={`${styles.trail} ${stacked ? styles.stacked : ""}`} aria-label="Source">
      {steps.map((step, index) => (
        <li key={step.label} className={styles.step}>
          {!stacked && index > 0 ? <Icon name="chevron" size={13} className={styles.separator} /> : null}
          {stacked ? <span className="data-label" aria-hidden="true">{step.label}</span> : null}
          <span className="visually-hidden">{step.label}: </span>
          <span className={step.className}>{step.value}</span>
        </li>
      ))}
    </ol>
  );
}

/** The insurer's own words, set apart from anything the system wrote. Highlights never change the wording. */
export function PolicyText({ text, segments, clamped = false }: {
  text: string;
  segments?: Segment[];
  clamped?: boolean;
}) {
  const paragraphs = text.split(/\n{2,}/);
  return (
    <blockquote className={`${styles.quote} ${clamped ? styles.clamped : ""}`}>
      {segments
        ? <p>{segments.map((s, index) => (s.mark ? <mark key={index}>{s.text}</mark> : <span key={index}>{s.text}</span>))}</p>
        : paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>)}
    </blockquote>
  );
}

/** A passage with a preview and an evidence view: the exact text, under the provenance trail that precedes it. The
 * full text is always in the document (the preview only clips it visually), so it is available to assistive
 * technology. */
export function EvidenceViewer({ hit, segments, defaultOpen = false, label = "View evidence" }: {
  hit: Pick<EvidenceHit, "chunk_text"> & Provenance;
  segments?: Segment[];
  defaultOpen?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <div className={styles.viewer}>
      <div id={id} className={open ? styles.opened : undefined}>
        <PolicyText text={hit.chunk_text} segments={segments} clamped={!open} />
      </div>
      <button type="button" className={`button button-quiet ${styles.toggle}`} aria-expanded={open}
        aria-controls={id} onClick={() => setOpen((value) => !value)}>
        <Icon name="eye" size={15} />
        {open ? "Hide evidence" : label}
      </button>
    </div>
  );
}

export function EvidencePassage({ hit, directory, heading }: {
  hit: EvidenceHit;
  directory: Directory;
  heading?: ReactNode;
}) {
  return (
    <article className={styles.passage}>
      {heading}
      <ProvenanceTrail source={hit} directory={directory} />
      <PolicyText text={hit.chunk_text} />
    </article>
  );
}
