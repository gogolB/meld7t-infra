import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { ErrorBox } from "../components.jsx";

export default function Submit() {
  const nav = useNavigate();
  const [pseudonym, setPseudonym] = useState("");
  const [studyUid, setStudyUid] = useState("");
  const [dicomPath, setDicomPath] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const c = await api.createCase({
        pseudonym,
        orthanc_study_uid: studyUid || null,
        dicom_path: dicomPath || null,
      });
      nav(`/cases/${c.id}`);
    } catch (e) { setErr(e.message); setBusy(false); }
  }

  return (
    <div>
      <h1>Submit a study</h1>
      <p className="muted">Create a case, then confirm which series is which and choose the workup.</p>
      <form className="panel grow" style={{ maxWidth: 560 }} onSubmit={submit}>
        <ErrorBox error={err} />
        <label>Pseudonym *</label>
        <input value={pseudonym} onChange={(e) => setPseudonym(e.target.value)}
               placeholder="e.g. EPI-001" required />
        <label>Orthanc study UID (if already ingested — enables series sync)</label>
        <input value={studyUid} onChange={(e) => setStudyUid(e.target.value)}
               placeholder="1.2.840…" />
        <label>Local DICOM staging path (worker input, single-box demo)</label>
        <input value={dicomPath} onChange={(e) => setDicomPath(e.target.value)}
               placeholder="/var/home/bazzite/meld7t/data/raw/subject 1 clean/DICOM" />
        <div style={{ marginTop: 16 }}>
          <button className="btn" disabled={busy || !pseudonym}>{busy ? "Creating…" : "Create case"}</button>
        </div>
      </form>
    </div>
  );
}
