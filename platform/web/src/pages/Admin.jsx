import React from "react";
import { api } from "../api.js";
import { useAsync, ErrorBox } from "../components.jsx";

export default function Admin() {
  const sys = useAsync(() => api.system(), [], 4000);
  const q = useAsync(() => api.queue(), [], 4000);
  const audit = useAsync(() => api.auditVerify(), []);

  const detectors = sys.data?.detectors || {};
  const byStatus = sys.data?.runs?.by_status || {};

  return (
    <div>
      <h1>Admin</h1>
      <ErrorBox error={sys.error} />

      <div className="row">
        <div className="panel grow">
          <h2>GPU queue</h2>
          <p>{q.data?.paused ? "Paused" : "Running"} · in use: {q.data?.in_use_run?.slice(0, 8) || "idle"}</p>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn ghost" onClick={() => api.pause().then(q.reload)}>Pause queue</button>
            <button className="btn" onClick={() => api.resume().then(q.reload)}>Resume</button>
          </div>
        </div>

        <div className="panel grow">
          <h2>Audit ledger (immudb)</h2>
          <p>{audit.data
            ? (audit.data.ok ? <span className="ok-chip">✓ hash chain intact ({audit.data.count} records)</span>
               : <span className="err">✗ chain broken at #{audit.data.broken_at}</span>)
            : "…"}</p>
        </div>
      </div>

      <div className="row">
        <div className="panel grow">
          <h2>Detectors</h2>
          <table>
            <thead><tr><th>Detector</th><th>Status</th></tr></thead>
            <tbody>{Object.entries(detectors).map(([d, s]) => (
              <tr key={d}><td className="detector">{d}</td>
                <td><span className={`badge b-${s === "built" ? "review_ready" : "pending"}`}>{s}</span></td></tr>
            ))}</tbody>
          </table>
        </div>
        <div className="panel grow">
          <h2>Runs by status</h2>
          <table>
            <thead><tr><th>Status</th><th>Count</th></tr></thead>
            <tbody>{Object.entries(byStatus).map(([s, n]) => (
              <tr key={s}><td>{s}</td><td>{n}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
