import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { ErrorBox } from "../components.jsx";

export default function Submit() {
  const nav = useNavigate();
  const [pseudonym, setPseudonym] = useState("");
  const [studyUid, setStudyUid] = useState("");
  const [assignedSubject, setAssignedSubject] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const c = await api.createCase({
        pseudonym,
        orthanc_study_uid: studyUid || null,
        assigned_subject: assignedSubject || null,
      });
      nav(`/cases/${c.id}`);
    } catch (e) { setErr(e.message); setBusy(false); }
  }

  return (
    <div>
      <h1>Approved study intake</h1>
      <p className="muted">Administrator-only in server mode. Create a case from a study already
        approved in the dedicated research Orthanc, then hand it to a named researcher.</p>
      <form className="panel grow" style={{ maxWidth: 560 }} onSubmit={submit}>
        <ErrorBox error={err} />
        <label>Pseudonym *</label>
        <input value={pseudonym} onChange={(e) => setPseudonym(e.target.value)}
               placeholder="e.g. EPI-001" required />
        <label>Orthanc study UID (if already ingested — enables series sync)</label>
        <input value={studyUid} onChange={(e) => setStudyUid(e.target.value)}
               placeholder="1.2.840…" />
        <label>Assigned institutional subject (optional)</label>
        <input value={assignedSubject} onChange={(e) => setAssignedSubject(e.target.value)}
               placeholder="researcher@example.org" />
        <p className="muted">Local filesystem paths are never accepted by the browser. Offline
          staging is performed by the authenticated ingest service or an operator runbook.</p>
        <div style={{ marginTop: 16 }}>
          <button className="btn" disabled={busy || !pseudonym}>{busy ? "Creating…" : "Create case"}</button>
        </div>
      </form>
    </div>
  );
}
