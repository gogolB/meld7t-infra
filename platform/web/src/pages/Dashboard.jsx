import React from "react";
import { Link } from "react-router-dom";
import { api } from "../api.js";
import { Badge, useAsync, ErrorBox } from "../components.jsx";

export default function Dashboard() {
  const sys = useAsync(() => api.system(), [], 4000);
  const q = useAsync(() => api.queue(), [], 4000);
  const cases = useAsync(() => api.listCases(), [], 6000);
  const audit = useAsync(() => api.auditVerify(), []);

  return (
    <div>
      <h1>Dashboard</h1>
      <p className="muted">Live status of cases, the GPU queue, and the audit ledger.</p>
      <ErrorBox error={sys.error} />

      <div className="tiles">
        <div className="tile"><b>{sys.data?.cases ?? "–"}</b>cases</div>
        <div className="tile"><b>{sys.data?.runs?.total ?? "–"}</b>runs</div>
        <div className="tile">
          <b className={q.data?.in_use_run ? "" : "muted"}>{q.data?.in_use_run ? "busy" : "idle"}</b>
          GPU {q.data?.paused ? "(paused)" : ""}
        </div>
        <div className="tile">
          <b className={audit.data?.ok ? "ok-chip" : ""}>{audit.data ? (audit.data.ok ? "✓" : "✗") : "–"}</b>
          audit chain {audit.data ? `(${audit.data.count})` : ""}
        </div>
      </div>

      <h2>GPU queue (serialized — one job at a time)</h2>
      <div className="panel">
        {q.data?.active?.length ? (
          <table>
            <thead><tr><th>Detector</th><th>Source</th><th>Status</th><th>Run</th></tr></thead>
            <tbody>
              {q.data.active.map((r) => (
                <tr key={r.run_id}>
                  <td className="detector">{r.detector}</td>
                  <td>{r.source_role || "–"}</td>
                  <td><Badge status={r.status} /></td>
                  <td className="muted">{r.run_id.slice(0, 8)}
                    {r.run_id === q.data.in_use_run ? " · on GPU" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <span className="muted">Queue empty.</span>}
      </div>

      <h2>Cases</h2>
      <div className="panel">
        <ErrorBox error={cases.error} />
        <table>
          <thead><tr><th>Pseudonym</th><th>Status</th><th>Workup</th><th>Created</th></tr></thead>
          <tbody>
            {(cases.data || []).map((c) => (
              <tr key={c.id}>
                <td><Link to={`/cases/${c.id}`}>{c.pseudonym}</Link></td>
                <td><Badge status={c.status} /></td>
                <td>{c.workup || "–"}</td>
                <td className="muted">{(c.created_at || "").slice(0, 16).replace("T", " ")}</td>
              </tr>
            ))}
            {cases.data && !cases.data.length && (
              <tr><td colSpan="4" className="muted">No cases yet — <Link to="/submit">submit one</Link>.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
