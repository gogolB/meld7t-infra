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
  const [recipe, setRecipe] = useState(null);
  const [roles, setRoles] = useState({});
  const [workup, setWorkup] = useState("fcd");
  const [profileSelections, setProfileSelections] = useState({});
  const [allowUnharmonized, setAllowUnharmonized] = useState(false);
  const [unharmonizedReason, setUnharmonizedReason] = useState("");
  const [err, setErr] = useState(null);

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
      unharmonized_reason: allowUnharmonized ? unharmonizedReason : null,
    }));
    c.reload();
  });
  const run = wrap(async () => { await api.confirmRecipe(id); runs.reload(); c.reload(); setRecipe(null); });
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

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
        <h1>{c.data?.pseudonym || "Case"} <Badge status={c.data?.status || "created"} /></h1>
        <Link to={`/cases/${id}/mdt`} className="btn ghost" style={{ marginLeft: "auto" }}>MDT summary →</Link>
      </div>
      <p className="muted">{id}</p>
      <ErrorBox error={err} />

      <h2>1 · Input series <span className="muted">(propose → confirm, §16)</span></h2>
      <div className="panel">
        <ErrorBox error={series.error} />
        <table>
          <thead><tr><th>Series description</th><th>Modality</th><th>Proposed</th><th>Confirm role</th></tr></thead>
          <tbody>
            {(series.data || []).map((s) => (
              <tr key={s.id}>
                <td>{s.series_description || <span className="muted">—</span>}</td>
                <td>{s.modality || "–"}</td>
                <td><span className="pill">{s.proposed_role}</span></td>
                <td>
                  <select value={roleFor(s)}
                    onChange={(e) => setRoles({ ...roles, [s.orthanc_series_uid]: e.target.value })}>
                    {SERIES_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
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
        <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
          {c.data?.orthanc_study_uid && <button className="btn ghost" onClick={sync}>Sync from Orthanc</button>}
          <button className="btn" disabled={!series.data?.length} onClick={confirm}>Confirm series</button>
        </div>
      </div>

      <h2>2 · Harmonization <span className="muted">(scanner + protocol → versioned profile)</span></h2>
      <div className="panel">
        <ErrorBox error={harmo.error} />
        <p className="muted">Profiles are proposed from minimized, protected scanner/protocol
          metadata. A researcher confirms each detector/source assignment; the worker verifies
          artifact hashes again.</p>
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
                  <td>{assigned ? <span className="ok-chip">✓ confirmed</span> : (
                    target.candidates?.length ? <select
                      value={profileSelections[key] ||
                        (target.ambiguous_top ? "" : target.candidates[0].profile.id)}
                      onChange={(e) => setProfileSelections({ ...profileSelections, [key]: e.target.value })}>
                      {target.ambiguous_top && <option value="">Choose explicitly…</option>}
                      {target.candidates.map((candidate) => <option key={candidate.profile.id}
                        value={candidate.profile.id}>{candidate.profile.code} v{candidate.profile.version}
                        {` (score ${candidate.score})`}</option>)}
                      </select> : <span className="err">No matching active profile</span>
                  )}
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
                  <td>{!assigned && target.candidates?.length > 0 &&
                    <button className="btn ghost" onClick={assign(target)}>Confirm profile</button>}</td>
                </tr>
              );
            })}</tbody>
          </table>
        ) : <span className="muted">Confirm series roles to calculate profile candidates.</span>}
        <div style={{ marginTop: 14 }}>
          <label><input type="checkbox" checked={allowUnharmonized}
            onChange={(e) => setAllowUnharmonized(e.target.checked)} /> Allow an explicitly
            unharmonized exploratory run</label>
          {allowUnharmonized && <input value={unharmonizedReason}
            onChange={(e) => setUnharmonizedReason(e.target.value)}
            placeholder="Research rationale (minimum 10 characters)" />}
        </div>
      </div>

      <h2>3 · Recipe <span className="muted">(detectors × exact sources)</span></h2>
      <div className="panel">
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginBottom: 10 }}>
          <div>
            <label>Workup</label>
            <select value={workup} onChange={(e) => setWorkup(e.target.value)}>
              {WORKUPS.map((w) => <option key={w} value={w}>{w.toUpperCase()}</option>)}
            </select>
          </div>
          <button className="btn ghost" disabled={allowUnharmonized && unharmonizedReason.trim().length < 10}
            onClick={build}>Build recipe</button>
          {recipe && <button className="btn" disabled={recipe.summary.blocked > 0}
            onClick={run}>Confirm &amp; run →</button>}
        </div>
        {recipe && (
          <>
            <p className="muted">
              Will run <b>{recipe.summary.will_run}</b>, pending <b>{recipe.summary.pending}</b>
              {recipe.summary.blocked > 0 && <> · <span style={{ color: "var(--bad)" }}>
                blocked <b>{recipe.summary.blocked}</b> (resolve before confirmation)</span></>}
              {recipe.summary.tandem && <> · <span className="badge b-built">tandem</span></>}
            </p>
            <table>
              <thead><tr><th>Detector</th><th>Source</th><th>Plan</th><th>Note</th></tr></thead>
              <tbody>
                {recipe.recipe.spec.map((e, i) => (
                  <tr key={i}>
                    <td className="detector">{e.detector_label}</td>
                    <td>{e.source_role || "–"}</td>
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
                  <Link className="btn ghost" to={`/runs/${r.id}/review`}>Review →</Link>}</td>
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
