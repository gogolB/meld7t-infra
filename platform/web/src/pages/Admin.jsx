import React from "react";
import { api } from "../api.js";
import { useAsync, ErrorBox } from "../components.jsx";

export default function Admin() {
  const sys = useAsync(() => api.system(), [], 15000);
  const q = useAsync(() => api.queue(), [], 15000);
  const audit = useAsync(() => api.auditVerify(), []);
  const profiles = useAsync(() => api.harmonizationProfiles(""), []);

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
            ? (audit.data.fully_verified
              ? <span className="ok-chip">✓ PostgreSQL chain + immudb proofs verified ({audit.data.count} records)</span>
              : audit.data.ok
                ? <span style={{ color: "var(--warn)", fontWeight: 600 }}>Local chain intact; immutable mirror incomplete ({audit.data.count} records)</span>
                : <span className="err">✗ audit verification failed at #{audit.data.broken_at ?? "ledger"}</span>)
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

      <div className="panel">
        <h2>Harmonization profiles</h2>
        <p className="muted">Profile artifacts are created and transferred through the signed
          offline release workflow. One administrator independently validates the signed scientific
          evidence and hashes; a second administrator activates the profile.</p>
        <ErrorBox error={profiles.error} />
        <table>
          <thead><tr><th>Code</th><th>Version</th><th>Detector</th><th>Method</th><th>Status</th><th></th></tr></thead>
          <tbody>{(profiles.data || []).map((p) => (
            <tr key={p.id}><td>{p.code}</td><td>{p.version}</td><td>{p.detector_id || "generic"}</td>
              <td>{p.method}</td><td><span className="pill">{p.status}</span></td>
              <td>{p.status === "draft" ? <button className="btn ghost"
                onClick={() => api.validateHarmonizationProfile(p.id).then(profiles.reload)}>Independently validate</button>
                : p.status === "validated" ? <button className="btn ghost"
                  onClick={() => api.activateHarmonizationProfile(p.id).then(profiles.reload)}>Activate (second admin)</button>
                : p.status === "active" ? <button className="btn ghost"
                  onClick={() => api.retireHarmonizationProfile(p.id).then(profiles.reload)}>Retire</button> : null}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}
