import React, { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api.js";
import { Badge, useAsync, ErrorBox } from "../components.jsx";

const VIEWER_PORT = import.meta.env.VITE_VIEWER_PORT || "9444";
const viewerUrl = (studyUid) =>
  `${window.location.protocol}//${window.location.hostname}:${VIEWER_PORT}` +
  `/viewer?StudyInstanceUIDs=${encodeURIComponent(studyUid)}`;

const detectorLabel = (value) => ({
  meld_fcd: "MELD FCD",
  map: "MAP",
  hippunfold: "HippUnfold HS",
}[value] || value || "Detector");

function HarmonizationStatus({ run }) {
  const harmonization = run?.harmonization;
  if (!harmonization) return null;
  if (harmonization.mode === "harmonized") {
    const profile = harmonization.profile || {};
    return <span className="ok-chip">Harmonized · {profile.code} v{profile.version}</span>;
  }
  if (harmonization.mode === "not_applicable") {
    return <span className="pill">Harmonization not applicable</span>;
  }
  return <span className="badge b-unharmonized">Unharmonized</span>;
}

function AdjudicationPanel({ row, canReview, reload }) {
  const run = row?.run;
  const history = row?.adjudications || [];
  const latest = history.length ? history[history.length - 1] : null;
  const [form, setForm] = useState({ agree: true, confidence: 3, ground_truth: "", notes: "" });
  const [correcting, setCorrecting] = useState(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    setCorrecting(null); setSaved(false); setError(null);
  }, [run?.id]);

  if (!run) return null;
  if (!row?.result) {
    return <div className="panel muted">This processing-plan slot was not run, so there is no
      detector result to adjudicate.</div>;
  }
  if (!["review_ready", "adjudicated"].includes(run.status)) {
    return <div className="panel muted">Adjudication becomes available after a validated detector
      result reaches review.</div>;
  }
  if (!canReview) {
    return <div className="panel muted">Review history is visible to all authenticated users.
      Recording an adjudication requires the reviewer role.</div>;
  }

  async function save(event) {
    event.preventDefault(); setError(null);
    try {
      await api.adjudicate(run.id, {
        ...form,
        ...(correcting ? { supersedes: correcting } : {}),
      });
      setSaved(true); setCorrecting(null); reload();
    } catch (err) { setError(err.message); }
  }

  return (
    <form className="panel" onSubmit={save}>
      <ErrorBox error={error} />
      {saved && <div className="notice">Review recorded in the immutable audit trail.</div>}
      {history.length > 0 && <p className="muted">{history.length} review record(s). Corrections
        append a linked record and do not overwrite history.</p>}
      <label>Assessment</label>
      <select value={String(form.agree)} onChange={(event) => setForm({
        ...form, agree: event.target.value === "true",
      })}>
        <option value="true">Agree with detector</option>
        <option value="false">Disagree</option>
      </select>
      <label>Confidence (1–5)</label>
      <input type="number" min="1" max="5" value={form.confidence}
        onChange={(event) => setForm({ ...form, confidence: Number(event.target.value) })} />
      <label>Ground-truth mark (optional)</label>
      <input value={form.ground_truth}
        onChange={(event) => setForm({ ...form, ground_truth: event.target.value })} />
      <label>Notes</label>
      <textarea rows="4" value={form.notes}
        onChange={(event) => setForm({ ...form, notes: event.target.value })} />
      <div className="button-row">
        <button className="btn" disabled={saved || (latest && !correcting)}>
          {correcting ? "Record correction" : "Record research review"}
        </button>
        {latest && !correcting && <button type="button" className="btn ghost"
          onClick={() => { setCorrecting(latest.id); setSaved(false); }}>
          Correct latest review
        </button>}
      </div>
    </form>
  );
}

export default function Review() {
  const { id } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const review = useAsync(() => api.reviewStudy(id), [id], 15000);
  const me = useAsync(() => api.me(), []);
  const data = review.data;
  const requestedRun = searchParams.get("run");
  const [studyUid, setStudyUid] = useState("");
  const [runId, setRunId] = useState(requestedRun || "");
  const [reportBusy, setReportBusy] = useState("");
  const [reportError, setReportError] = useState(null);

  useEffect(() => {
    if (!studyUid && data?.viewer_studies?.length) {
      setStudyUid(data.viewer_studies[0].study_uid);
    }
    if (!runId && data?.runs?.length) setRunId(data.runs[0].run.id);
  }, [data, studyUid, runId]);

  const selected = useMemo(() => (data?.runs || []).find((row) => row.run.id === runId)
    || data?.runs?.[0], [data, runId]);
  const canReview = me.data?.roles?.some((role) => role === "reviewer" || role === "admin");

  function selectRun(next) {
    setRunId(next);
    const params = new URLSearchParams(searchParams);
    params.set("run", next); setSearchParams(params, { replace: true });
    const row = (data?.runs || []).find((item) => item.run.id === next);
    if (row?.result?.orthanc_study_uid) setStudyUid(row.result.orthanc_study_uid);
  }

  const metrics = selected?.result?.metric_schema || {};

  async function generateReport(kind) {
    setReportBusy(kind); setReportError(null);
    try { await api.requestCaseReport(id, kind); review.reload(); }
    catch (error) { setReportError(error.message); }
    finally { setReportBusy(""); }
  }

  return (
    <div>
      <div className="section-heading">
        <div>
          <p className="eyebrow">Review study</p>
          <h1>{data?.case?.pseudonym || "Study"}</h1>
          <p className="muted">All uploaded scans and current MAP, MELD, and HS outputs in one workspace.</p>
        </div>
        <div className="button-row">
          <Link className="btn ghost" to={`/cases/${id}`}>Processing plan</Link>
          <Link className="btn ghost" to={`/cases/${id}/mdt`}>Research summary</Link>
        </div>
      </div>
      <ErrorBox error={review.error} />

      {(data?.warnings || []).map((warning) => <div className="warning warning-prominent"
        key={warning}><strong>Unharmonized result</strong><br />{warning}</div>)}

      <section>
        <div className="section-heading">
          <div><h2>Combined reports</h2><p className="muted">White-labeled, immutable versions
            combining MAP, MELD, and HS results. Preliminary reports capture automated analysis;
            final reports capture completed adjudications.</p></div>
          {canReview && <div className="button-row">
            <button type="button" className="btn ghost" disabled={Boolean(reportBusy)}
              onClick={() => generateReport("preliminary")}>Generate preliminary</button>
            <button type="button" className="btn" disabled={Boolean(reportBusy)}
              onClick={() => generateReport("final")}>Generate final</button>
          </div>}
        </div>
        <ErrorBox error={reportError} />
        <div className="report-list">
          {(data?.reports || []).map((report) => <div className="report-card" key={report.id}>
            <div><strong>{report.kind === "final" ? "Final" : "Preliminary"} report</strong>
              <div className="muted">Version {report.version} · {report.snapshot_sha256?.slice(0, 12)}</div></div>
            <Badge status={report.status} />
            {report.has_error && <span className="err">Generation failed; a reviewer can retry.</span>}
            {report.download_url && <a className="btn ghost" href={report.download_url}
              target="_blank" rel="noreferrer">Open PDF ↗</a>}
          </div>)}
          {data?.reports && !data.reports.length && <div className="panel empty-state">
            The preliminary report is queued automatically when all runnable analyses finish.</div>}
        </div>
      </section>

      <section>
        <div className="section-heading">
          <div><h2>Imaging workspace</h2><p className="muted">The source study remains immutable;
            derived studies are grouped here without rewriting uploaded DICOM identifiers.</p></div>
        </div>
        <div className="study-tabs" role="tablist" aria-label="Available imaging studies">
          {(data?.viewer_studies || []).map((study) => <button type="button"
            role="tab" aria-selected={study.study_uid === studyUid}
            className={`study-tab ${study.study_uid === studyUid ? "active" : ""}`}
            key={study.study_uid} onClick={() => setStudyUid(study.study_uid)}>
            <span>{study.label}</span>
            <small>{study.kind === "source" ? `${data?.source_series?.length || 0} uploaded series`
              : (study.detector_ids || []).map(detectorLabel).join(" · ")}</small>
          </button>)}
        </div>
        {studyUid ? <iframe className="viewer" title="DICOM review viewer"
          src={viewerUrl(studyUid)} /> : <div className="panel empty-state">
          Imaging is still being imported or packaged.</div>}
      </section>

      <section>
        <h2>Uploaded scans</h2>
        <div className="panel table-scroll">
          <table>
            <thead><tr><th>Series</th><th>Modality</th><th>Images</th><th>Availability</th>
              <th>Confirmed use</th></tr></thead>
            <tbody>{(data?.source_series || []).map((series) => <tr key={series.id}>
              <td>{series.series_description || series.orthanc_series_uid}</td>
              <td>{series.modality || "–"}</td><td>{series.instance_count ?? "–"}</td>
              <td>{series.active === false ? <span className="badge b-blocked">
                No longer present</span> : <span className="ok-chip">Present</span>}</td>
              <td><span className="pill">{series.confirmed_role || series.proposed_role}</span></td>
            </tr>)}</tbody>
          </table>
        </div>
      </section>

      <section>
        <h2>Combined detector results</h2>
        <div className="detector-tabs" role="tablist" aria-label="Detector results">
          {(data?.runs || []).map((row) => <button type="button" role="tab"
            aria-selected={row.run.id === selected?.run?.id}
            className={`detector-tab ${row.run.id === selected?.run?.id ? "active" : ""}`}
            key={row.run.id} onClick={() => selectRun(row.run.id)}>
            <span>{detectorLabel(row.run.detector_id)}</span>
            <Badge status={row.run.status} />
          </button>)}
        </div>

        {selected ? <div className="review-grid">
          <div>
            {selected.result && selected.run.warnings?.map((warning) => <div className="warning" key={warning}>
              <strong>Unharmonized output</strong><br />{warning}</div>)}
            {selected.result?.derived_series_integrity === "failed" && <div className="err">
              Derived DICOM publication metadata failed integrity verification. Viewer links are
              hidden until the output is repaired or rerun.</div>}
            <div className="panel">
              <div className="result-heading">
                <div><h3>{detectorLabel(selected.run.detector_id)}</h3>
                  <span className="muted">{selected.run.source_role || "No source role"}</span></div>
                {selected.result && <HarmonizationStatus run={selected.run} />}
              </div>
              {!selected.result ? <div className="notice">This detector was declared in the
                processing plan but was not run. No negative result is asserted.</div> :
                selected.clusters?.length ? selected.clusters.map((cluster) => <div
                className="finding" key={cluster.id}>
                <strong>Finding #{cluster.index}</strong> · {cluster.hemi || ""} {cluster.location || ""}
                <div className="muted">{metrics.size?.label || "Detector size"}: {cluster.size ?? "–"}
                  {metrics.size?.unit ? ` ${metrics.size.unit}` : ""} · {metrics.confidence?.label ||
                    "Detector score"}: {cluster.confidence ?? "–"}{metrics.confidence?.unit
                    ? ` ${metrics.confidence.unit}` : ""}</div>
              </div>) : <p className="muted">No findings above this detector’s operating point.</p>}
              {selected.frames?.length > 0 && <div className="report-frames">
                {selected.frames.map((frame) => <img key={frame}
                  src={api.frameUrl(selected.run.id, frame)} alt={`${detectorLabel(
                    selected.run.detector_id)} ${frame}`} />)}
              </div>}
            </div>
          </div>
          <div><h3>Adjudication</h3><AdjudicationPanel row={selected} canReview={canReview}
            reload={review.reload} /></div>
        </div> : <div className="panel empty-state">No detector runs have been created.</div>}
      </section>
    </div>
  );
}
