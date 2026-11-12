import { Link } from "react-router-dom";
import { api } from "../api";
import { AvailabilityBar, ErrorBanner } from "../components";
import { formatDate, money, timeUntil } from "../format";
import { usePolling } from "../hooks";

export const REFRESH_MS = 3000;

export function EventsPage() {
  const { data: events, error } = usePolling(api.events, REFRESH_MS);

  return (
    <>
      <div className="page-title">
        <h1>Upcoming events</h1>
        <span className="live" title="Availability refreshes every few seconds">
          Live availability
        </span>
      </div>
      <ErrorBanner message={error} />
      {events === null && !error && <p className="muted">Loading events…</p>}
      {events?.length === 0 && <p className="muted">No events are on sale right now.</p>}

      <div className="grid">
        {events?.map((event) => {
          const started = new Date(event.starts_at).getTime() <= Date.now();
          const soldOut = event.ticket_types.every((tt) => tt.available === 0);
          return (
            <article key={event.id} className="card">
              <h2>{event.name}</h2>
              <p className="muted">
                {event.venue} · {formatDate(event.starts_at)}
                <br />
                {timeUntil(event.starts_at)}
              </p>
              <ul className="ticket-list">
                {event.ticket_types.map((tt) => (
                  <li key={tt.id}>
                    <div className="ticket-row">
                      <strong>{tt.name}</strong>
                      <span>{money(tt.price)}</span>
                      <span className={tt.available === 0 ? "sold-out" : "muted"}>
                        {tt.available === 0 ? "Sold out" : `${tt.available} left`}
                      </span>
                    </div>
                    <AvailabilityBar ticketType={tt} />
                  </li>
                ))}
              </ul>
              <Link className="button primary" to={`/events/${event.id}`}>
                {started ? "View event" : soldOut ? "View (sold out)" : "Get tickets"}
              </Link>
            </article>
          );
        })}
      </div>
    </>
  );
}
