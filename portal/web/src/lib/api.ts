export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function parseResponse(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

type RequestInitJson = Omit<RequestInit, "body" | "headers"> & {
  json?: unknown;
  headers?: Record<string, string>;
};

export async function api<T = unknown>(
  path: string,
  init: RequestInitJson = {},
): Promise<T> {
  const { json, headers = {}, ...rest } = init;
  const opts: RequestInit = {
    ...rest,
    headers: {
      Accept: "application/json",
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
  };
  if (json !== undefined) opts.body = JSON.stringify(json);

  const res = await fetch(path, opts);
  const body = await parseResponse(res);
  if (!res.ok) {
    const detail =
      (body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : null) ?? `HTTP ${res.status}`;
    throw new ApiError(res.status, detail, body);
  }
  return body as T;
}

/** "HTTP 422 · detail", joining FastAPI's list of validation errors. */
export function errorText(e: unknown): string {
  if (!(e instanceof ApiError)) return e instanceof Error ? e.message : String(e);
  const detail = (e.body as { detail?: unknown } | null)?.detail;
  const text = Array.isArray(detail)
    ? detail.map((d: { loc?: unknown[]; msg?: string }) => [d.loc?.slice(1).join("."), d.msg].filter(Boolean).join(": ")).join("; ")
    : e.message;
  return `HTTP ${e.status} · ${text}`;
}
