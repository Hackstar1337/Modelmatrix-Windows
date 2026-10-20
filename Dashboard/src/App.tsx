import { useEffect, useState } from "react";
import { KeyRound, Loader2 } from "lucide-react";
import { api, ApiError, post } from "./lib/api";
import { AppProvider, useApp } from "./lib/state";
import { Header, KpiStrip, Rail, SubNav } from "./components/Shell";
import { MatrixMark, Notice, Toasts } from "./components/ui";
import { OptimizeHost } from "./components/Optimize";
import { HuggingFaceHost } from "./components/HuggingFace";
import { HFDataProvider } from "./components/Downloads";
import { OverviewPage } from "./pages/Overview";
import { ServersPage } from "./pages/Servers";
import { ClientsPage } from "./pages/Clients";
import { ModelsPage } from "./pages/Models";
import { RequestsPage } from "./pages/Requests";
import { SettingsPage } from "./pages/Settings";

function Login({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="login">
      <form
        className="card card-pad stack"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            await post("/api/dashboard/session", { token: token.trim() });
            onDone();
          } catch (err) {
            setError((err as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="row" style={{ gap: 12 }}>
          <MatrixMark size={36} />
          <div>
            <div className="brand-title" style={{ fontSize: 20 }}>
              ModelMatrix
            </div>
            <div className="brand-sub">Dashboard sign in</div>
          </div>
        </div>
        <Notice>This controller is protected by an access token. Find it in the ModelMatrix desktop app or controller settings.</Notice>
        <div className="field">
          <label>Access token</label>
          <input className="input mono" type="password" autoFocus value={token} onChange={(e) => setToken(e.target.value)} />
        </div>
        {error && <div className="red">{error}</div>}
        <button className="btn primary" disabled={busy || !token.trim()}>
          {busy ? <Loader2 size={14} className="spin" /> : <KeyRound size={14} />} Sign in
        </button>
      </form>
    </div>
  );
}

function Dashboard() {
  const { page, statusError } = useApp();
  const [editorRequest, setEditorRequest] = useState(false);
  const { setPage } = useApp();
  const newProfile = () => {
    setPage("servers");
    setEditorRequest(true);
  };
  return (
    <HFDataProvider>
    <div className="app">
      <Header />
      <KpiStrip />
      <div className="body">
        <Rail />
        <SubNav onNewProfile={newProfile} />
        <main className="content" key={page}>
          {statusError?.status === 0 && (
            <div style={{ marginBottom: 18 }}>
              <Notice kind="bad">Lost connection to the ModelMatrix controller. Retrying…</Notice>
            </div>
          )}
          {page === "overview" && <OverviewPage />}
          {page === "servers" && <ServersPage editorRequest={editorRequest} clearEditorRequest={() => setEditorRequest(false)} />}
          {page === "clients" && <ClientsPage />}
          {page === "models" && <ModelsPage onNewProfile={newProfile} />}
          {page === "requests" && <RequestsPage />}
          {page === "settings" && <SettingsPage />}
        </main>
      </div>
      <OptimizeHost />
      <HuggingFaceHost />
      <Toasts />
    </div>
    </HFDataProvider>
  );
}

export function App() {
  const [state, setState] = useState<"checking" | "login" | "ready" | "offline">("checking");
  const check = async () => {
    try {
      const session = await api<{ authenticated: boolean; token_required: boolean }>("/api/dashboard/session");
      if (session.authenticated) return setState("ready");
      if (!session.token_required) {
        await post("/api/dashboard/session", {});
        return setState("ready");
      }
      setState("login");
    } catch (e) {
      setState(e instanceof ApiError && e.status === 401 ? "login" : "offline");
    }
  };
  useEffect(() => {
    check();
  }, []);
  useEffect(() => {
    if (state !== "offline") return;
    const id = window.setInterval(check, 3000);
    return () => window.clearInterval(id);
  }, [state]);

  if (state === "checking") {
    return (
      <div className="login">
        <Loader2 className="spin accent" />
      </div>
    );
  }
  if (state === "login") return <Login onDone={() => setState("ready")} />;
  if (state === "offline") {
    return (
      <div className="login">
        <div className="card card-pad stack" style={{ width: 420 }}>
          <div className="row" style={{ gap: 12 }}>
            <MatrixMark size={36} />
            <div className="brand-title" style={{ fontSize: 20 }}>
              ModelMatrix
            </div>
          </div>
          <Notice kind="warn">The ModelMatrix controller is not responding. Start the local ModelMatrix controller or desktop app. This page reconnects automatically.</Notice>
        </div>
      </div>
    );
  }
  return (
    <AppProvider>
      <Dashboard />
    </AppProvider>
  );
}
