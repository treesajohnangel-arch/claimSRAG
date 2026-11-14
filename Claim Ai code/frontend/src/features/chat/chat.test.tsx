import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { ChatResponse } from "../../api/types";
import { AMBULANCE, MATERNITY, chatResponse, mockApi, policy, policyDocument, renderApp, demoUser } from "../../test/utils";

const DIRECTORY = {
  "GET /policies": { status: 200, body: [policy] },
  [`GET /documents/${policyDocument.id}`]: { status: 200, body: policyDocument },
};
const QUESTION = "What is the maternity waiting period?";

async function askWith(response: ChatResponse | { status: number; body: unknown }) {
  const reply = "status" in response && typeof response.status === "number" ? response : { status: 200, body: response };
  const api = mockApi({ ...DIRECTORY, "POST /chat": reply as { status: number; body: unknown } });
  demoUser("STAFF");
  await renderApp("/ask");
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Your question"), QUESTION);
  await user.click(screen.getByRole("button", { name: "Ask" }));
  const turn = await screen.findByRole("region", { name: `Question: ${QUESTION}` });
  return { ...api, user, turn };
}

describe("asking a question", () => {
  it("sends only the question, passage count and chosen filters", async () => {
    const { requests } = await askWith(chatResponse("ANSWERED"));

    await screen.findByText("Answered from policy evidence");
    expect(requests.find((r) => r.path === "/chat")?.body).toEqual({ question: QUESTION, top_k: 5, filters: {} });
  });

  it("shows progress while the answer is written, and blocks a second question", async () => {
    let release: () => void = () => {};
    mockApi({ ...DIRECTORY, "POST /chat": () => new Promise((r) => { release = () => r({ status: 200, body: chatResponse("ANSWERED") }); }) });
    demoUser("STAFF");
    await renderApp("/ask");
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Your question"), QUESTION);
    await user.click(screen.getByRole("button", { name: "Ask" }));

    expect(screen.getByRole("button", { name: "Answering…" })).toBeDisabled();
    expect(screen.getByRole("region", { name: `Question: ${QUESTION}` })).toHaveAttribute("aria-busy", "true");
    release();
    expect(await screen.findByText("Answered from policy evidence")).toBeInTheDocument();
  });
});

describe("an answered question", () => {
  it("shows the answer, then each citation with its provenance and the insurer's exact words", async () => {
    const { turn } = await askWith(chatResponse("ANSWERED"));

    expect(await within(turn).findByText("Answered from policy evidence")).toBeInTheDocument();
    expect(within(turn).getByText(/Maternity expenses are covered after 9 months/)).toBeInTheDocument();
    const sources = within(turn).getByRole("complementary", { name: "Sources" });
    const source = within(sources).getByRole("article", { name: "Source 1" });
    expect(await within(source).findByText("DH-100 Family Health Plus")).toBeInTheDocument();
    expect(await within(source).findByText("family-health-wording.pdf, version 2")).toBeInTheDocument();
    expect(within(source).getByText("Page 3")).toBeInTheDocument();
    expect(within(source).getByText("SECTION 1")).toBeInTheDocument();
    expect(within(source).getByText(MATERNITY).closest("blockquote")).not.toBeNull();
    expect(within(sources).getAllByRole("article")).toHaveLength(1); // only what the answer rests on
  });

  it("highlights the sentence of a source that the answer rests on, without changing its words", async () => {
    const passage = "Maternity expenses are covered after a waiting period of 9 months. Newborn cover starts at birth.";
    const response = chatResponse("ANSWERED", {
      evidence: [{ ...chatResponse("ANSWERED").evidence[0]!, chunk_text: passage }, chatResponse("ANSWERED").evidence[1]!],
    });
    const { turn } = await askWith(response);

    const source = await within(turn).findByRole("article", { name: "Source 1" });
    const quote = within(source).getByRole("blockquote");
    expect(quote).toHaveTextContent(passage);
    expect([...quote.querySelectorAll("mark")].map((m) => m.textContent)).toEqual(
      ["Maternity expenses are covered after a waiting period of 9 months."]);
  });

  it("turns each [k] into a link to source k, and leaves anything else as text", async () => {
    const response = chatResponse("ANSWERED", { answer: "Covered after 9 months [1]; see also [3]." });
    const { turn, user } = await askWith(response);

    const reference = await within(turn).findByRole("button", { name: "Source 1, page 3" });
    expect(within(turn).queryByRole("button", { name: /Source 3/ })).toBeNull();
    expect(within(turn).getByText(/see also \[3\]/)).toBeInTheDocument();
    await user.click(reference);
    expect(within(turn).getByRole("article", { name: "Source 1" })).toHaveFocus();
  });

  it("keeps the passages the answer does not rest on separate", async () => {
    const { turn } = await askWith(chatResponse("ANSWERED"));

    const reviewed = (await within(turn).findByText("Other passages reviewed (1)")).closest("details")!;
    expect(within(reviewed).getByText(AMBULANCE)).toBeInTheDocument();
    expect(within(turn).getByText("Written by qwen2.5:3b from 2 passages in 4.4 s.")).toBeInTheDocument();
  });
});

describe("a conflict", () => {
  it("is shown as conflicting evidence with every source, never as a plain answer", async () => {
    const { turn } = await askWith(chatResponse("CONFLICT"));

    expect(await within(turn).findByText("Conflicting policy evidence")).toBeInTheDocument();
    expect(within(turn).getByText(/doesn't choose between them/)).toBeInTheDocument();
    expect(within(turn).queryByText("Answered from policy evidence")).toBeNull();
    const sources = within(turn).getByRole("complementary", { name: "Sources" });
    expect(within(sources).getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual(["Source 1", "Source 2"]);
    expect(within(turn).getByRole("button", { name: "Source 2, page 4" })).toBeInTheDocument();
  });
});

describe("an abstention", () => {
  it("says there isn't enough evidence, with no sources and nothing that looks like an answer", async () => {
    const { turn } = await askWith(chatResponse("ABSTAINED"));

    expect(await within(turn).findByText("Insufficient evidence")).toBeInTheDocument();
    expect(within(turn).getByText(/No unsupported answer was generated/)).toBeInTheDocument();
    expect(within(turn).getByText("The provided policy evidence is insufficient to determine the answer.")).toBeInTheDocument();
    expect(within(turn).queryByRole("complementary", { name: "Sources" })).toBeNull();
    expect(within(turn).queryByText("Answered from policy evidence")).toBeNull();
    const reviewed = within(turn).getByText("Passages reviewed (2)").closest("details")!;
    expect(within(reviewed).getByText(/don't support an answer/)).toBeInTheDocument();
  });

  it("says when nothing matched at all and no answer was written", async () => {
    const { turn } = await askWith(chatResponse("ABSTAINED", { evidence: [], evidence_ids: [], generated: false }));

    expect(await within(turn).findByText(/No passage in your company's active policy documents matched/)).toBeInTheDocument();
    expect(within(turn).getByText("No passages were found, so no answer was written.")).toBeInTheDocument();
    expect(within(turn).queryByText(/Passages reviewed/)).toBeNull();
  });
});

describe("answering errors", () => {
  it("explains an unavailable answer service and offers a retry", async () => {
    const { turn } = await askWith({ status: 503, body: { detail: "Answering is temporarily unavailable" } });

    const alert = await within(turn).findByRole("alert");
    expect(alert).toHaveTextContent("Answering is unavailable right now");
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("asks for a question instead of sending an empty one", async () => {
    const { requests } = mockApi(DIRECTORY);
    demoUser("STAFF");
    await renderApp("/ask");

    await userEvent.setup().click(screen.getByRole("button", { name: "Ask" }));

    expect(screen.getByText("Enter a question about your policies.")).toBeInTheDocument();
    expect(requests.some((r) => r.path === "/chat")).toBe(false);
  });
});
