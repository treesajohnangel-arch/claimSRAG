// One place for talking to the ClaimsRAG API: the base URL, the bearer token, JSON parsing and error mapping.
// Server error text is never shown as-is: every error becomes an ApiError with a fixed, user-facing message, and
// field errors are named by field only. A 401 on an authenticated request means the token was rejected: whoever
// registered onRejected (the SessionProvider) renews it, and the request is retried once with the new token.

export type ErrorKind =
  | "too_large"
  | "validation"
  | "unauthenticated"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "unavailable"
  | "server"
  | "network";

export class ApiError extends Error {
  readonly status: number;
  readonly kind: ErrorKind;
  /** Request fields the server rejected, by field name, with a user-facing message. */
  readonly fieldErrors: Record<string, string>;
  /** The server's fixed reason identifier per rejected field (e.g. "no_text"), never its prose. */
  readonly fieldCodes: Record<string, string>;

  constructor(status: number, kind: ErrorKind, message: string, fieldErrors: Record<string, string> = {},
    fieldCodes: Record<string, string> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
    this.fieldErrors = fieldErrors;
    this.fieldCodes = fieldCodes;
  }
}

export const MESSAGES = {
  network: "ClaimsRAG can't reach the server. Check your connection and try again.",
  unauthenticated: "Your access to the workspace had expired and has been renewed. Try again.",
  forbidden: "Your role doesn't allow this action. Ask a company admin if you need it.",
  not_found: "This item doesn't exist, or it isn't in your company's workspace.",
  conflict: "This change conflicts with existing data.",
  validation: "Some details aren't valid. Check the highlighted fields.",
  too_large: "The file is too large to upload.",
  unavailable: "This service is temporarily unavailable. Try again in a moment.",
  server: "Something went wrong on the server. Try again; if it keeps happening, contact your administrator.",
} as const satisfies Record<ErrorKind, string>;

// The backend's fixed 409 messages (app/api/policies.py) are safe to show, with clearer wording here.
const CONFLICT_MESSAGES: Record<string, string> = {
  "A policy with this number already exists": "A policy with this number already exists in your workspace.",
  "This document version already exists": "This document version already exists.",
  "The policy still has documents": "This policy still has documents. Delete or move them before deleting it.",
  "The document still has embeddings":
    "This document is indexed for search, so it can't be deleted. Withdraw it instead to stop it being used.",
};

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "/api";

let tokenSource: () => string | null = () => null;
let rejectedHandler: (rejectedToken: string | null) => Promise<boolean> = () => Promise.resolve(false);

/** Called by the SessionProvider: where the client reads the token, and how it renews a token the server rejected
 * (resolving true when a new token is ready). */
export function configureClient(getToken: () => string | null,
  onRejected: (rejectedToken: string | null) => Promise<boolean>): void {
  tokenSource = getToken;
  rejectedHandler = onRejected;
}

function kindFor(status: number): ErrorKind {
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 413) return "too_large";
  if (status === 400 || status === 422) return "validation";
  if (status === 503) return "unavailable";
  return "server";
}

function fieldErrorsFrom(detail: unknown): [Record<string, string>, Record<string, string>] {
  const fields: Record<string, string> = {};
  const codes: Record<string, string> = {};
  if (!Array.isArray(detail)) return [fields, codes];
  for (const item of detail) {
    const { loc, type } = item as { loc?: unknown; type?: unknown };
    if (Array.isArray(loc) && loc.length > 1 && loc[0] === "body") {
      const field = String(loc[loc.length - 1]);
      fields[field] = "Enter a valid value.";
      if (typeof type === "string" && /^[a-z_]{1,40}$/.test(type)) codes[field] = type;
    }
  }
  return [fields, codes];
}

async function errorFrom(response: Response): Promise<ApiError> {
  const kind = kindFor(response.status);
  let detail: unknown;
  try {
    detail = ((await response.json()) as { detail?: unknown }).detail;
  } catch {
    detail = undefined;
  }
  if (kind === "conflict" && typeof detail === "string" && detail in CONFLICT_MESSAGES) {
    return new ApiError(response.status, kind, CONFLICT_MESSAGES[detail] ?? MESSAGES.conflict);
  }
  if (kind === "validation") {
    return new ApiError(response.status, kind, MESSAGES.validation, ...fieldErrorsFrom(detail));
  }
  return new ApiError(response.status, kind, MESSAGES[kind]);
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Opening the demo session sends no token, and is never retried. */
  anonymous?: boolean;
}

export function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return send<T>(path, options, !options.anonymous);
}

async function send<T>(path: string, options: RequestOptions, retryOnRejection: boolean): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const token = options.anonymous ? null : tokenSource();
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
      credentials: "omit",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network", MESSAGES.network);
  }

  if (!response.ok) {
    const error = await errorFrom(response);
    if (error.kind === "unauthenticated" && retryOnRejection && await rejectedHandler(token)) {
      return send<T>(path, options, false);
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(response.status, "server", MESSAGES.server);
  }
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** Upload a file as the raw request body, reporting progress: "uploading" with a fraction, then "processing" once
 * the server has the whole file and is indexing it. XMLHttpRequest is used because fetch cannot report upload
 * progress. Errors are mapped exactly as for every other request; a rejected token is renewed, but the file is not
 * sent again by itself (the user is asked to try again). */
export function uploadFile<T>(path: string, file: Blob, contentType: string,
  onProgress: (stage: "uploading" | "processing", fraction: number) => void): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE_URL}${path}`);
    xhr.setRequestHeader("Content-Type", contentType);
    xhr.setRequestHeader("Accept", "application/json");
    const token = tokenSource();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.withCredentials = false;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress("uploading", event.loaded / event.total);
    };
    xhr.upload.onload = () => onProgress("processing", 1);
    xhr.onerror = () => reject(new ApiError(0, "network", MESSAGES.network));
    xhr.onload = async () => {
      const response = new Response(xhr.responseText || null, {
        status: xhr.status, headers: { "Content-Type": "application/json" },
      });
      if (xhr.status < 200 || xhr.status >= 300) {
        const error = await errorFrom(response);
        if (error.kind === "unauthenticated") void rejectedHandler(token);
        reject(error);
        return;
      }
      try {
        resolve((await response.json()) as T);
      } catch {
        reject(new ApiError(xhr.status, "server", MESSAGES.server));
      }
    };
    onProgress("uploading", 0);
    xhr.send(file);
  });
}

