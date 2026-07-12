import React, { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.js";
import { Badge, ErrorBox, useAsync } from "../components.jsx";
import { uploadCohortFiles } from "../harmonizationUploads.js";

function countFor(cohort, name) {
  const aliases = { total: "studies", eligible: "included" };
  return cohort?.counts?.[name] ?? cohort?.counts?.[aliases[name]]
    ?? cohort?.[`${name}_count`] ?? 0;
}

function listFor(value, key) {
  if (Array.isArray(value)) return value;
  return value?.[key] || value?.items || [];
}

function currentBuildId(cohort) {
  const builds = listFor(cohort?.builds, "builds");
  return cohort?.latest_build?.id || cohort?.latest_build_id || builds[0]?.id || null;
}

function parseStudyRows(value) {
  const seen = new Set();
  const seenSubjects = new Set();
  return value.split(/\r?\n/).filter((line) => line.trim()).map((line, index) => {
    const [studyUid, subjectKey, ...extra] = line.split(",").map((part) => part.trim());
    if (!studyUid || !subjectKey || extra.length) {
      throw new Error(`line ${index + 1} must contain study_uid,subject_key`);
    }
    if (!/^[0-9]+(?:\.[0-9]+)+$/.test(studyUid)) {
      throw new Error(`line ${index + 1} has an invalid StudyInstanceUID`);
    }
    if (subjectKey.length > 128) throw new Error(`line ${index + 1} subject key is too long`);
    if (seen.has(studyUid)) throw new Error(`duplicate study UID on line ${index + 1}`);
    if (seenSubjects.has(subjectKey)) throw new Error(`duplicate subject key on line ${index + 1}`);
    seen.add(studyUid);
    seenSubjects.add(subjectKey);
    return { study_uid: studyUid, subject_key: subjectKey, included: true };
  });
}

function progressPercent(progress) {
  const value = Number(progress);
  if (!Number.isFinite(value)) return null;
  return Math.max(0, Math.min(100, value <= 1 ? value * 100 : value));
}

function scientificReportError(report, cohort, expectedAdapterSha256 = null) {
  if (!report || Array.isArray(report) || typeof report !== "object") {
    return "scientific validation must be a JSON object";
  }
  if (report.schema_version !== 1) return "scientific validation must use schema_version 1";
  if (report.profile?.code !== cohort?.profile_code
      || Number(report.profile?.version) !== Number(cohort?.profile_version)
      || report.profile?.detector_id !== "meld_fcd") {
    return "scientific validation is bound to a different profile";
  }
  for (const field of ["approval_id", "independent_reviewer", "approved_at"]) {
    if (typeof report[field] !== "string" || !report[field].trim()) {
      return `scientific validation requires ${field}`;
    }
  }
  if (!Array.isArray(report.acquisition_fingerprints) || !report.acquisition_fingerprints.length
      || report.acquisition_fingerprints.some((value) => !/^[0-9a-f]{64}$/i.test(value))) {
    return "scientific validation requires acquisition fingerprint digests";
  }
  if (!Number.isInteger(report.qc?.included) || report.qc.included < 20
      || !Number.isInteger(report.qc?.excluded) || report.qc.excluded < 0) {
    return "scientific validation requires valid included and excluded QC counts";
  }
  const holdout = report.holdout || {};
  const holdoutCount = ["positive_cases", "negative_cases", "control_cases"]
    .reduce((sum, field) => sum + (Number.isInteger(holdout[field]) ? holdout[field] : 0), 0);
  if (["positive_cases", "negative_cases", "control_cases"].some(
    (field) => !Number.isInteger(holdout[field]) || holdout[field] < 1)
      || holdout.case_count !== holdoutCount) {
    return "scientific validation requires positive, negative, and control holdouts";
  }
  for (const field of ["metrics_sha256", "golden_case_evidence_sha256", "methodology_sha256"]) {
    if (!/^[0-9a-f]{64}$/i.test(String(report[field] || ""))) {
      return `scientific validation requires ${field}`;
    }
  }
  if (!/^[0-9a-f]{64}$/.test(String(report.builder_adapter_sha256 || ""))) {
    return "scientific validation requires builder_adapter_sha256";
  }
  if (expectedAdapterSha256
      && report.builder_adapter_sha256.toLowerCase() !== expectedAdapterSha256.toLowerCase()) {
    return "scientific validation builder adapter differs from the admitted build";
  }
  const images = Object.values(report.image_digests || {});
  if (!images.length || images.some((value) => !/@sha256:[0-9a-f]{64}$/i.test(String(value)))) {
    return "scientific validation requires digest-pinned build images";
  }
  return null;
}

function StudyDecision({ study, busy, onSave }) {
  const [editing, setEditing] = useState(false);
  const [reason, setReason] = useState("");
  if (!study.included) {
    return <button type="button" className="btn ghost" disabled={busy}
      onClick={() => onSave(true, null)}>Include</button>;
  }
  if (!editing) return <button type="button" className="btn ghost" disabled={busy}
    onClick={() => setEditing(true)}>Exclude</button>;
  return <div className="study-decision">
    <label htmlFor={`exclude-${study.id}`}>Exclusion reason *</label>
    <input id={`exclude-${study.id}`} value={reason} maxLength="500"
      onChange={(e) => setReason(e.target.value)} />
    <button type="button" className="btn danger" disabled={busy || !reason.trim()}
      onClick={() => onSave(false, reason.trim()).then((saved) => saved && setEditing(false))}>
      Save exclusion</button>
    <button type="button" className="btn ghost" onClick={() => setEditing(false)}>Cancel</button>
  </div>;
}

function RollbackResolution({ upload, busy, onLoadEvidence, onResolve }) {
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [receipt, setReceipt] = useState(null);
  const [loadingReceipt, setLoadingReceipt] = useState(false);
  const [receiptError, setReceiptError] = useState(null);
  const valid = reason.trim().length >= 20 && /^[0-9a-f]{64}$/i.test(evidence.trim());

  async function loadReceipt() {
    setLoadingReceipt(true);
    setReceiptError(null);
    try {
      const value = await onLoadEvidence();
      setReceipt(value);
      setEvidence(value.receipt_evidence_sha256);
    } catch (err) {
      setReceiptError(err.message);
    } finally {
      setLoadingReceipt(false);
    }
  }

  return <div className="study-decision">
    <p className="digest">Protected receipt evidence: {
      upload.import_result?.receipt_evidence_sha256 || "unavailable—restore the receipt"}</p>
    <button type="button" className="btn ghost" disabled={busy || loadingReceipt}
      onClick={loadReceipt}>{loadingReceipt ? "Verifying receipt…" : "Load verified receipt"}</button>
    {receiptError && <p className="error" role="alert">{receiptError}</p>}
    {receipt && <details open>
      <summary>Canonical rollback evidence ({receipt.instances.length} instances)</summary>
      <p className="muted">Pending categories: {Object.entries(receipt.pending_counts)
        .map(([name, count]) => `${name}=${count}`).join(", ")}</p>
      <pre>{JSON.stringify(receipt.instances, null, 2)}</pre>
    </details>}
    <label htmlFor={`rollback-reason-${upload.id}`}>Rollback resolution reason *</label>
    <input id={`rollback-reason-${upload.id}`} value={reason} maxLength="2000"
      onChange={(event) => setReason(event.target.value)} />
    <label htmlFor={`rollback-evidence-${upload.id}`}>Evidence SHA-256 *</label>
    <input id={`rollback-evidence-${upload.id}`} value={evidence} maxLength="64"
      onChange={(event) => setEvidence(event.target.value)} />
    <p className="muted">Exact deletion requires the verified receipt digest. For preservation,
      replace it with the SHA-256 of the site ownership/reference attestation.</p>
    <button type="button" className="btn ghost" disabled={busy || !valid}
      onClick={() => onResolve("preserve", reason.trim(), evidence.trim())}>
      Preserve external object</button>
    <button type="button" className="btn danger" disabled={busy || !valid}
      onClick={() => onResolve("delete", reason.trim(), evidence.trim())}>
      Approve exact deletion</button>
  </div>;
}

export default function HarmonizationCohort() {
  const { id } = useParams();
  const cohort = useAsync(() => api.getHarmonizationCohort(id), [id], 15000);
  const [buildId, setBuildId] = useState(null);
  const build = useAsync(
    () => buildId ? api.getHarmonizationBuild(buildId) : Promise.resolve(null),
    [buildId], buildId ? 5000 : 0,
  );
  const buildStatus = build.data?.status;
  const shouldLoadQc = buildId && ["qc_review", "validated", "active"].includes(buildStatus);
  const qc = useAsync(
    () => shouldLoadQc ? api.getHarmonizationBuildQc(buildId) : Promise.resolve(null),
    [buildId, shouldLoadQc], shouldLoadQc ? 15000 : 0,
  );
  const [studyRows, setStudyRows] = useState("");
  const [demographics, setDemographics] = useState(null);
  const [files, setFiles] = useState([]);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [builderDigest, setBuilderDigest] = useState("");
  const [criteria, setCriteria] = useState(
    '{\n  "methodology_sha256": "",\n  "required_metrics": {}\n}');
  const [scientificValidation, setScientificValidation] = useState("{}");
  const [rejectionReason, setRejectionReason] = useState("");
  const [rejectionEvidence, setRejectionEvidence] = useState("");
  const [releaseExport, setReleaseExport] = useState(null);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  useEffect(() => {
    if (!buildId && cohort.data) setBuildId(currentBuildId(cohort.data));
  }, [buildId, cohort.data]);

  const studies = listFor(cohort.data?.studies, "studies");
  const uploads = listFor(cohort.data?.uploads, "uploads");
  const builds = listFor(cohort.data?.builds, "builds");
  const mutable = ["draft", "cohort_ready"].includes(cohort.data?.status);
  const eligibleCount = countFor(cohort.data, "eligible");
  const demographicsCount = countFor(cohort.data, "demographics");
  const canFreeze = eligibleCount >= (cohort.data?.min_controls || 20)
    && demographicsCount === eligibleCount;
  const pct = progressPercent(build.data?.progress);
  const uploadPct = uploadProgress?.total
    ? Math.round((uploadProgress.loaded / uploadProgress.total) * 100) : null;
  const selector = useMemo(() => cohort.data?.selector || {}, [cohort.data]);

  async function action(name, work, message) {
    setBusy(name); setError(null); setNotice(null);
    try {
      const result = await work();
      if (message) setNotice(message);
      cohort.reload();
      if (buildId) build.reload();
      return result;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function importStudies(e) {
    e.preventDefault();
    let rows;
    try {
      rows = parseStudyRows(studyRows);
      if (!rows.length) throw new Error("enter at least one Orthanc study");
    } catch (err) {
      setError(err.message); return;
    }
    const result = await action("import", () => api.importHarmonizationStudies(id, rows),
      `${rows.length} stud${rows.length === 1 ? "y" : "ies"} submitted for validation.`);
    if (result) setStudyRows("");
  }

  async function uploadFiles(e) {
    e.preventDefault();
    if (!files.length) { setError("select DICOM files or ZIP archives first"); return; }
    const result = await action("upload", () => uploadCohortFiles(id, files, {
      onProgress: setUploadProgress,
    }), `${files.length} upload${files.length === 1 ? "" : "s"} verified and submitted.`);
    if (result) setFiles([]);
    setUploadProgress(null);
  }

  async function uploadDemographics(e) {
    e.preventDefault();
    if (!demographics) { setError("select a demographics CSV first"); return; }
    await action("demographics", async () => {
      const csv = await demographics.text();
      return api.submitHarmonizationDemographics(id, csv);
    }, "Demographics validated against the cohort subject keys.");
  }

  async function startBuild(e) {
    e.preventDefault();
    let parsed;
    try {
      parsed = JSON.parse(criteria);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("acceptance criteria must be a JSON object");
      }
      if (!/^[0-9a-f]{64}$/i.test(String(parsed.methodology_sha256 || ""))) {
        throw new Error("acceptance criteria require a methodology_sha256 digest");
      }
      if (!parsed.required_metrics || Array.isArray(parsed.required_metrics) ||
          typeof parsed.required_metrics !== "object" ||
          !Object.keys(parsed.required_metrics).length) {
        throw new Error("acceptance criteria require at least one approved metric bound");
      }
    } catch (err) {
      setError(`Invalid acceptance criteria: ${err.message}`); return;
    }
    const result = await action("build", () => api.createHarmonizationBuild(id, {
      acceptance_criteria: parsed,
      builder_image_digest: builderDigest.trim(),
    }), "Build queued on the isolated harmonization worker.");
    if (result?.id) setBuildId(result.id);
  }

  async function validateBuild() {
    let report;
    try {
      report = JSON.parse(scientificValidation);
      const validationError = scientificReportError(
        report, cohort.data, build.data?.builder_adapter_sha256,
      );
      if (validationError) throw new Error(validationError);
    } catch (err) {
      setError(`Invalid scientific validation report: ${err.message}`); return;
    }
    await action("validate", () => api.validateHarmonizationBuild(buildId, report),
      "Candidate and external validation evidence independently validated.");
  }

  async function rejectBuild() {
    if (rejectionReason.trim().length < 20 ||
        !/^[0-9a-f]{64}$/i.test(rejectionEvidence.trim())) {
      setError("Rejection requires a substantive reason and a 64-hex evidence digest.");
      return;
    }
    await action("reject", () => api.rejectHarmonizationBuild(
      buildId, rejectionReason.trim(), rejectionEvidence.trim()),
    "Candidate rejected and cohort archived; use a new profile version for revised work.");
  }

  async function prepareReleaseExport() {
    const result = await action("release-export",
      () => api.exportHarmonizationBuild(buildId),
      "Release document prepared; copy the listed generated artifacts before offline signing.");
    if (result) setReleaseExport(result);
  }

  async function decideStudy(study, included, reason) {
    return action(`decision-${study.id}`,
      () => api.decideHarmonizationStudy(id, study.id, included, reason),
      included ? "Control included." : "Control excluded with an audited reason.");
  }

  async function resolveRollback(upload, resolution, reason, evidence) {
    return action(`rollback-${upload.id}`,
      () => api.resolveHarmonizationUploadRollback(
        id, upload.id, resolution, reason, evidence),
      resolution === "preserve"
        ? "Ambiguous Orthanc object preserved and rollback gate closed."
        : "Exact deletion approved; the builder will reconcile it before accepting new work.");
  }

  function useImportedMappings(upload) {
    const mappings = Array.isArray(upload.import_result?.studies)
      ? upload.import_result.studies : [];
    setStudyRows((current) => {
      const existingUids = new Set(current.split(/\r?\n/).map(
        (line) => line.split(",", 1)[0].trim()).filter(Boolean));
      const additions = mappings.filter((item) => !existingUids.has(item.study_uid))
        .map((item) => `${item.study_uid},${item.subject_key}`);
      return [current.trim(), ...additions].filter(Boolean).join("\n");
    });
    setNotice(`${mappings.length} DICOM pseudonym mapping${mappings.length === 1 ? "" : "s"} added to the admission form.`);
  }

  async function loadScientificValidation(file) {
    if (!file) return;
    try {
      setScientificValidation(await file.text());
      setError(null);
    } catch (err) {
      setError(`Could not read scientific validation report: ${err.message}`);
    }
  }

  if (cohort.loading && !cohort.data) return <p role="status">Loading cohort…</p>;
  if (cohort.error && !cohort.data) return <ErrorBox error={cohort.error} />;

  return (
    <div>
      <p><Link to="/admin">← Admin</Link></p>
      <div className="section-heading">
        <div><h1>{cohort.data?.name || "Harmonization cohort"}</h1>
          <p className="muted">{cohort.data?.site_code} · {cohort.data?.profile_code}
            {cohort.data?.profile_version ? ` v${cohort.data.profile_version}` : ""}</p></div>
        <Badge status={cohort.data?.status || "pending"} />
      </div>
      <ErrorBox error={cohort.error || build.error || error} />
      {notice && <div className="notice" role="status">{notice}</div>}

      <div className="tiles cohort-tiles">
        <div className="tile"><b>{countFor(cohort.data, "total")}</b>controls received</div>
        <div className="tile"><b>{countFor(cohort.data, "eligible")}</b>eligible</div>
        <div className="tile"><b>{countFor(cohort.data, "excluded")}</b>excluded</div>
        <div className="tile"><b>{demographicsCount}</b>demographics matched</div>
        <div className="tile"><b>{cohort.data?.cv_folds || 5}</b>CV folds</div>
      </div>

      <div className="row">
        <section className="panel grow" aria-labelledby="cohort-contract-title">
          <h2 id="cohort-contract-title">Frozen profile contract</h2>
          <dl className="definition-grid">
            <dt>Source role</dt><dd>{cohort.data?.source_role || "–"}</dd>
            <dt>Minimum controls</dt><dd>{cohort.data?.min_controls || 20}</dd>
            <dt>Selector</dt><dd><code>{JSON.stringify(selector)}</code></dd>
            <dt>Cohort digest</dt><dd className="digest">
              {cohort.data?.frozen_manifest?.manifest_sha256 || "Not frozen"}</dd>
          </dl>
        </section>
        <section className="panel grow" aria-labelledby="cohort-safety-title">
          <h2 id="cohort-safety-title">Storage boundary</h2>
          <p>Controls remain in the dedicated harmonization Orthanc. Builds use a restricted
            workspace; activated artifacts become immutable profile storage.</p>
          <p className="muted">Only deidentified research controls are permitted. Freezing captures
            cohort membership, demographics, configuration, and input hashes.</p>
        </section>
      </div>

      {mutable && <section className="panel" aria-labelledby="ingestion-title">
        <h2 id="ingestion-title">1. Add deidentified controls</h2>
        <div className="three-columns">
          <form className="subpanel" onSubmit={importStudies}>
            <h3>Select from harmonization Orthanc</h3>
            <label htmlFor="orthanc-study-rows">One StudyInstanceUID,subject_key pair per line</label>
            <textarea id="orthanc-study-rows" rows="6" value={studyRows}
              onChange={(e) => setStudyRows(e.target.value)}
              placeholder={"1.2.840.10008.1,HC-001\n1.2.840.10008.2,HC-002"} />
            <button className="btn" disabled={busy === "import"}>
              {busy === "import" ? "Importing…" : "Import selected studies"}</button>
          </form>

          <form className="subpanel" onSubmit={uploadFiles}>
            <h3>Upload DICOM or ZIP</h3>
            <label htmlFor="cohort-files">Files</label>
            <input id="cohort-files" type="file" multiple
              accept=".dcm,.dicom,.zip,application/dicom,application/zip"
              onChange={(e) => setFiles(Array.from(e.target.files || []))} />
            <p className="muted">Uploads are checksum-verified and sent in resumable chunks.
              Archive paths and DICOM policy are checked by the server.</p>
            {uploadProgress && <p role="status">{uploadProgress.phase}: {uploadProgress.file}
              {uploadPct !== null ? ` (${uploadPct}%)` : ""}</p>}
            <button className="btn" disabled={busy === "upload" || !files.length}>
              {busy === "upload" ? "Uploading…" : "Upload controls"}</button>
          </form>

          <form className="subpanel" onSubmit={uploadDemographics}>
            <h3>Demographics</h3>
            <label htmlFor="demographics-file">CSV with exact ID,Age,Sex headers</label>
            <input id="demographics-file" type="file" accept=".csv,text/csv"
              onChange={(e) => setDemographics(e.target.files?.[0] || null)} />
            <p className="muted">The server checks required MELD covariates, duplicate or missing
              subjects, allowed values, and cohort variance.</p>
            <button className="btn" disabled={busy === "demographics" || !demographics}>
              {busy === "demographics" ? "Validating…" : "Validate demographics"}</button>
          </form>
        </div>
      </section>}

      {uploads.length > 0 && <section className="panel" aria-labelledby="uploads-title">
        <h2 id="uploads-title">Browser ingestion status</h2>
        <p className="muted">After an upload is imported, copy its StudyInstanceUID values into
          the selection form above and pair each with the matching deidentified subject key.</p>
        <div className="table-scroll"><table>
          <thead><tr><th>File</th><th>Status</th><th>Progress</th><th>Imported studies</th><th>Error</th></tr></thead>
          <tbody>{uploads.map((upload) => <tr key={upload.id}>
            <td>{upload.filename}</td><td><Badge status={upload.status} /></td>
            <td>{upload.received_size ?? 0} / {upload.total_size ?? "–"} bytes</td>
            <td className="digest">{upload.import_result?.studies?.length
              ? <>{upload.import_result.studies.map((item) => <div key={item.study_uid}>
                {item.study_uid} · {item.subject_key}</div>)}
                {mutable && <button type="button" className="btn ghost"
                  onClick={() => useImportedMappings(upload)}>Use mappings</button>}</>
              : (upload.import_result?.study_uids?.length
                ? upload.import_result.study_uids.map((uid) => <div key={uid}>{uid}</div>) : "–")}</td>
            <td>{upload.last_error || "–"}
              {upload.import_result?.phase === "rollback_incomplete" &&
                <RollbackResolution upload={upload} busy={busy === `rollback-${upload.id}`}
                  onLoadEvidence={() => api.getHarmonizationUploadRollbackEvidence(id, upload.id)}
                  onResolve={(resolution, reason, evidence) =>
                    resolveRollback(upload, resolution, reason, evidence)} />}</td>
          </tr>)}</tbody>
        </table></div>
      </section>}

      <section className="panel" aria-labelledby="controls-title">
        <div className="section-heading"><div><h2 id="controls-title">Control inventory</h2>
          <p className="muted">Patient identifiers are never shown in this inventory.</p></div>
          {mutable && <button type="button" className="btn danger"
            disabled={busy === "freeze" || !canFreeze}
            onClick={() => action("freeze", () => api.freezeHarmonizationCohort(id),
              "Cohort frozen and ready to build.")}>Freeze cohort</button>}
        </div>
        {mutable && !canFreeze && <p className="warning">Freezing requires at least
          {` ${cohort.data?.min_controls || 20} `}eligible controls and one validated demographics
          row for every included subject.</p>}
        <div className="table-scroll"><table>
          <thead><tr><th>Subject HMAC</th><th>Study</th><th>Fingerprint</th><th>Eligibility</th><th>Reason</th>
            {mutable && <th>Decision</th>}</tr></thead>
          <tbody>{studies.map((study) => (
            <tr key={study.id || study.study_uid}>
              <td className="digest">{study.subject_key_hmac || study.subject_hmac || "–"}</td>
              <td className="digest">{study.study_uid || study.study_key || "–"}</td>
              <td className="digest">{study.acquisition_fingerprint || "Pending"}</td>
              <td><Badge status={study.included === false || study.eligible === false ? "excluded" :
                (study.included || study.eligible ? "eligible" : "pending")} /></td>
              <td>{study.exclusion_reason || "–"}</td>
              {mutable && <td><StudyDecision study={study} busy={busy === `decision-${study.id}`}
                onSave={(included, reason) => decideStudy(study, included, reason)} /></td>}
            </tr>
          ))}
          {!studies.length && <tr><td colSpan={mutable ? 6 : 5} className="muted">No controls imported.</td></tr>}
          </tbody>
        </table></div>
      </section>

      {cohort.data?.status === "frozen" && !builds.some((item) =>
        ["queued", "building", "qc_review", "validated", "active"].includes(item.status)) &&
        <section className="panel" aria-labelledby="build-start-title">
        <h2 id="build-start-title">2. Start harmonization build</h2>
        <form onSubmit={startBuild}>
          <div className="form-grid">
            <div><label htmlFor="builder-digest">Approved builder image digest *</label>
              <input id="builder-digest" className="wide-input" value={builderDigest}
                onChange={(e) => setBuilderDigest(e.target.value)}
                placeholder="registry/image@sha256:..." maxLength="512"
                pattern="[^ @]+@sha256:[0-9a-f]{64}" required /></div>
            <div className="span-all"><label htmlFor="acceptance-criteria">Versioned acceptance criteria (JSON) *</label>
              <textarea id="acceptance-criteria" rows="5" value={criteria}
                onChange={(e) => setCriteria(e.target.value)} required /></div>
          </div>
          <p className="muted">Criteria must come from the approved methodology. Internal
            cross-validation measures stability; it does not replace external validation.</p>
          <button className="btn" disabled={busy === "build"}>
            {busy === "build" ? "Queueing…" : "Queue build"}</button>
        </form>
      </section>}

      {(build.data || builds.length) && <section className="panel" aria-labelledby="build-title">
        <div className="section-heading"><div><h2 id="build-title">3. Build and QC</h2>
          {build.data && <p className="muted">Stage: {build.data.stage || "pending"}</p>}</div>
          {build.data && <Badge status={build.data.status} />}
        </div>
        {build.data && <>
          {pct !== null && <div className="progress" role="progressbar" aria-valuemin="0"
            aria-valuemax="100" aria-valuenow={Math.round(pct)}
            aria-label={`Build progress ${Math.round(pct)}%`}>
            <span style={{ width: `${pct}%` }} /></div>}
          {(build.data.failure_reason || build.data.error_code) &&
            <ErrorBox error={build.data.failure_reason || build.data.error_code} />}
          <dl className="definition-grid">
            <dt>Build ID</dt><dd className="digest">{build.data.id}</dd>
            <dt>Builder image</dt><dd className="digest">{build.data.builder_image_digest || "–"}</dd>
            <dt>Builder adapter SHA-256</dt>
            <dd className="digest">{build.data.builder_adapter_sha256 || "–"}</dd>
            <dt>Started</dt><dd>{build.data.started_at || "–"}</dd>
            <dt>Completed</dt><dd>{build.data.completed_at || "–"}</dd>
          </dl>
          {["queued", "building"].includes(build.data.status)
            && build.data.stage !== "publishing" &&
            <button type="button" className="btn danger" disabled={busy === "cancel"}
              onClick={() => action("cancel", () => api.cancelHarmonizationBuild(buildId),
                "Cancellation requested.")}>Cancel build</button>}
          {build.data.status === "building" && build.data.stage === "publishing" &&
            <p className="muted">Artifact publication is durable and is completing automatic
              reconciliation; cancellation is disabled for this final atomic stage.</p>}
        </>}
        {builds.length > 1 && <div className="table-scroll"><table>
          <thead><tr><th>Build</th><th>Status</th><th>Created</th><th></th></tr></thead>
          <tbody>{builds.map((item) => <tr key={item.id}>
            <td className="digest">{item.id}</td><td><Badge status={item.status} /></td>
            <td>{item.created_at || "–"}</td><td><button type="button" className="btn ghost"
              onClick={() => setBuildId(item.id)}>View</button></td>
          </tr>)}</tbody>
        </table></div>}

        {shouldLoadQc && <div className="qc-report">
          <h3>QC report</h3>
          <ErrorBox error={qc.error} />
          {qc.loading && !qc.data ? <p role="status">Loading QC report…</p> : qc.data ? <>
            <div className="tiles">
              <div className="tile"><b>{qc.data.fold_count ?? qc.data.folds?.length ?? qc.data.folds ?? "–"}</b>folds</div>
              <div className="tile"><b>{qc.data.excluded_count ?? qc.data.exclusions?.length ?? 0}</b>excluded</div>
              <div className="tile"><b>{(qc.data.passed ?? qc.data.all_folds_succeeded) === true ? "pass" :
                ((qc.data.passed ?? qc.data.all_folds_succeeded) === false ? "review" : "–")}</b>configured criteria</div>
            </div>
            <details><summary>Complete PHI-minimized JSON report</summary>
              <pre>{JSON.stringify(qc.data, null, 2)}</pre></details>
          </> : <p className="muted">QC report is not available yet.</p>}
        </div>}

        {build.data?.status === "qc_review" && <div className="action-box">
          <h3>Scientific validation</h3>
          <p className="warning">The internal control-cohort cross-validation above measures
            stability only. Validation still requires an external schema-v1 report with positive,
            negative, and control holdouts and profile-bound evidence hashes.</p>
          <label htmlFor="scientific-validation-file">Load validation JSON</label>
          <input id="scientific-validation-file" type="file" accept=".json,application/json"
            onChange={(e) => loadScientificValidation(e.target.files?.[0])} />
          <label htmlFor="scientific-validation-json">Scientific validation report *</label>
          <textarea id="scientific-validation-json" rows="12" value={scientificValidation}
            onChange={(e) => setScientificValidation(e.target.value)} required />
          <button type="button" className="btn" disabled={busy === "validate"}
            onClick={validateBuild}>Validate candidate</button>
          <div className="subpanel">
            <h3>Reject candidate</h3>
            <label htmlFor="rejection-reason">Scientific rejection reason</label>
            <textarea id="rejection-reason" rows="3" value={rejectionReason}
              onChange={(event) => setRejectionReason(event.target.value)} />
            <label htmlFor="rejection-evidence">Evidence document SHA-256</label>
            <input id="rejection-evidence" className="wide-input" value={rejectionEvidence}
              onChange={(event) => setRejectionEvidence(event.target.value)} />
            <button type="button" className="btn danger" disabled={busy === "reject"}
              onClick={rejectBuild}>Reject and archive cohort</button>
          </div>
        </div>}
        {build.data?.status === "validated" && <div className="action-box">
          <p>Activate the scientifically validated immutable profile for matching research
            studies. The authenticated actor and evidence remain recorded in the audit trail.</p>
          <button type="button" className="btn" disabled={busy === "activate"}
            onClick={() => action("activate", () => api.activateHarmonizationBuild(buildId),
              "Profile activated for matching research studies.")}>Activate profile</button>
        </div>}
        {build.data?.status === "active" && <div className="action-box">
          <h3>Promote into a signed release</h3>
          <p>Generate the profile document, expected-inventory entry, and hash-bound artifact copy
            plan. The result is not trusted until the normal offline release is signed.</p>
          <button type="button" className="btn" disabled={busy === "release-export"}
            onClick={prepareReleaseExport}>Prepare release export</button>
          {releaseExport && <details open><summary>Release export JSON</summary>
            <pre>{JSON.stringify(releaseExport, null, 2)}</pre></details>}
        </div>}
      </section>}
    </div>
  );
}

export { parseStudyRows, progressPercent, scientificReportError };
