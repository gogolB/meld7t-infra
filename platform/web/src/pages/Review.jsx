import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.js";
import { Badge, useAsync, ErrorBox } from "../components.jsx";

// OHIF runs on its own origin (dedicated viewer port); deep-link the packaged study.
const VIEWER_PORT = import.meta.env.VITE_VIEWER_PORT || "8444";
const viewerUrl = (studyUid) =>
  `${window.location.protocol}//${window.location.hostname}:${VIEWER_PORT}/viewer?StudyInstanceUIDs=${studyUid}`;

export default function Review() {
  const { runId } = useParams();
  const r = useAsync(() => api.getRun(runId), [runId]);
  const [adj, setAdj] = useState({ reviewer: "", agree: true, confidence: 3, ground_truth: "", notes: "" });
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState(null);

  const run = r.data?.run, result = r.data?.result, clusters = r.data?.clusters || [];

  async function save(e) {
    e.preventDefault(); setErr(null);
    try { await api.adjudicate(runId, adj); setSaved(true); }
    catch (e) { setErr(e.message); }
  }

  return (
    <div>
      <h1>Review — {run?.detector_id} <Badge status={run?.status || "review_ready"} /></h1>
      <p className="muted">
        {run && <>source: {run.source_role || "–"} · {run.detector_version} · </>}
        <Link to={run ? `/cases/${run.case_id}` : "#"}>back to case</Link>
      </p>
      <ErrorBox error={r.error} />

      <div className="split">
        <div>
          {result?.orthanc_study_uid ? (
            <iframe className="viewer" title="OHIF" src={viewerUrl(result.orthanc_study_uid)} />
          ) : (
            <div className="panel muted">No packaged study yet (Orthanc UID missing).</div>
          )}
        </div>

        <div>
          <h2>Clusters (operating point)</h2>
          <div className="panel">
            {clusters.length ? clusters.map((c) => (
              <div key={c.id} style={{ borderBottom: "1px solid var(--line)", padding: "6px 0" }}>
                <b>#{c.index}</b> {c.hemi} {c.location}<br />
                <span className="muted">size {c.size} · confidence {c.confidence}</span>
              </div>
            )) : <span className="muted">No clusters above operating point (first-class result, §21).</span>}
          </div>

          <h2>Adjudication <span className="muted">(append-only, §24)</span></h2>
          <form className="panel" onSubmit={save}>
            <ErrorBox error={err} />
            {saved && <div className="ok-chip">Saved — recorded to the immudb ledger.</div>}
            <label>Reviewer *</label>
            <input value={adj.reviewer} required
              onChange={(e) => setAdj({ ...adj, reviewer: e.target.value })} />
            <label>Assessment</label>
            <select value={String(adj.agree)}
              onChange={(e) => setAdj({ ...adj, agree: e.target.value === "true" })}>
              <option value="true">Agree with detector</option>
              <option value="false">Disagree</option>
            </select>
            <label>Confidence (1–5)</label>
            <input type="number" min="1" max="5" value={adj.confidence}
              onChange={(e) => setAdj({ ...adj, confidence: Number(e.target.value) })} />
            <label>Ground-truth mark (optional)</label>
            <input value={adj.ground_truth}
              onChange={(e) => setAdj({ ...adj, ground_truth: e.target.value })} />
            <label>Notes</label>
            <input value={adj.notes} onChange={(e) => setAdj({ ...adj, notes: e.target.value })} />
            <div style={{ marginTop: 12 }}>
              <button className="btn" disabled={!adj.reviewer || saved}>Sign &amp; record</button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
