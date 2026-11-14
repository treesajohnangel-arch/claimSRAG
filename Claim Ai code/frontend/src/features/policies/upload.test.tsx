import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TOKEN, mockApi, policy, policyDocument, renderApp, demoUser } from "../../test/utils";
import { nextVersion } from "./UploadPanel";

/** A stand-in for XMLHttpRequest that the test drives: progress, upload complete, then the response. */
class FakeXHR {
  static last: FakeXHR | null = null;
  method = "";
  url = "";
  headers: Record<string, string> = {};
  body: unknown;
  status = 0;
  responseText = "";
  withCredentials = true;
  upload: { onprogress: ((event: Partial<ProgressEvent>) => void) | null; onload: (() => void) | null } = {
    onprogress: null, onload: null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;

  open(method: string, url: string) { this.method = method; this.url = url; }
  setRequestHeader(name: string, value: string) { this.headers[name] = value; }
  send(body: unknown) { this.body = body; FakeXHR.last = this; }
  progress(loaded: number, total: number) { this.upload.onprogress?.({ lengthComputable: true, loaded, total }); }
  uploaded() { this.upload.onload?.(); }
  respond(status: number, body?: unknown) {
    this.status = status;
    this.responseText = body === undefined ? "" : JSON.stringify(body);
    this.onload?.();
  }
}

const DETAIL = `/policies/${policy.id}`;
const UPLOADED = { ...policyDocument, id: "44444444-4444-4444-8444-444444444444", original_filename: "wording.pdf",
  version: 3 };
const pdf = () => new File(["%PDF-1.7 test"], "wording.pdf", { type: "application/pdf" });

async function renderPolicy(role: "COMPANY_ADMIN" | "STAFF" = "COMPANY_ADMIN") {
  const api = mockApi({
    [`GET ${DETAIL}`]: { status: 200, body: policy },
    [`GET ${DETAIL}/documents`]: { status: 200, body: [policyDocument] },
  });
  demoUser(role);
  await renderApp(DETAIL);
  return api;
}

async function choose(file: File, options: { applyAccept?: boolean } = {}) {
  const user = userEvent.setup({ applyAccept: options.applyAccept ?? true });
  await user.upload(await screen.findByLabelText("PDF file"), file);
  await user.click(screen.getByRole("button", { name: "Upload and index" }));
  return user;
}

beforeEach(() => {
  FakeXHR.last = null;
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
});

describe("uploading a policy PDF", () => {
  it("suggests the next version of the document", () => {
    expect(nextVersion([policyDocument], "policy-wording")).toBe(3);
    expect(nextVersion([policyDocument], "endorsement")).toBe(1);
  });

  it("sends the PDF itself with the bearer token, shows each stage, then the indexed document", async () => {
    await renderPolicy();
    expect(await screen.findByLabelText("Version")).toHaveValue("3");

    await choose(pdf());

    const xhr = FakeXHR.last!;
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe(`/api${DETAIL}/documents/upload?filename=wording.pdf&document_key=policy-wording&version=3`);
    expect(xhr.headers).toMatchObject({ "Content-Type": "application/pdf", Authorization: `Bearer ${TOKEN}` });
    expect(xhr.body).toBeInstanceOf(File);
    expect(xhr.withCredentials).toBe(false);
    expect(xhr.url).not.toMatch(/company|tenant|role/);

    act(() => xhr.progress(50, 100));
    expect(screen.getByText("Uploading… 50%")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Uploading…" })).toBeDisabled();
    const pipeline = screen.getByRole("list", { name: "Indexing progress" });
    expect(pipeline).toHaveTextContent("In progress: Upload");
    expect(pipeline).toHaveTextContent("Waiting: Indexed");
    act(() => xhr.uploaded());
    expect(screen.getByText(/Uploaded\. Reading the text, splitting it into passages and indexing them/)).toBeInTheDocument();
    // The backend reports processing as one step: its parts are all in progress, none claimed done before the reply.
    expect(pipeline).toHaveTextContent("Done: Upload");
    for (const step of ["Extract pages", "Split into passages", "Embed"]) {
      expect(pipeline).toHaveTextContent(`In progress: ${step}`);
    }
    expect(pipeline).toHaveTextContent("Waiting: Indexed");
    act(() => xhr.respond(201, { document: UPLOADED, page_count: 4, chunk_count: 5 }));

    const ready = await screen.findByText(/Ready: wording\.pdf \(version 3\) is indexed, 4 pages in 5 passages\./);
    expect(within(ready).getByRole("link", { name: "search it" })).toHaveAttribute("href", "/search");
    expect(screen.getByRole("cell", { name: "wording.pdf" })).toBeInTheDocument();
    expect(screen.getByLabelText("Version")).toHaveValue("4");
  });

  it.each([
    [{ type: "no_text" }, "This PDF has no text layer (it looks scanned)."],
    [{ type: "unreadable_pdf" }, "This file isn't a readable PDF."],
  ])("explains a refused file (%o) without the server's text", async (item, message) => {
    await renderPolicy();
    await choose(pdf());

    act(() => FakeXHR.last!.respond(422, { detail: [{ ...item, loc: ["body", "file"], msg: "SERVER TEXT" }] }));

    expect(await screen.findByText(new RegExp(`Upload failed\\. ${message.replace(/[().]/g, "\\$&")}`))).toBeInTheDocument();
    expect(screen.queryByText(/SERVER TEXT/)).toBeNull();
  });

  it.each([
    [409, { detail: "This document version already exists" }, "This document version already exists. Choose another version number."],
    [413, { detail: "The request is too large" }, "The file is larger than the 25.0 MB limit."],
    [403, { detail: "Forbidden" }, "Your role doesn't allow this action."],
    [503, { detail: "Indexing is temporarily unavailable" }, "Indexing is unavailable right now."],
  ])("explains a %i", async (status, body, message) => {
    await renderPolicy();
    await choose(pdf());

    act(() => FakeXHR.last!.respond(status, body));

    expect(await screen.findByText(new RegExp(message.replace(/[().]/g, "\\$&")))).toBeInTheDocument();
  });

  it("accepts a PDF dropped on the upload area", async () => {
    await renderPolicy();
    const input = await screen.findByLabelText("PDF file");
    const zone = input.closest("label")!;

    fireEvent.dragOver(zone, { dataTransfer: { files: [pdf()] } });
    fireEvent.drop(zone, { dataTransfer: { files: [pdf()] } });
    expect(screen.getByText("wording.pdf")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Upload and index" }));

    expect(FakeXHR.last?.url).toContain("filename=wording.pdf");
  });

  it("refuses a file that is not a PDF before sending anything", async () => {
    await renderPolicy();

    await choose(new File(["hello"], "notes.txt", { type: "text/plain" }), { applyAccept: false });

    expect(screen.getByText("Choose a PDF file (.pdf).")).toBeInTheDocument();
    expect(FakeXHR.last).toBeNull();
  });

  it("asks for a file, and a valid document key and version", async () => {
    await renderPolicy();
    const user = userEvent.setup();
    await user.clear(await screen.findByLabelText("Document"));
    await user.type(screen.getByLabelText("Document"), "Bad Key");
    await user.clear(screen.getByLabelText("Version"));
    await user.type(screen.getByLabelText("Version"), "0");

    await user.click(screen.getByRole("button", { name: "Upload and index" }));

    expect(screen.getByText("Choose a PDF file.")).toBeInTheDocument();
    expect(screen.getByText("Use lower-case letters, digits and single hyphens.")).toBeInTheDocument();
    expect(screen.getByText("Use a whole number from 1.")).toBeInTheDocument();
    expect(FakeXHR.last).toBeNull();
  });

  it("says when the server can't be reached", async () => {
    await renderPolicy();
    await choose(pdf());

    act(() => FakeXHR.last!.onerror?.());

    expect(await screen.findByText(/Upload failed\. ClaimsRAG can't reach the server/)).toBeInTheDocument();
  });

  it("renews access if the server rejects the token during an upload, and asks to upload again", async () => {
    const { sessionRequests } = await renderPolicy();
    await choose(pdf());

    act(() => FakeXHR.last!.respond(401, { detail: "Invalid or expired token" }));

    expect(await screen.findByText(/Your access to the workspace had expired and has been renewed\. Try again\./))
      .toBeInTheDocument();
    await waitFor(() => expect(sessionRequests).toHaveLength(2)); // opened, then renewed
    expect(screen.getByRole("region", { name: "Upload a policy PDF" })).toBeInTheDocument(); // the page stays open
  });

  it("is not offered to staff", async () => {
    await renderPolicy("STAFF");

    expect(await screen.findByRole("cell", { name: "family-health-wording.pdf" })).toBeInTheDocument();
    expect(screen.queryByLabelText("PDF file")).toBeNull();
  });
});
