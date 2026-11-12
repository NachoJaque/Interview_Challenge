import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { ErrorBanner } from "../components";

function useRedirectAfterAuth() {
  const navigate = useNavigate();
  const from = (useLocation().state as { from?: string } | null)?.from;
  // Only ever return to a path inside this app, never to an external URL.
  const target = from?.startsWith("/") && !from.startsWith("//") ? from : "/";
  return { state: { from: target }, go: () => navigate(target, { replace: true }) };
}

export function LoginPage() {
  const { login } = useAuth();
  const redirect = useRedirectAfterAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      redirect.go();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not log in");
      setBusy(false);
    }
  };

  return (
    <form className="card auth-form" onSubmit={submit}>
      <h1>Log in</h1>
      <label>
        Email
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
      </label>
      <ErrorBanner message={error} />
      <button className="primary" disabled={busy}>
        Log in
      </button>
      <p className="muted">
        New here?{" "}
        <Link to="/register" state={redirect.state}>
          Create an account
        </Link>
      </p>
    </form>
  );
}

export function RegisterPage() {
  const { register } = useAuth();
  const redirect = useRedirectAfterAuth();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await register(email, fullName, password);
      redirect.go();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the account");
      setBusy(false);
    }
  };

  return (
    <form className="card auth-form" onSubmit={submit}>
      <h1>Create an account</h1>
      <label>
        Full name
        <input
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
          autoComplete="name"
          maxLength={120}
          required
        />
      </label>
      <label>
        Email
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          minLength={8}
          maxLength={72}
          required
        />
        <span className="muted small">At least 8 characters.</span>
      </label>
      <ErrorBanner message={error} />
      <button className="primary" disabled={busy}>
        Create account
      </button>
      <p className="muted">
        Already have an account?{" "}
        <Link to="/login" state={redirect.state}>
          Log in
        </Link>
      </p>
    </form>
  );
}
