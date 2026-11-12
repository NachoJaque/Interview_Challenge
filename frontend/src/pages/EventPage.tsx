import { useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { api, type EventSummary } from "../api";
import { useAuth } from "../auth";
import { AvailabilityBar, ErrorBanner, PendingCheckout, Stepper } from "../components";
import { formatDate, money, timeUntil } from "../format";
import { usePolling } from "../hooks";
import { REFRESH_MS } from "./EventsPage";

const MAX_TICKETS_PER_PURCHASE = 10; // mirrors the API rule; the API is the one that enforces it

export function EventPage() {
  const eventId = Number(useParams().eventId);
  const { user } = useAuth();
  const location = useLocation();

  const event = usePolling(() => api.event(eventId), REFRESH_MS);
  const purchases = usePolling(api.purchases, REFRESH_MS, user !== null);

  const [quantities, setQuantities] = useState<Record<number, number>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [justBought, setJustBought] = useState(false);
  // One key per checkout attempt: a retry of the same attempt reuses it.
  const idempotencyKey = useRef(crypto.randomUUID());

  if (event.data === null) {
    return event.error ? <ErrorBanner message={event.error} /> : <p className="muted">Loading event…</p>;
  }

  const pending = purchases.data?.find((p) => p.event_id === eventId && p.status === "pending");
  const started = new Date(event.data.starts_at).getTime() <= Date.now();
  const totalQuantity = Object.values(quantities).reduce((sum, quantity) => sum + quantity, 0);
  const totalPrice = event.data.ticket_types.reduce(
    (sum, tt) => sum + Number(tt.price) * (quantities[tt.id] ?? 0),
    0,
  );

  const refreshAll = () => {
    void event.refresh();
    void purchases.refresh();
  };

  const reserve = async () => {
    setBusy(true);
    setError(null);
    setJustBought(false);
    try {
      const items = Object.entries(quantities)
        .filter(([, quantity]) => quantity > 0)
        .map(([id, quantity]) => ({ ticket_type_id: Number(id), quantity }));
      await api.reserve(eventId, items, idempotencyKey.current);
      setQuantities({});
      idempotencyKey.current = crypto.randomUUID();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reserve the tickets");
    } finally {
      setBusy(false);
      refreshAll();
    }
  };

  const onCheckoutChange = async () => {
    // Read the outcome before refreshing, since a confirmed purchase stops being "pending".
    const latest = await api.purchases().catch(() => []);
    setJustBought(latest.find((p) => p.id === pending?.id)?.status === "completed");
    refreshAll();
  };

  return (
    <>
      <Link to="/" className="back">
        ← All events
      </Link>
      <div className="page-title">
        <h1>{event.data.name}</h1>
        <span className="live">Live availability</span>
      </div>
      <p className="muted">
        {event.data.venue} · {formatDate(event.data.starts_at)} · {timeUntil(event.data.starts_at)}
      </p>
      {event.data.description && <p>{event.data.description}</p>}

      {justBought && (
        <p className="banner banner-success" role="status">
          Purchase complete. Your tickets are in <Link to="/purchases">My purchases</Link>.
        </p>
      )}

      {pending ? (
        <PendingCheckout purchase={pending} onChange={onCheckoutChange} />
      ) : (
        <section className="card">
          <h2>Choose your tickets</h2>
          <ul className="ticket-list">
            {event.data.ticket_types.map((tt) => {
              const quantity = quantities[tt.id] ?? 0;
              const roomInPurchase = MAX_TICKETS_PER_PURCHASE - (totalQuantity - quantity);
              return (
                <li key={tt.id}>
                  <div className="ticket-row">
                    <div>
                      <strong>{tt.name}</strong>
                      {!tt.is_refundable && <span className="tag">Non-refundable</span>}
                      <div className={tt.available === 0 ? "sold-out" : "muted"}>
                        {tt.available === 0 ? "Sold out" : `${tt.available} of ${tt.capacity} left`}
                        {tt.held > 0 && ` · ${tt.held} reserved by buyers right now`}
                      </div>
                    </div>
                    <span>{money(tt.price)}</span>
                    <Stepper
                      label={`${tt.name} tickets`}
                      value={quantity}
                      max={started ? 0 : Math.min(tt.available, roomInPurchase)}
                      onChange={(value) => setQuantities({ ...quantities, [tt.id]: value })}
                    />
                  </div>
                  <AvailabilityBar ticketType={tt} />
                </li>
              );
            })}
          </ul>

          <ErrorBanner message={error} />
          <div className="summary">
            <span>
              {totalQuantity} ticket{totalQuantity === 1 ? "" : "s"} · <strong>{money(totalPrice)}</strong>
            </span>
            {started ? (
              <span className="muted">Sales are closed: the event has started.</span>
            ) : user ? (
              <button className="primary" disabled={busy || totalQuantity === 0} onClick={reserve}>
                Reserve tickets
              </button>
            ) : (
              <Link className="button primary" to="/login" state={{ from: location.pathname }}>
                Log in to buy
              </Link>
            )}
          </div>
          <p className="muted small">
            Up to {MAX_TICKETS_PER_PURCHASE} tickets per purchase. Reserving holds your tickets for a few minutes
            while you confirm.
          </p>
        </section>
      )}

      {user?.is_admin && <AddTicketTypeForm event={event.data} onAdded={refreshAll} />}
    </>
  );
}

/** Admin only: a new ticket category is just a new row, no deploy needed. */
function AddTicketTypeForm({ event, onAdded }: { event: EventSummary; onAdded: () => void }) {
  const [name, setName] = useState("");
  const [price, setPrice] = useState("");
  const [capacity, setCapacity] = useState("");
  const [refundable, setRefundable] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.addTicketType(event.id, { name, price, capacity: Number(capacity), is_refundable: refundable });
      setName("");
      setPrice("");
      setCapacity("");
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the ticket type");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card admin">
      <h2>Admin: add a ticket type</h2>
      <form className="inline-form" onSubmit={submit}>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={50} required placeholder="Platinum" />
        </label>
        <label>
          Price
          <input
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            type="number"
            min="0"
            step="0.01"
            required
            placeholder="250.00"
          />
        </label>
        <label>
          Capacity
          <input
            value={capacity}
            onChange={(e) => setCapacity(e.target.value)}
            type="number"
            min="0"
            step="1"
            required
            placeholder="20"
          />
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={refundable} onChange={(e) => setRefundable(e.target.checked)} />
          Refundable
        </label>
        <button className="primary" disabled={busy}>
          Add ticket type
        </button>
      </form>
      <ErrorBanner message={error} />
    </section>
  );
}
