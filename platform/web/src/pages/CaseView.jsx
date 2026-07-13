import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, SERIES_ROLES, WORKUPS } from "../api.js";
import { Badge, useAsync, ErrorBox } from "../components.jsx";

export default function CaseView() {
  const { id } = useParams();
  const c = useAsync(() => api.getCase(id), [id]);
  const series = useAsync(() => api.listSeries(id), [id]);
  const harmo = useAsync(() => api.harmonizationCandidates(id), [id]);
  const runs = useAsync(() => api.listRuns(id), [id], 15000);
  const persistedRecipe = useAsync(() => api.getRecipe(id), [id]);
  const [recipe, setRecipe] = useState(null);
  const [roles, setRoles] = useState({});
  const [workup, setWorkup] = useState("both");
  const [profileSelections, setProfileSelections] = useState({});
  const [allowUnharmonized, setAllowUnharmonized] = useState(true);
  const [err, setErr] = useState(null);

  const canMutate = c.data?.permissions?.can_mutate === true;
  const displayedRecipe = recipe || persistedRecipe.data;
  const recipeStudyUids = [...new Set((displayedRecipe?.recipe?.spec || []).flatMap(
    (entry) => (entry.inputs || []).map((input) => input.study_uid).filter(Boolean)))];
  const displayedStudyUids = recipeStudyUids.length ? recipeStudyUids
    : (c.data?.orthanc_study_uid ? [c.data.orthanc_study_uid] : []);
  const roleFor = (s) => roles[s.orthanc_series_uid] ?? s.confirmed_role ?? s.proposed_role;
  const wrap = (fn) => async () => { setErr(null); try { await fn(); } catch (e) { setErr(e.message); } };

  const sync = wrap(async () => { await api.syncSeries(id); series.reload(); });
  const confirm = wrap(async () => {
    const payload = {}; (series.data || []).forEach((s) => { payload[s.orthanc_series_uid] = roleFor(s); });
    await api.confirmSeries(id, payload); series.reload(); harmo.reload(); c.reload();
  });
  const build = wrap(async () => {
    setRecipe(await api.buildRecipe(id, workup, {
      allow_unharmonized: allowUnharmonized,
      unharmonized_reason: null,
    }));
    c.reload();
  });
  const run = wrap(async () => {
    await api.confirmRecipe(id); runs.reload(); c.reload(); persistedRecipe.reload(); setRecipe(null);
  });
  const assign = (target) => wrap(async () => {
    const key = `${target.detector_id}:${target.source_series_uid}`;
    const profileId = profileSelections[key] ||
      (!target.ambiguous_top ? target.candidates?.[0]?.profile?.id : null);
    if (!profileId) throw new Error("No active profile matches this scanner/protocol target");
    await api.assignHarmonization(id, {
      profile_id: profileId,
      detector_id: target.detector_id,
      source_series_uid: target.source_series_uid,
    });
    harmo.reload(); c.reload();
  });
  const seriesName = (uid) => {
    const item = (series.data || []).find((row) => row.orthanc_series_uid === uid);
    return item?.series_description || uid;
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
        <h1>{c.data?.pseudonym || "Case"} <Badge status={c.data?.status || "created"} /></h1>
        <div className="button-row" style={{ marginLeft: "auto" }}>
          <Link to={`/cases/${id}/review`} className="btn ghost">Review study →</Link>
          <Link to={`/cases/${id}/mdt`} className="btn ghost">Research summary →</Link>
        </div>
      </div>
      <p className="muted">{id}</p>
      <ErrorBox error={err} />
      {c.data && !canMutate && <div className="notice">Read-only case access. The case creator,
        assignee, or an administrator with intake permission manages series roles and processing
        plans.</div>}

      <h2>1 · Input series <span className="muted">(propose → confirm, §16)</span></h2>
      <div className="panel">
        <ErrorBox error={series.error} />
        <table>
          <thead><tr><th>Series description</th><th>Modality</th><th>Proposed</th>
            <th>{canMutate ? "Confirm role" : "Confirmed role"}</th></tr></thead>
          <tbody>
            {(series.data || []).map((s) => (
              <tr key={s.id}>
                <td>{s.series_description || <span className="muted">—</span>}</td>
                <td>{s.modality || "–"}</td>
                <td><span className="pill">{s.proposed_role}</span></td>
                <td>
                  {canMutate ? <select value={roleFor(s)}
                    onChange={(e) => setRoles({ ...roles, [s.orthanc_series_uid]: e.target.value })}>
                    {SERIES_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select> : <span className="pill">{s.confirmed_role || s.proposed_role}</span>}
                </td>
              </tr>
            ))}
            {series.data && !series.data.length && (
              <tr><td colSpan="4" className="muted">
                No series yet. {c.data?.orthanc_study_uid ? "Sync from Orthanc →" : "Ingest a study to sync."}
              </td></tr>
            )}
          </tbody>
        </table>
        {canMutate && <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
          {c.data?.orthanc_study_uid && <button className="btn ghost" onClick={sync}>Sync from Orthanc</button>}
          <button className="btn" disabled={!series.data?.length} onClick={confirm}>Confirm series</button>
        </div>}
      </div>

      <h2>2 · Harmonization <span className="muted">(optional scanner + protocol profile)</span></h2>
      <div className="panel">
        <ErrorBox error={harmo.error} />
        <p className="muted">Profiles are proposed from scanner/protocol metadata. Confirm a
          matching versioned profile where available, or explicitly include unharmonized runs in
          the plan. Unharmonized outputs remain clearly marked in review and every PDF.</p>
        {harmo.data?.targets?.length ? (
          <table>
            <thead><tr><th>Detector</th><th>Source</th><th>Fingerprint</th><th>Profile</th><th></th></tr></thead>
            <tbody>{harmo.data.targets.map((target) => {
              const key = `${target.detector_id}:${target.source_series_uid}`;
              const assigned = target.assignment?.status === "confirmed" &&
                !target.assignment?.stale;
              return (
                <tr key={key}>
                  <td className="detector">{target.detector_id}</td>
                  <td>{target.source_role}<br /><span className="muted">{target.source_series_uid}</span></td>
                  <td className="muted">{target.fingerprint?.slice(0, 12) || "missing"}</td>
                  <td>{assigned ? <span className="ok-chip">✓ confirmed</span> : canMutate ? (
                    target.candidates?.length ? <select
                      value={profileSelections[key] ||
                        (target.ambiguous_top ? "" : target.candidates[0].profile.id)}
                      onChange={(e) => setProfileSelections({ ...profileSelections, [key]: e.target.value })}>
                      {target.ambiguous_top && <option value="">Choose explicitly…</option>}
                      {target.candidates.map((candidate) => <option key={candidate.profile.id}
                        value={candidate.profile.id}>{candidate.profile.code} v{candidate.profile.version}
                        {` (score ${candidate.score})`}</option>)}
                      </select> : <span className="err">No matching active profile</span>
                  ) : target.candidates?.length ? <span className="muted">Best available: {
                    target.candidates[0].profile.code} v{target.candidates[0].profile.version}</span> :
                    <span className="muted">No matching active profile</span>}
                    {target.ambiguous_top && !assigned && <div className="err">
                      Equal-scoring profiles require an explicit selection.</div>}
                    <details className="muted" style={{ marginTop: 6 }}>
                      <summary>Match evidence</summary>
                      <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify({
                        candidates: target.candidates?.map((candidate) => ({
                          code: candidate.profile.code,
                          version: candidate.profile.version,
                          score: candidate.score,
                          reasons: candidate.reasons,
                        })),
                      }, null, 2)}</pre>
                    </details>
                  </td>
                  <td>{canMutate && !assigned && target.candidates?.length > 0 &&
                    <button className="btn ghost" onClick={assign(target)}>Confirm profile</button>}</td>
                </tr>
              );
            })}</tbody>
          </table>
        ) : <span className="muted">Confirm series roles to calculate profile candidates.</span>}
        {canMutate && <div style={{ marginTop: 14 }}>
          <label><input type="checkbox" checked={allowUnharmonized}
            onChange={(e) => setAllowUnharmonized(e.target.checked)} /> Include runnable detectors
            without a matching harmonization profile</label>
          {allowUnharmonized && <div className="warning" style={{ marginTop: 8 }}>
            These runs will be labeled <strong>unharmonized</strong> in the queue, Review Study,
            combined reports, and exported DICOM provenance.</div>}
        </div>}
      </div>

      <h2>3 · Recipe <span className="muted">(detectors × exact sources)</span></h2>
      <div className="panel">
        {canMutate ? <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginBottom: 10 }}>
          <div>
            <label>Workup</label>
            <select value={workup} onChange={(e) => setWorkup(e.target.value)}>
              {WORKUPS.map((w) => <option key={w} value={w}>{w.toUpperCase()}</option>)}
            </select>
          </div>
          <button className="btn ghost" onClick={build}>Build processing plan</button>
          {displayedRecipe && !displayedRecipe.recipe.confirmed_at &&
            <button className="btn" disabled={displayedRecipe.summary.blocked > 0}
            onClick={run}>Confirm plan &amp; queue analyses →</button>}
        </div> : <p className="muted">Current confirmed processing plan and exact source scans.</p>}
        {displayedRecipe && (
          <>
            {displayedStudyUids.length > 0 && <div className="notice" style={{ marginBottom: 12 }}>
              Source {displayedStudyUids.length === 1 ? "study" : "studies"} for this processing plan
              {displayedStudyUids.map((studyUid) => <div className="muted digest" key={studyUid}>
                Study Instance UID: {studyUid}</div>)}
            </div>}
            <p className="muted">
              Will run <b>{displayedRecipe.summary.will_run}</b>, pending <b>{
                displayedRecipe.summary.pending}</b>
              {displayedRecipe.summary.blocked > 0 && <> · <span style={{ color: "var(--bad)" }}>
                blocked <b>{displayedRecipe.summary.blocked}</b> (resolve before confirmation)</span></>}
              {displayedRecipe.summary.tandem && <> · <span className="badge b-built">tandem</span></>}
            </p>
            {displayedRecipe.summary.unharmonized > 0 && <div className="warning warning-prominent">
              <strong>{displayedRecipe.summary.unharmonized} unharmonized run(s)</strong> {
                displayedRecipe.recipe.confirmed_at ? "are in this plan." : "will be queued."}
              {canMutate && !displayedRecipe.recipe.confirmed_at &&
                " Confirm the exact inputs below before continuing."}</div>}
            <table>
              <thead><tr><th>Detector</th><th>Exact scans used</th><th>Harmonization</th>
                <th>Plan</th><th>Note</th></tr></thead>
              <tbody>
                {displayedRecipe.recipe.spec.map((e, i) => (
                  <tr key={i}>
                    <td className="detector">{e.detector_label}</td>
                    <td>{e.inputs?.length ? e.inputs.map((input) => <div key={input.role}>
                      <span className="pill">{input.role}</span> {seriesName(input.series_uid)}
                      <div className="muted digest">{input.series_uid}</div>
                    </div>) : <span className="muted">No compatible input</span>}</td>
                    <td>{e.harmonization?.profile_id ? <span className="ok-chip">
                      {e.harmonization.code} v{e.harmonization.version}</span> :
                      e.harmonization?.mode === "not_applicable" ? <span className="pill">
                        Not applicable</span> : <span className="badge b-unharmonized">
                        Unharmonized</span>}</td>
                    <td><Badge status={e.status} /></td>
                    <td className="muted">{e.note || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>

      <h2>4 · Runs</h2>
      <div className="panel">
        <table>
          <thead><tr><th>Detector</th><th>Source</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {(runs.data || []).map((r) => (
              <tr key={r.id}>
                <td className="detector">{r.detector_id}</td>
                <td>{r.source_role || "–"}</td>
                <td><Badge status={r.status} /></td>
                <td>{r.status === "review_ready" &&
                  <Link className="btn ghost" to={`/cases/${id}/review?run=${r.id}`}>Review →</Link>}</td>
              </tr>
            ))}
            {runs.data && !runs.data.length && (
              <tr><td colSpan="4" className="muted">No runs — build and confirm a recipe.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
