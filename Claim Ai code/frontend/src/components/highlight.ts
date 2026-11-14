// Highlighting inside policy text. The text itself is never changed: these functions only split it into segments,
// some marked, whose concatenation is exactly the original.

export interface Segment {
  text: string;
  mark: boolean;
}

const STOP_WORDS = new Set([
  "the", "and", "for", "are", "was", "were", "with", "that", "this", "what", "when", "which", "who", "how", "does",
  "did", "can", "any", "all", "not", "but", "from", "under", "over", "into", "its", "has", "have", "had", "will",
  "would", "should", "may", "per", "than", "then", "there", "their", "about", "after", "before", "policy", "is",
]);

const NUMBER = /\d+(?:[.,]\d+)*/g;

function escape(term: string): string {
  return term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The words of a search worth highlighting: at least three letters, not a common word, each once. */
export function queryTerms(query: string): string[] {
  const words = query.toLowerCase().match(/[\p{L}\p{N}][\p{L}\p{N}-]*/gu) ?? [];
  return [...new Set(words.filter((word) => word.length >= 3 && !STOP_WORDS.has(word)))];
}

/** Mark whole-word (prefix) occurrences of the terms, case-insensitively. */
export function markTerms(text: string, terms: string[]): Segment[] {
  if (terms.length === 0) return [{ text, mark: false }];
  const pattern = new RegExp(`\\b(${terms.map(escape).join("|")})[\\p{L}]*`, "giu");
  const segments: Segment[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > last) segments.push({ text: text.slice(last, start), mark: false });
    segments.push({ text: match[0], mark: true });
    last = start + match[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last), mark: false });
  return segments;
}

function numbers(text: string): Set<string> {
  return new Set((text.match(NUMBER) ?? []).map((n) => n.replace(/,/g, "")));
}

/** Mark the sentences of a cited passage that the answer rests on: those sharing a number with the answer, or, when
 * the answer states no number, at least two of its significant words. Nothing is marked if nothing matches. */
export function markAnswerSentences(text: string, answer: string): Segment[] {
  const sentences = text.match(/[^.!?\n]+(?:[.!?]+|$)|\n+/g) ?? [text];
  const answerNumbers = numbers(answer);
  const answerTerms = queryTerms(answer);
  const supports = (sentence: string) => {
    if (answerNumbers.size > 0) return [...numbers(sentence)].some((n) => answerNumbers.has(n));
    const lower = sentence.toLowerCase();
    return answerTerms.filter((term) => lower.includes(term)).length >= 2;
  };
  const segments: Segment[] = [];
  for (const sentence of sentences) {
    const lead = sentence.match(/^\s*/)?.[0] ?? "";
    const body = sentence.slice(lead.length);
    if (lead) segments.push({ text: lead, mark: false });
    if (body) segments.push({ text: body, mark: supports(body) });
  }
  // Rejoin neighbours with the same marking, so the passage renders as few elements as possible.
  return segments.reduce<Segment[]>((joined, segment) => {
    const previous = joined[joined.length - 1];
    if (previous && previous.mark === segment.mark) previous.text += segment.text;
    else joined.push({ ...segment });
    return joined;
  }, []);
}
