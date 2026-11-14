import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router";
import { isAbort } from "../../api/client";
import { api, chosenFilters } from "../../api/endpoints";
import { MAX_CHAT_PASSAGES, MAX_QUESTION_CHARS, type ChatResponse, type RetrievalFilters } from "../../api/types";
import { FormField } from "../../components/FormField";
import { Icon } from "../../components/Icon";
import { PageHeader } from "../../components/PageHeader";
import { ErrorState, LoadingState } from "../../components/States";
import { toApiError } from "../../components/useAsync";
import { useDirectory } from "../../components/useDirectory";
import { EvidenceFilters } from "../search/SearchPage";
import { AnswerCard } from "./AnswerCard";
import styles from "./Chat.module.css";

interface Turn {
  id: number;
  question: string;
  topK: number;
  filters: RetrievalFilters;
  state: { status: "pending" } | { status: "done"; response: ChatResponse } | { status: "error"; message: string };
}

const PASSAGE_COUNTS = Array.from({ length: MAX_CHAT_PASSAGES }, (_, index) => index + 1);

export function ChatPage() {
  const [question, setQuestion] = useState("");
  const [params] = useSearchParams();
  const [filters, setFilters] = useState<RetrievalFilters>(() => ({ policy_id: params.get("policy_id") || undefined }));
  const [topK, setTopK] = useState(5);
  const [questionError, setQuestionError] = useState<string | undefined>();
  const [turns, setTurns] = useState<Turn[]>([]);
  const nextId = useRef(1);
  const controllers = useRef(new Map<number, AbortController>());
  const pending = turns.some((turn) => turn.state.status === "pending");
  const directory = useDirectory(
    turns.flatMap((turn) => (turn.state.status === "done" ? turn.state.response.evidence.map((h) => h.document_id) : [])),
  );

  useEffect(() => () => controllers.current.forEach((controller) => controller.abort()), []);

  async function ask(text: string, askTopK: number, askFilters: RetrievalFilters, id = nextId.current++) {
    const controller = new AbortController();
    controllers.current.set(id, controller);
    const turn: Turn = { id, question: text, topK: askTopK, filters: askFilters, state: { status: "pending" } };
    setTurns((current) => [turn, ...current.filter((t) => t.id !== id)]);
    const update = (state: Turn["state"]) =>
      setTurns((current) => current.map((t) => (t.id === id ? { ...t, state } : t)));
    try {
      const response = await api.chat(text, askTopK, chosenFilters(askFilters), controller.signal);
      update({ status: "done", response });
    } catch (caught) {
      if (isAbort(caught)) return;
      const error = toApiError(caught);
      update({
        status: "error",
        message: error.kind === "unavailable"
          ? "Answering is unavailable right now: the answer service isn't responding. Try again in a moment."
          : error.message,
      });
    } finally {
      controllers.current.delete(id);
    }
  }

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const text = question.trim();
    if (!text) {
      setQuestionError("Enter a question about your policies.");
      return;
    }
    if (text.length > MAX_QUESTION_CHARS) {
      setQuestionError(`Keep the question under ${MAX_QUESTION_CHARS.toLocaleString("en")} characters.`);
      return;
    }
    setQuestionError(undefined);
    setQuestion("");
    void ask(text, topK, filters);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) submit();
  }

  return (
    <>
      <PageHeader
        title="Ask ClaimsRAG"
        description="Ask about your company's policies. Every answer comes from your active policy documents and shows the passages it rests on; when they don't settle the question, you're told so instead of getting a guess."
      />
      <form className={styles.form} onSubmit={submit} noValidate>
        <FormField label="Your question" hint="Press Ctrl+Enter to ask." error={questionError}>
          <textarea className={`textarea ${styles.questionInput}`} rows={3} value={question} maxLength={MAX_QUESTION_CHARS}
            placeholder="for example: What is the waiting period for maternity expenses?"
            onChange={(event) => setQuestion(event.target.value)} onKeyDown={onKeyDown} />
        </FormField>
        <div className={styles.formRow}>
          <div className={styles.filters}>
            <EvidenceFilters filters={filters} onChange={setFilters} />
            <FormField label="Passages to read">
              <select className="select" value={topK} onChange={(event) => setTopK(Number(event.target.value))}>
                {PASSAGE_COUNTS.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </FormField>
          </div>
          <button type="submit" className={`button button-primary ${styles.askButton}`} disabled={pending}>
            <Icon name="ask" size={16} />
            {pending ? "Answering…" : "Ask"}
          </button>
        </div>
      </form>

      <div className={styles.turns} aria-live="polite">
        {turns.map((turn) => (
          <section key={turn.id} className={styles.turn} aria-label={`Question: ${turn.question}`}
            aria-busy={turn.state.status === "pending"}>
            <h2 className={styles.question}>
              <Icon name="ask" size={18} />
              <span>{turn.question}</span>
            </h2>
            {turn.state.status === "pending" ? (
              <div className={styles.pending}>
                <LoadingState label="Retrieving the relevant passages and writing a grounded answer…" rows={4} showLabel />
                <p className={styles.muted}>
                  Reading the policy evidence. On this server an answer can take up to a minute.
                </p>
              </div>
            ) : null}
            {turn.state.status === "error" ? (
              <ErrorState title="No answer" message={turn.state.message}
                onRetry={pending ? undefined : () => void ask(turn.question, turn.topK, turn.filters, turn.id)} />
            ) : null}
            {turn.state.status === "done" ? <AnswerCard response={turn.state.response} directory={directory} /> : null}
          </section>
        ))}
      </div>
    </>
  );
}
