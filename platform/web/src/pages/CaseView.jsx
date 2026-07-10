import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, SERIES_ROLES, WORKUPS } from "../api.js";
import { Badge, useAsync, ErrorBox } from "../components.jsx";

export default function CaseView() {
  const { id } = useParams();
  const c = useAsync(() => api.getCase(id), [id]);
  const series = useAsync(() => api.listSeries(id), [id]);
  const runs = useAsync(() => api.listRuns(id), [id], 4000);
  const [recipe, setRecipe] = useState(null);
  const [roles, setRoles] = useState({});
  const [workup, setWorkup] = useState("fcd");
  const [err, setErr] = useState(null);

  const roleFor = (s) => roles[s.orthanc_series_uid] ?? s.confirmed_role ?? s.proposed_role;
  const wrap = (fn) => async () => { setErr(null); try { await fn(); } catch (e) { setErr(e.message); } };

  const sync = wrap(async () => { await api.syncSeries(id); series.reload(); });
  const confirm = wrap(async () => {
    const payload = {}; (series.data || []).forEach((s) => { payload[s.orthanc_series_uid] = roleFor(s); });
    await api.confirmSeries(id, payload); series.reload(); c.reload();
  });
  const build = wrap(async () => { setRecipe(await api.buildRecipe(id, workup)); c.reload(); });
  const run = wrap(async () => { await api.confirmRecipe(id); runs.reload(); c.reload(); setRecipe(null); });

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

      <h2>2 · Recipe <span className="muted">(detectors × sources, §25.1)</span></h2>
      <div className="panel">
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginBottom: 10 }}>
          <div>
            <label>Workup</label>
            <select value={workup} onChange={(e) => setWorkup(e.target.value)}>
              {WORKUPS.map((w) => <option key={w} value={w}>{w.toUpperCase()}</option>)}
            </select>
          </div>
          <button className="btn ghost" onClick={build}>Build recipe</button>
          {recipe && <button className="btn" onClick={run}>Confirm &amp; run →</button>}
        </div>
        {recipe && (
          <>
            <p className="muted">
              Will run <b>{recipe.summary.will_run}</b>, pending <b>{recipe.summary.pending}</b>
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

      <h2>3 · Runs</h2>
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
