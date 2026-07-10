import React from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.js";
import { Badge, useAsync, ErrorBox } from "../components.jsx";

export default function MDT() {
  const { id } = useParams();
  const s = useAsync(() => api.summary(id), [id]);
  const d = s.data;
  const conc = d?.concordance;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
        <h1>MDT summary — {d?.case?.pseudonym || "…"}</h1>
        {d?.case && <Badge status={d.case.status} />}
        <Link to={`/cases/${id}`} className="muted" style={{ marginLeft: "auto" }}>edit case →</Link>
      </div>
      <p className="muted">Workup {d?.case?.workup?.toUpperCase() || "–"} · presentable case summary for conference.
        <b> Research / hypothesis-generating — not diagnostic; the reviewer read is the record.</b></p>
      <ErrorBox error={s.error} />

      <h2>Detector concordance <span className="muted">(§25.6)</span></h2>
      <div className="panel">
        {conc && (
          <p>
            <b>{conc.detectors_with_findings}</b> detector run(s) with findings ·{" "}
            {conc.concordant_regions > 0
              ? <span className="ok-chip">{conc.concordant_regions} concordant region(s) — a strong surgical signal</span>
              : <span style={{ color: "var(--warn)", fontWeight: 600 }}>0 concordant regions — findings DISCORDANT across sources (adjudicate carefully)</span>}
          </p>
        )}
        {conc?.regions?.length ? (
          <table>
            <thead>
              <tr>
                <th>Flagged region</th>
                {conc.runs.map((r) => <th key={r.run_id}>{r.detector}·{r.source_role || "?"}</th>)}
                <th>Concordant</th>
              </tr>
            </thead>
            <tbody>
              {conc.regions.map((rg, i) => (
                <tr key={i}>
                  <td><b>{rg.hemi}</b> {rg.location}</td>
                  {conc.runs.map((r) => (
                    <td key={r.run_id}>{rg.by_run[r.run_id] != null
                      ? <span className="pill">conf {rg.by_run[r.run_id]}</span>
                      : <span className="muted">–</span>}</td>
                  ))}
                  <td>{rg.concordant ? <span className="ok-chip">✓ concordant</span> : <span className="muted">single source</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <span className="muted">No clusters flagged by any detector (§21).</span>}
      </div>

      <h2>Per-detector results</h2>
      {(d?.runs || []).map((rr) => (
        <div className="panel" key={rr.run.id}>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span className="detector" style={{ fontSize: 15 }}>{rr.run.detector_id}</span>
            <span className="pill">{rr.run.source_role || "–"}</span>
            <Badge status={rr.run.status} />
            {rr.result?.report_path &&
              <a className="btn ghost" href={`/api/runs/${rr.run.id}/report`} target="_blank" rel="noreferrer"
                 style={{ marginLeft: "auto" }}>MELD PDF ↗</a>}
            <Link className="btn ghost" to={`/runs/${rr.run.id}/review`}>Open in viewer →</Link>
          </div>

          {rr.clusters?.length ? (
            <p className="muted" style={{ margin: "8px 0" }}>
              {rr.clusters.map((c) => `#${c.index} ${c.hemi} ${c.location} (size ${c.size}, conf ${c.confidence})`).join(" · ")}
            </p>
          ) : <p className="muted">No clusters above operating point.</p>}

          {rr.frames?.length > 0 && (
            <div className="tiles" style={{ marginTop: 6 }}>
              {rr.frames.filter((f) => !f.startsWith("saliency")).map((f) => (
                <img key={f} src={`/api/runs/${rr.run.id}/frames/${f}`} alt={f}
                     style={{ maxWidth: 340, borderRadius: 6, border: "1px solid var(--line)" }} />
              ))}
            </div>
          )}
        </div>
      ))}

      <h2>Adjudications <span className="muted">(append-only, immudb, §24)</span></h2>
      <div className="panel">
        {d?.adjudications?.length ? (
          <table>
            <thead><tr><th>Reviewer</th><th>Assessment</th><th>Confidence</th><th>Ground truth</th><th>When</th></tr></thead>
            <tbody>
              {d.adjudications.map((a) => (
                <tr key={a.id}>
                  <td>{a.reviewer}</td>
                  <td>{a.agree ? "Agree" : "Disagree"}</td>
                  <td>{a.confidence ?? "–"}</td>
                  <td>{a.ground_truth || "–"}</td>
                  <td className="muted">{(a.ts || "").slice(0, 16).replace("T", " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <span className="muted">No adjudications yet — open a run in the viewer to record one.</span>}
      </div>
    </div>
  );
}
