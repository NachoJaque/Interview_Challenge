// Typed client for the ticketing API. Amounts arrive as decimal strings
// ("150.00") and are only converted to numbers for display.

export interface User {
  id: number;
  email: string;
  full_name: string;
  is_admin: boolean;
}

export interface TicketType {
  id: number;
  name: string;
  price: string;
  capacity: number;
  available: number;
  /** Sales breakdown: null unless the logged-in user is an admin. */
  held: number | null;
  sold: number | null;
  is_refundable: boolean;
}

export interface EventSummary {
  id: number;
  name: string;
  description: string | null;
  venue: string;
  starts_at: string;
  ticket_types: TicketType[];
}

export type PurchaseStatus =
  | "pending"
  | "completed"
  | "cancelled"
  | "expired"
  | "partially_refunded"
  | "refunded";

export interface PurchaseItem {
  id: number;
  ticket_type_id: number;
  ticket_type_name: string;
  quantity: number;
  refunded_quantity: number;
  refundable_quantity: number;
  unit_price: string;
  is_refundable: boolean;
}

export interface Refund {
  id: number;
  amount: string;
  refund_percentage: number;
  reason: string | null;
  created_at: string;
  items: { purchase_item_id: number; quantity: number; amount: string }[];
}

export interface Purchase {
  id: number;
  event_id: number;
  event_name: string;
  event_starts_at: string;
  status: PurchaseStatus;
  total_amount: string;
  refunded_amount: string;
  created_at: string;
  expires_at: string;
  confirmed_at: string | null;
  items: PurchaseItem[];
  refunds: Refund[];
  refund_policy: { allowed: boolean; percentage: number; reason: string };
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
}

// sessionStorage keeps the session per browser tab, so two tabs can be logged
// in as two different customers (handy for showing them compete for tickets),
// and the token is gone when the tab closes.
const TOKEN_KEY = "krunchbox.token";
export const SESSION_EXPIRED_EVENT = "krunchbox:session-expired";

export const getToken = () => sessionStorage.getItem(TOKEN_KEY);
export const setToken = (token: string) => sessionStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => sessionStorage.removeItem(TOKEN_KEY);

export function errorMessage(status: number, body: unknown): { message: string; code?: string } {
  if (body && typeof body === "object") {
    const { error, detail } = body as {
      error?: { code: string; message: string };
      detail?: string | { msg: string; loc?: (string | number)[] }[];
    };
    if (error) return { message: error.message, code: error.code };
    if (typeof detail === "string") return { message: detail };
    if (Array.isArray(detail) && detail.length > 0) {
      const field = detail[0].loc?.slice(1).join(".");
      return { message: field ? `${field}: ${detail[0].msg}` : detail[0].msg };
    }
  }
  // 429 without a body comes from the web server's rate limit.
  if (status === 429) return { message: "Too many attempts. Please wait a minute and try again." };
  return { message: "Something went wrong. Please try again." };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`/api${path}`, { ...init, headers });
  } catch {
    throw new ApiError("Cannot reach the server. Check your connection.", 0);
  }

  if (response.status === 401 && token) {
    clearToken();
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const { message, code } = errorMessage(response.status, body);
    throw new ApiError(message, response.status, code);
  }
  return body as T;
}

const postJson = <T>(path: string, payload?: unknown, headers: Record<string, string> = {}) =>
  request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });

export const api = {
  register: (email: string, full_name: string, password: string) =>
    postJson<User>("/auth/register", { email, full_name, password }),

  login: (email: string, password: string) =>
    request<{ access_token: string }>("/auth/login", {
      method: "POST",
      body: new URLSearchParams({ username: email, password }),
    }),

  me: () => request<User>("/auth/me"),

  events: () => request<EventSummary[]>("/events"),
  event: (id: number) => request<EventSummary>(`/events/${id}`),

  addTicketType: (
    eventId: number,
    ticketType: { name: string; price: string; capacity: number; is_refundable: boolean },
  ) => postJson<TicketType>(`/events/${eventId}/ticket-types`, ticketType),

  // The idempotency key makes a retry after a network failure return the same
  // reservation (or refund) instead of holding or refunding the tickets twice.
  reserve: (eventId: number, items: { ticket_type_id: number; quantity: number }[], idempotencyKey: string) =>
    postJson<Purchase>(`/events/${eventId}/purchases`, { items }, { "Idempotency-Key": idempotencyKey }),

  purchases: () => request<Purchase[]>("/purchases"),
  confirm: (purchaseId: number) => postJson<Purchase>(`/purchases/${purchaseId}/confirm`),
  cancel: (purchaseId: number) => postJson<Purchase>(`/purchases/${purchaseId}/cancel`),
  refund: (
    purchaseId: number,
    items: { purchase_item_id: number; quantity: number }[],
    reason: string,
    idempotencyKey: string,
  ) =>
    postJson<Purchase>(
      `/purchases/${purchaseId}/refunds`,
      { items, reason: reason.trim() || null },
      { "Idempotency-Key": idempotencyKey },
    ),
};
