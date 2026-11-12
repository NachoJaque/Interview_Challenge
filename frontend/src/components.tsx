import { useState } from "react";
import { api, type Purchase, type PurchaseStatus, type TicketType } from "./api";
import { clock, money } from "./format";
import { useSecondsUntil } from "./hooks";

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p className="banner banner-error" role="alert">
      {message}
    </p>
  );
}

export function Stepper({
  value,
  max,
  onChange,
  label,
}: {
  value: number;
  max: number;
  onChange: (value: number) => void;
  label: string;
}) {
  return (
    <div className="stepper" role="group" aria-label={label}>
      <button type="button" onClick={() => onChange(value - 1)} disabled={value <= 0} aria-label={`Fewer ${label}`}>
        −
      </button>
      <output aria-live="polite">{value}</output>
      <button type="button" onClick={() => onChange(value + 1)} disabled={value >= max} aria-label={`More ${label}`}>
        +
      </button>
    </div>
  );
}

/**
 * Capacity bar. Admins get the breakdown (sold, then reserved but not yet
 * paid, then free); everyone else sees only how much is taken.
 */
export function AvailabilityBar({ ticketType }: { ticketType: TicketType }) {
  const { capacity, available, held, sold } = ticketType;
  const percent = (count: number) => (capacity === 0 ? 0 : (count / capacity) * 100);
  if (held === null || sold === null) {
    return (
      <div className="bar" title={`${available} of ${capacity} available`}>
        <span className="bar-sold" style={{ width: `${percent(capacity - available)}%` }} />
      </div>
    );
  }
  return (
    <div className="bar" title={`${sold} sold · ${held} reserved · ${available} available`}>
      <span className="bar-sold" style={{ width: `${percent(sold)}%` }} />
      <span className="bar-held" style={{ width: `${percent(held)}%` }} />
    </div>
  );
}

const STATUS_LABELS: Record<PurchaseStatus, string> = {
  pending: "Reserved",
  completed: "Completed",
  cancelled: "Cancelled",
  expired: "Expired",
  partially_refunded: "Partially refunded",
  refunded: "Refunded",
};

export function StatusBadge({ status }: { status: PurchaseStatus }) {
  return <span className={`badge badge-${status}`}>{STATUS_LABELS[status]}</span>;
}

/**
 * A pending reservation: what is held, how long is left, and the two ways out
 * (confirm or cancel). `onChange` fires after either, and when the hold runs out.
 */
export function PendingCheckout({ purchase, onChange }: { purchase: Purchase; onChange: () => void }) {
  const secondsLeft = useSecondsUntil(purchase.expires_at);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const expired = secondsLeft === 0;

  const run = async (action: (id: number) => Promise<Purchase>) => {
    setBusy(true);
    setError(null);
    try {
      await action(purchase.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
      onChange();
    }
  };

  return (
    <section className="checkout" aria-label="Your reservation">
      <header className="checkout-header">
        <h3>Your tickets are reserved</h3>
        <span className={`countdown ${secondsLeft <= 60 ? "countdown-urgent" : ""}`} aria-live="off">
          {expired ? "Time is up" : clock(secondsLeft)}
        </span>
      </header>
      <p className="muted">
        {expired
          ? "This reservation has expired and its tickets are being released."
          : "They are held for you until the timer runs out. Confirm to complete the purchase."}
      </p>
      <ul className="lines">
        {purchase.items.map((item) => (
          <li key={item.id}>
            <span>
              {item.quantity} × {item.ticket_type_name}
            </span>
            <span>{money(Number(item.unit_price) * item.quantity)}</span>
          </li>
        ))}
        <li className="lines-total">
          <span>Total</span>
          <span>{money(purchase.total_amount)}</span>
        </li>
      </ul>
      <ErrorBanner message={error} />
      <div className="actions">
        <button className="primary" disabled={busy || expired} onClick={() => run(api.confirm)}>
          Confirm purchase
        </button>
        <button disabled={busy || expired} onClick={() => run(api.cancel)}>
          Cancel reservation
        </button>
      </div>
    </section>
  );
}
