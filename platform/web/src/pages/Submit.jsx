import React, { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { ErrorBox, Badge } from "../components.jsx";
import { uploadCaseZip } from "../caseUploads.js";

const sizeLabel = (bytes) => {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024 ** 2) return `${Math.ceil(bytes / 1024)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
};

export default function Submit() {
  const navigate = useNavigate();
  const [pseudonym, setPseudonym] = useState("");
  const [file, setFile] = useState(null);
  const [upload, setUpload] = useState(null);
  const [progress, setProgress] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [recent, setRecent] = useState([]);

  useEffect(() => { api.listCaseUploads().then(setRecent).catch(() => {}); }, []);
  useEffect(() => {
    if (!upload?.id || !["staged", "importing"].includes(upload.status)) return undefined;
    const timer = setInterval(async () => {
      try {
        const current = await api.getCaseUpload(upload.id);
        setUpload(current);
        if (["ready", "failed"].includes(current.status)) {
          clearInterval(timer);
          setRecent((rows) => [current, ...rows.filter((row) => row.id !== current.id)]);
        }
      } catch (err) { setError(err.message); clearInterval(timer); }
    }, 2500);
    return () => clearInterval(timer);
  }, [upload?.id, upload?.status]);

  async function submit(event) {
    event.preventDefault(); setError(null); setProgress(null); setBusy(true);
    try {
      const result = await uploadCaseZip(pseudonym, file, { onProgress: setProgress });
      setUpload(result);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  const percent = progress?.total ? Math.floor(progress.loaded / progress.total * 100) : 0;
  return (
    <div>
      <div className="section-heading"><div><p className="eyebrow">Study intake</p>
        <h1>Upload DICOM study</h1><p className="muted">Upload one ZIP containing one DICOM study.
          It is validated, imported into the research archive, and classified before you confirm
          the exact processing plan.</p></div></div>

      <div className="intake-grid">
        <form className="panel" onSubmit={submit}>
          <ErrorBox error={error} />
          <label htmlFor="case-pseudonym">Research pseudonym</label>
          <input id="case-pseudonym" value={pseudonym} required
            pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" maxLength="64"
            onChange={(event) => setPseudonym(event.target.value)} placeholder="e.g. HMRI-001" />
          <label htmlFor="dicom-zip">DICOM ZIP</label>
          <input id="dicom-zip" type="file" accept=".zip,application/zip" required
            onChange={(event) => setFile(event.target.files?.[0] || null)} />
          {file && <p className="muted">{file.name} · {sizeLabel(file.size)}</p>}
          <div className="warning"><strong>One study per ZIP.</strong> Nested directories and a
            DICOMDIR are supported. Mixed patients/studies and unexpected files are rejected.</div>
          {progress && <div className="upload-progress" aria-live="polite">
            <div><span>{progress.phase === "hashing" ? "Calculating SHA-256" :
              progress.phase === "uploading" ? "Uploading" : "Verifying upload"}</span>
              <strong>{percent}%</strong></div>
            <progress max="100" value={percent} />
          </div>}
          <button className="btn" disabled={!file || !pseudonym || busy}>
            {busy ? "Preparing durable upload…" : "Upload and validate study"}
          </button>
        </form>

        <div>
          <h2>Current upload</h2>
          {upload ? <div className="panel">
            <div className="result-heading"><div><strong>{upload.pseudonym}</strong>
              <p className="muted">{upload.import_result?.phase?.replaceAll("_", " ") ||
                "durable upload session"}</p></div><Badge status={upload.status} /></div>
            {upload.status === "failed" && <div className="err">Import failed: {
              upload.last_error || "validation failed"}</div>}
            {upload.status === "ready" && <>
              <div className="notice">Import complete. Review every proposed series role and the
                detector/harmonization plan before anything is queued.</div>
              <button className="btn" onClick={() => navigate(`/cases/${upload.case_id}`)}>
                Review processing plan →</button>
            </>}
            {["staged", "importing"].includes(upload.status) && <p className="muted">
              Server-side validation and import are queued. This page updates automatically.</p>}
          </div> : <div className="panel empty-state">No upload active in this browser.</div>}

          <h2>Recent uploads</h2>
          <div className="panel">
            {(recent || []).map((row) => <div className="upload-row" key={row.id}>
              <div><strong>{row.pseudonym}</strong><span className="muted">{
                row.import_result?.series_count != null ? `${row.import_result.series_count} series` :
                  sizeLabel(row.total_size)}</span></div>
              <Badge status={row.status} />
              {row.case_id && <Link className="btn ghost" to={`/cases/${row.case_id}`}>
                Open plan</Link>}
            </div>)}
            {recent && !recent.length && <span className="muted">No recent uploads.</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
