import { useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Purchase } from "../api";
import { ErrorBanner, PendingCheckout, StatusBadge, Stepper } from "../components";
import { formatDate, money, refundPerTicket } from "../format";
import { usePolling } from "../hooks";
import { REFRESH_MS } from "./EventsPage";

export function PurchasesPage() {
  const { data: purchases, error, refresh } = usePolling(api.purchases, REFRESH_MS);

  return (
    <>
      <div className="page-title">
        <h1>My purchases</h1>
      </div>
      <ErrorBanner message={error} />
      {purchases === null && !error && <p className="muted">Loading your purchases…</p>}
      {purchases?.length === 0 && (
        <p className="muted">
          You have not bought any tickets yet. <Link to="/">Browse events</Link>.
        </p>
      )}
      <div className="stack">
        {purchases?.map((purchase) => (
          <PurchaseCard key={purchase.id} purchase={purchase} onChange={() => void refresh()} />
        ))}
      </div>
    </>
  );
}

function PurchaseCard({ purchase, onChange }: { purchase: Purchase; onChange: () => void }) {
  const [refunding, setRefunding] = useState(false);
  const inactive = purchase.status === "cancelled" || purchase.status === "expired";
  const refundable = purchase.status === "completed" || purchase.status === "partially_refunded";

  return (
    <article className={`card ${inactive ? "card-inactive" : ""}`}>
      <header className="purchase-header">
        <div>
          <h2>
            <Link to={`/events/${purchase.event_id}`}>{purchase.event_name}</Link>
          </h2>
          <p className="muted">
            Event: {formatDate(purchase.event_starts_at)} · Order #{purchase.id} · {formatDate(purchase.created_at)}
          </p>
        </div>
        <StatusBadge status={purchase.status} />
      </header>

      {purchase.status === "pending" ? (
        <PendingCheckout purchase={purchase} onChange={onChange} />
      ) : (
        <>
          <ul className="lines">
            {purchase.items.map((item) => (
              <li key={item.id}>
                <span>
                  {item.quantity} × {item.ticket_type_name} at {money(item.unit_price)}
                  {item.refunded_quantity > 0 && <span className="muted"> ({item.refunded_quantity} refunded)</span>}
                </span>
                <span>{money(Number(item.unit_price) * item.quantity)}</span>
              </li>
            ))}
            <li className="lines-total">
              <span>Total</span>
              <span>{money(purchase.total_amount)}</span>
            </li>
            {Number(purchase.refunded_amount) > 0 && (
              <li className="lines-refunded">
                <span>Refunded</span>
                <span>−{money(purchase.refunded_amount)}</span>
              </li>
            )}
          </ul>

          {purchase.refunds.length > 0 && (
            <details>
              <summary>Refund history ({purchase.refunds.length})</summary>
              <ul className="history">
                {purchase.refunds.map((refund) => (
                  <li key={refund.id}>
                    {formatDate(refund.created_at)}: {refund.items.reduce((n, i) => n + i.quantity, 0)} ticket(s),{" "}
                    {money(refund.amount)} at {refund.refund_percentage}%
                    {refund.reason && <span className="muted"> — “{refund.reason}”</span>}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {refundable && (
            <div className="refund-area">
              <p className={purchase.refund_policy.allowed ? "policy" : "policy policy-closed"}>
                {purchase.refund_policy.reason}
              </p>
              {purchase.refund_policy.allowed && !refunding && (
                <button onClick={() => setRefunding(true)}>Refund tickets</button>
              )}
              {purchase.refund_policy.allowed && refunding && (
                <RefundForm
                  purchase={purchase}
                  onDone={() => {
                    setRefunding(false);
                    onChange();
                  }}
                  onCancel={() => setRefunding(false)}
                />
              )}
            </div>
          )}
        </>
      )}
    </article>
  );
}

function RefundForm({
  purchase,
  onDone,
  onCancel,
}: {
  purchase: Purchase;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [quantities, setQuantities] = useState<Record<number, number>>({});
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // One key per refund attempt: retrying after a lost response never refunds twice.
  const idempotencyKey = useRef(crypto.randomUUID());

  const percentage = purchase.refund_policy.percentage;
  const totalQuantity = Object.values(quantities).reduce((sum, quantity) => sum + quantity, 0);
  // For display only: the API calculates the amount actually refunded. Like
  // the API, it rounds the refund of one ticket to the cent, then multiplies.
  const estimate = purchase.items.reduce(
    (sum, item) => sum + refundPerTicket(item.unit_price, percentage) * (quantities[item.id] ?? 0),
    0,
  );

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const items = Object.entries(quantities)
        .filter(([, quantity]) => quantity > 0)
        .map(([id, quantity]) => ({ purchase_item_id: Number(id), quantity }));
      await api.refund(purchase.id, items, reason, idempotencyKey.current);
      onDone();
    } catch (err) {
      // A rejected request refunded nothing, so the next attempt is a new one;
      // after a network failure the key is kept, which makes the retry safe.
      if (err instanceof ApiError && err.status !== 0) idempotencyKey.current = crypto.randomUUID();
      setError(err instanceof Error ? err.message : "Could not process the refund");
      setBusy(false);
    }
  };

  return (
    <form className="refund-form" onSubmit={submit}>
      <ul className="ticket-list">
        {purchase.items.map((item) => (
          <li key={item.id} className="ticket-row">
            <div>
              <strong>{item.ticket_type_name}</strong>
              <div className="muted">
                {!item.is_refundable
                  ? "Non-refundable ticket type"
                  : item.refundable_quantity === 0
                    ? "Already refunded"
                    : `${item.refundable_quantity} can be refunded`}
              </div>
            </div>
            <span>{money(item.unit_price)}</span>
            <Stepper
              label={`${item.ticket_type_name} tickets to refund`}
              value={quantities[item.id] ?? 0}
              max={item.is_refundable ? item.refundable_quantity : 0}
              onChange={(value) => setQuantities({ ...quantities, [item.id]: value })}
            />
          </li>
        ))}
      </ul>
      <label>
        Reason (optional)
        <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
      </label>
      <ErrorBanner message={error} />
      <div className="summary">
        <span>
          You will get back <strong>{money(estimate)}</strong> ({percentage}% of the price paid)
        </span>
        <div className="actions">
          <button type="button" onClick={onCancel} disabled={busy}>
            Keep tickets
          </button>
          <button className="primary danger" disabled={busy || totalQuantity === 0}>
            Refund {totalQuantity} ticket{totalQuantity === 1 ? "" : "s"}
          </button>
        </div>
      </div>
    </form>
  );
}
