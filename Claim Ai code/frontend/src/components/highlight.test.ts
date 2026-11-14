import { describe, expect, it } from "vitest";
import { markAnswerSentences, markTerms, queryTerms } from "./highlight";

const PASSAGE = "Maternity expenses are covered after a waiting period of 9 months. Road ambulance charges up to " +
  "Rs 2,000 per hospitalisation are reimbursed.\n\nCosmetic surgery is not covered.";
const joined = (segments: { text: string }[]) => segments.map((s) => s.text).join("");

describe("highlighting policy text", () => {
  it("never changes the text: the segments rejoin to exactly the original", () => {
    for (const segments of [markTerms(PASSAGE, queryTerms("maternity waiting period")),
      markAnswerSentences(PASSAGE, "Up to Rs 2000 is reimbursed."), markAnswerSentences(PASSAGE, "Nothing matches here.")]) {
      expect(joined(segments)).toBe(PASSAGE);
    }
  });

  it("marks a search's significant words, whole-word and case-insensitively", () => {
    expect(queryTerms("What is the waiting period for maternity?")).toEqual(["waiting", "period", "maternity"]);
    const marked = markTerms(PASSAGE, queryTerms("maternity waiting")).filter((s) => s.mark).map((s) => s.text);

    expect(marked).toEqual(["Maternity", "waiting"]);
  });

  it("marks the sentence that shares the answer's number, however the number is written", () => {
    const marked = markAnswerSentences(PASSAGE, "Up to Rs 2000 is reimbursed for an ambulance.")
      .filter((s) => s.mark).map((s) => s.text);

    expect(marked).toEqual(["Road ambulance charges up to Rs 2,000 per hospitalisation are reimbursed."]);
  });

  it("without numbers, marks sentences sharing two significant words, and nothing otherwise", () => {
    expect(markAnswerSentences(PASSAGE, "Cosmetic surgery is excluded.").filter((s) => s.mark).map((s) => s.text))
      .toEqual(["Cosmetic surgery is not covered."]);
    expect(markAnswerSentences(PASSAGE, "Dental care.").some((s) => s.mark)).toBe(false);
  });
});
