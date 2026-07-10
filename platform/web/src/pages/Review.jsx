import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.js";
import { Badge, useAsync, ErrorBox } from "../components.jsx";

// OHIF runs on its own origin (dedicated viewer port); deep-link the packaged study.
const VIEWER_PORT = import.meta.env.VITE_VIEWER_PORT || "9444";
const viewerUrl = (studyUid) =>
  `${window.location.protocol}//${window.location.hostname}:${VIEWER_PORT}/viewer?StudyInstanceUIDs=${studyUid}`;

export default function Review() {
  const { runId } = useParams();
  const r = useAsync(() => api.getRun(runId), [runId]);
  const [adj, setAdj] = useState({ reviewer: "", agree: true, confidence: 3, ground_truth: "", notes: "" });
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState(null);

  const run = r.data?.run, result = r.data?.result, clusters = r.data?.clusters || [];
  const frames = r.data?.frames || [];
  // MELD's own MRI overlay (cluster drawn on the T1) + the inflated-surface view — the clearest look.
  const overlayFrame = frames.find((f) => f.startsWith("mri_"));
  const surfaceFrame = frames.find((f) => f.startsWith("inflatbrain"));

  async function save(e) {
    e.preventDefault(); setErr(null);
    try { await api.adjudicate(runId, adj); setSaved(true); }
    catch (e) { setErr(e.message); }
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
        <h1>Review — {run?.detector_id} <Badge status={run?.status || "review_ready"} /></h1>
        {run && <Link to={`/cases/${run.case_id}/mdt`} className="muted" style={{ marginLeft: "auto" }}>MDT summary →</Link>}
      </div>
      <p className="muted">
        {run && <>source: {run.source_role || "–"} · {run.detector_version} · </>}
        <Link to={run ? `/cases/${run.case_id}` : "#"}>back to case</Link>
      </p>
      <ErrorBox error={r.error} />

      {/* Interactive DICOM viewer — full width */}
      <h2>DICOM viewer <span className="muted">(OHIF · T1 + segmentation)</span></h2>
      {result?.orthanc_study_uid ? (
        <>
          <div className="muted" style={{ marginBottom: 6, fontSize: 13 }}>
            To overlay MELD's flagged cluster: in the left <b>Studies</b> panel click the <b>SEG</b>
            &nbsp;series to load it, then scroll to the cluster level (or use the segmentation panel's
            jump-to-segment). The T1 loads immediately below.
          </div>
          <iframe className="viewer" style={{ height: "76vh" }} title="OHIF"
                  src={viewerUrl(result.orthanc_study_uid)} />
        </>
      ) : (
        <div className="panel muted">No packaged study yet (Orthanc UID missing).</div>
      )}

      {/* MELD's own results + clusters + adjudication */}
      <div className="row" style={{ marginTop: 16, alignItems: "flex-start" }}>
        <div className="grow">
          <h2>What MELD found</h2>
          <div className="panel">
            {clusters.length ? clusters.map((c) => (
              <div key={c.id} style={{ borderBottom: "1px solid var(--line)", padding: "6px 0" }}>
                <b>Cluster #{c.index}</b> — {c.hemi} {c.location}<br />
                <span className="muted">size {c.size} · confidence {c.confidence}</span>
              </div>
            )) : <span className="muted">No clusters above operating point (a first-class result, §21).</span>}
            {overlayFrame && (
              <img src={api.frameUrl(runId, overlayFrame)} alt="MELD cluster on T1"
                   style={{ width: "100%", borderRadius: 6, marginTop: 10, border: "1px solid var(--line)" }} />
            )}
            {surfaceFrame && (
              <img src={api.frameUrl(runId, surfaceFrame)} alt="MELD inflated surface"
                   style={{ width: "100%", borderRadius: 6, marginTop: 8, border: "1px solid var(--line)" }} />
            )}
            {result?.report_path && (
              <a className="btn ghost" href={`/api/runs/${runId}/report`} target="_blank" rel="noreferrer"
                 style={{ marginTop: 10, display: "inline-block" }}>MELD PDF report ↗</a>
            )}
          </div>
        </div>

        <div style={{ width: 340 }}>
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
