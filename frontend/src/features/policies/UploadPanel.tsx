import { useId, useState, type DragEvent, type FormEvent } from "react";
import { Link } from "react-router";
import { ApiError, MESSAGES } from "../../api/client";
import { api } from "../../api/endpoints";
import { MAX_UPLOAD_BYTES, type DocumentUploadResponse, type PolicyDocument } from "../../api/types";
import { FormField } from "../../components/FormField";
import { formatBytes } from "../../components/format";
import { Icon } from "../../components/Icon";
import { Notice } from "../../components/States";
import { toApiError } from "../../components/useAsync";
import styles from "./Policies.module.css";

type Stage =
  | { name: "idle" }
  | { name: "uploading"; fraction: number }
  | { name: "processing" }
  | { name: "ready"; result: DocumentUploadResponse }
  | { name: "failed"; message: string };

const KEY = /^[a-z0-9]+(-[a-z0-9]+)*$/;
// The server's fixed refusal codes (app/api/policies.py) → what to tell the user.
const REFUSALS: Record<string, string> = {
  unreadable_pdf: "This file isn't a readable PDF. Check that it is a PDF and isn't password-protected or damaged.",
  no_text: "This PDF has no text layer (it looks scanned). Scanned PDFs can't be indexed yet.",
  empty_file: "The file is empty.",
};

export function nextVersion(documents: PolicyDocument[], key: string): number {
  return Math.max(0, ...documents.filter((d) => d.document_key === key).map((d) => d.version)) + 1;
}

function failureMessage(error: ApiError): string {
  const code = error.fieldCodes.file;
  if (code && REFUSALS[code]) return REFUSALS[code];
  if (error.kind === "conflict") return "This document version already exists. Choose another version number.";
  if (error.kind === "too_large") return `The file is larger than the ${formatBytes(MAX_UPLOAD_BYTES)} limit.`;
  if (error.kind === "validation" && error.fieldErrors.document_key) {
    return "Check the document key: lower-case letters, digits and single hyphens.";
  }
  if (error.kind === "unavailable") return "Indexing is unavailable right now. Try again in a moment.";
  return error.message;
}

export function UploadPanel({ policyId, documents, onUploaded }: {
  policyId: string;
  documents: PolicyDocument[];
  onUploaded: (document: PolicyDocument) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [documentKey, setDocumentKey] = useState("policy-wording");
  const [version, setVersion] = useState<string | null>(null); // null: use the suggested next version
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [stage, setStage] = useState<Stage>({ name: "idle" });
  const [inputKey, setInputKey] = useState(0);
  const [dragging, setDragging] = useState(false);
  const titleId = useId();
  const busy = stage.name === "uploading" || stage.name === "processing";
  const effectiveVersion = version ?? String(nextVersion(documents, documentKey.trim()));

  async function upload(event: FormEvent) {
    event.preventDefault();
    const found: Record<string, string> = {};
    if (!file) found.file = "Choose a PDF file.";
    else if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
      found.file = "Choose a PDF file (.pdf).";
    } else if (file.size === 0) found.file = "The file is empty.";
    else if (file.size > MAX_UPLOAD_BYTES) found.file = `The file is larger than the ${formatBytes(MAX_UPLOAD_BYTES)} limit.`;
    if (!KEY.test(documentKey.trim())) found.documentKey = "Use lower-case letters, digits and single hyphens.";
    if (!/^[1-9][0-9]{0,8}$/.test(effectiveVersion)) found.version = "Use a whole number from 1.";
    setErrors(found);
    if (Object.keys(found).length > 0 || !file) return;
    try {
      const result = await api.uploadDocument(policyId, file, documentKey.trim(), Number(effectiveVersion),
        (name, fraction) => setStage(name === "uploading" ? { name, fraction } : { name: "processing" }));
      setStage({ name: "ready", result });
      onUploaded(result.document);
      setFile(null);
      setVersion(null);
      setInputKey((value) => value + 1);
    } catch (caught) {
      const error = toApiError(caught);
      setStage({ name: "failed", message: error instanceof ApiError ? failureMessage(error) : MESSAGES.server });
    }
  }

  function choose(chosen: File | null) {
    setFile(chosen);
    setStage({ name: "idle" });
    setErrors((current) => ({ ...current, file: "" }));
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    if (busy) return;
    choose(event.dataTransfer.files?.[0] ?? null);
  }

  const steps = [
    { label: "Upload", state: stage.name === "uploading" ? "active" : stage.name === "idle" || stage.name === "failed" ? "pending" : "done" },
    { label: "Extract pages", state: stage.name === "processing" ? "active" : stage.name === "ready" ? "done" : "pending" },
    { label: "Split into passages", state: stage.name === "processing" ? "active" : stage.name === "ready" ? "done" : "pending" },
    { label: "Embed", state: stage.name === "processing" ? "active" : stage.name === "ready" ? "done" : "pending" },
    { label: "Indexed", state: stage.name === "ready" ? "done" : "pending" },
  ] as const;

  return (
    <section className={styles.upload} aria-labelledby={titleId}>
      <div className={styles.sectionHead}>
        <h3 id={titleId} className={styles.uploadTitle}>Upload a policy PDF</h3>
        <p className={styles.muted}>
          ClaimsRAG stores the PDF, reads its text page by page, splits it into passages and indexes them, so it can be
          searched and asked about.
        </p>
      </div>
      <form className={styles.uploadForm} onSubmit={upload} noValidate aria-busy={busy}>
        <label className={`${styles.dropzone} ${dragging ? styles.dragging : ""} ${errors.file ? styles.dropError : ""}`}
          onDragOver={(event) => { event.preventDefault(); if (!busy) setDragging(true); }}
          onDragLeave={() => setDragging(false)} onDrop={onDrop}>
          <input key={inputKey} className={styles.fileInput} type="file" accept="application/pdf,.pdf" disabled={busy}
            aria-label="PDF file" aria-describedby={`${titleId}-limits${errors.file ? ` ${titleId}-error` : ""}`}
            aria-invalid={errors.file ? true : undefined}
            onChange={(event) => choose(event.target.files?.[0] ?? null)} />
          <Icon name="upload" size={22} className={styles.dropIcon} />
          {file ? (
            <span className={styles.chosen}>
              <span className={styles.chosenName}>{file.name}</span>
              <span className={styles.chosenSize}>{formatBytes(file.size)}. Choose another file to replace it.</span>
            </span>
          ) : (
            <span className={styles.dropText}>
              <span className={styles.dropTitle}>Drag and drop a policy PDF here</span>
              <span>or <span className={styles.browse}>browse files</span></span>
            </span>
          )}
          <span id={`${titleId}-limits`} className={styles.dropLimits}>
            PDF with a text layer, up to {formatBytes(MAX_UPLOAD_BYTES)}
          </span>
        </label>
        {errors.file ? <p id={`${titleId}-error`} className={styles.fieldError}>{errors.file}</p> : null}
        <div className={styles.uploadRow}>
          <FormField label="Document" error={errors.documentKey}>
            <input className="input" value={documentKey} maxLength={100} disabled={busy}
              onChange={(event) => { setDocumentKey(event.target.value); setVersion(null); }} />
          </FormField>
          <FormField label="Version" error={errors.version}>
            <input className="input" inputMode="numeric" value={effectiveVersion} disabled={busy}
              onChange={(event) => setVersion(event.target.value)} />
          </FormField>
          <button type="submit" className={`button button-primary ${styles.uploadButton}`} disabled={busy}>
            <Icon name="upload" size={16} />
            {busy ? "Uploading…" : "Upload and index"}
          </button>
        </div>
        <p className={styles.footnote}>The suggested version is the next one for that document.</p>
      </form>

      <div aria-live="polite" className={styles.progressArea}>
        {stage.name === "uploading" || stage.name === "processing" || stage.name === "ready" ? (
          <ol className={styles.pipeline} aria-label="Indexing progress">
            {steps.map((step) => (
              <li key={step.label} className={styles[`step_${step.state}`]}>
                <span className={styles.stepDot} aria-hidden="true" />
                <span className="visually-hidden">{step.state === "done" ? "Done: " : step.state === "active" ? "In progress: " : "Waiting: "}</span>
                {step.label}
              </li>
            ))}
          </ol>
        ) : null}
        {stage.name === "uploading" ? <p className={styles.stageText}>Uploading… {Math.round(stage.fraction * 100)}%</p> : null}
        {stage.name === "processing" ? (
          <p className={styles.stageText}>
            Uploaded. Reading the text, splitting it into passages and indexing them on the server…
          </p>
        ) : null}
        {stage.name === "ready" ? (
          <Notice tone="success">
            Ready: {stage.result.document.original_filename} (version {stage.result.document.version}) is indexed,{" "}
            {stage.result.page_count} {stage.result.page_count === 1 ? "page" : "pages"} in{" "}
            {stage.result.chunk_count} {stage.result.chunk_count === 1 ? "passage" : "passages"}. You can now{" "}
            <Link to="/search">search it</Link> and <Link to="/ask">ask about it</Link>.
          </Notice>
        ) : null}
        {stage.name === "failed" ? <Notice tone="error">Upload failed. {stage.message}</Notice> : null}
      </div>
    </section>
  );
}
