import type { ReactNode } from "react";
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { LoginPage, RegisterPage } from "./pages/AuthPages";
import { EventPage } from "./pages/EventPage";
import { EventsPage } from "./pages/EventsPage";
import { PurchasesPage } from "./pages/PurchasesPage";

function Header() {
  const { user, logout } = useAuth();
  return (
    <header className="site-header">
      <Link to="/" className="brand">
        Krunchbox Tickets
      </Link>
      <nav>
        <NavLink to="/" end>
          Events
        </NavLink>
        {user && <NavLink to="/purchases">My purchases</NavLink>}
      </nav>
      <div className="session">
        {user ? (
          <>
            <span className="muted">
              {user.full_name}
              {user.is_admin && <span className="tag">Admin</span>}
            </span>
            <button onClick={logout}>Log out</button>
          </>
        ) : (
          <>
            <Link to="/login">Log in</Link>
            <Link className="button primary" to="/register">
              Sign up
            </Link>
          </>
        )}
      </div>
    </header>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <p className="muted">Loading…</p>;
  if (!user) return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  return children;
}

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Header />
        <main>
          <Routes>
            <Route path="/" element={<EventsPage />} />
            <Route path="/events/:eventId" element={<EventPage />} />
            <Route
              path="/purchases"
              element={
                <RequireAuth>
                  <PurchasesPage />
                </RequireAuth>
              }
            />
            <Route path="/login" element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </AuthProvider>
    </BrowserRouter>
  );
}
