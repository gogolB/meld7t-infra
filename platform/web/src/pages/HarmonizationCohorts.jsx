import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { Badge, ErrorBox, useAsync } from "../components.jsx";

function rowsFrom(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.items || payload?.cohorts || [];
}

function countFor(cohort, name) {
  const aliases = { total: "studies", eligible: "included" };
  return cohort?.counts?.[name] ?? cohort?.counts?.[aliases[name]]
    ?? cohort?.[`${name}_count`] ?? 0;
}

export default function HarmonizationCohorts() {
  const cohorts = useAsync(() => api.listHarmonizationCohorts(), []);
  const navigate = useNavigate();
  const [showCreate, setShowCreate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [form, setForm] = useState({
    name: "", site_code: "", profile_code: "", profile_version: 1,
    source_role: "t1_mprage", manufacturer: "", model: "", station_name: "",
    software_versions: "",
    protocol_name: "", field_strength_t: "7", min_controls: 20, cv_folds: 5,
  });

  function update(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
  }

  async function create(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const acquisition = {
      manufacturer: form.manufacturer,
      model: form.model,
      station_name: form.station_name,
      protocol_name: form.protocol_name,
      field_strength_t: Number(form.field_strength_t),
      software_versions: {
        eq: form.software_versions.split("\\").map((value) => value.trim()).filter(Boolean),
      },
    };
    try {
      const cohort = await api.createHarmonizationCohort({
        name: form.name.trim(),
        site_code: form.site_code.trim(),
        profile_code: form.profile_code.trim(),
        profile_version: Number(form.profile_version),
        source_role: form.source_role,
        selector: { roles: [form.source_role], acquisition },
        min_controls: Number(form.min_controls),
        cv_folds: Number(form.cv_folds),
      });
      navigate(`/admin/harmonization/cohorts/${cohort.id}`);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  const rows = rowsFrom(cohorts.data);
  return (
    <section className="panel" aria-labelledby="cohort-builder-title">
      <div className="section-heading">
        <div>
          <h2 id="cohort-builder-title">MELD harmonization cohorts</h2>
          <p className="muted">Build an immutable scanner/protocol profile from deidentified
            controls held in the dedicated harmonization Orthanc.</p>
        </div>
        <button className="btn" type="button" onClick={() => setShowCreate((value) => !value)}>
          {showCreate ? "Close" : "New cohort"}
        </button>
      </div>
      <ErrorBox error={cohorts.error || error} />

      {showCreate && (
        <form className="subpanel" onSubmit={create} aria-label="Create harmonization cohort">
          <div className="form-grid">
            <div><label htmlFor="cohort-name">Cohort name *</label>
              <input id="cohort-name" value={form.name}
                onChange={(e) => update("name", e.target.value)} required /></div>
            <div><label htmlFor="site-code">Site code *</label>
              <input id="site-code" value={form.site_code}
                onChange={(e) => update("site_code", e.target.value)} required /></div>
            <div><label htmlFor="profile-code">Profile code *</label>
              <input id="profile-code" value={form.profile_code}
                onChange={(e) => update("profile_code", e.target.value)}
                placeholder="H_SITE_PROTOCOL" maxLength="32"
                pattern="H[A-Za-z0-9][A-Za-z0-9_-]*" required /></div>
            <div><label htmlFor="profile-version">Profile version *</label>
              <input id="profile-version" type="number" min="1" value={form.profile_version}
                onChange={(e) => update("profile_version", e.target.value)} required /></div>
            <div><label htmlFor="manufacturer">Scanner manufacturer *</label>
              <input id="manufacturer" value={form.manufacturer}
                onChange={(e) => update("manufacturer", e.target.value)} required /></div>
            <div><label htmlFor="scanner-model">Scanner model *</label>
              <input id="scanner-model" value={form.model}
                onChange={(e) => update("model", e.target.value)} required /></div>
            <div><label htmlFor="station-name">Scanner station name *</label>
              <input id="station-name" value={form.station_name}
                onChange={(e) => update("station_name", e.target.value)} required /></div>
            <div><label htmlFor="software-version">Software version(s) *</label>
              <input id="software-version" value={form.software_versions}
                onChange={(e) => update("software_versions", e.target.value)}
                placeholder="XA60 (separate multiple values with \\)" required /></div>
            <div><label htmlFor="protocol-name">Protocol name *</label>
              <input id="protocol-name" value={form.protocol_name}
                onChange={(e) => update("protocol_name", e.target.value)} required /></div>
            <div><label htmlFor="field-strength">Field strength (T) *</label>
              <input id="field-strength" type="number" min="0.1" step="0.1"
                value={form.field_strength_t}
                onChange={(e) => update("field_strength_t", e.target.value)} required /></div>
            <div><label htmlFor="source-role">Source role *</label>
              <select id="source-role" value={form.source_role}
                onChange={(e) => update("source_role", e.target.value)}>
                {["t1_uni", "t1_mprage"].map((role) => (
                  <option key={role} value={role}>{role}</option>
                ))}
              </select></div>
            <div><label htmlFor="minimum-controls">Minimum eligible controls</label>
              <input id="minimum-controls" type="number" min="20" value={form.min_controls}
                onChange={(e) => update("min_controls", e.target.value)} /></div>
            <div><label htmlFor="cv-folds">Cross-validation folds</label>
              <input id="cv-folds" type="number" min="2" max="10" value={form.cv_folds}
                onChange={(e) => update("cv_folds", e.target.value)} /></div>
          </div>
          <p className="muted">Scanner and protocol fields become exact selector rules. Broader
            selectors can be introduced only through a reviewed profile version.</p>
          <button className="btn" disabled={busy}>{busy ? "Creating…" : "Create cohort"}</button>
        </form>
      )}

      {cohorts.loading ? <p role="status">Loading cohorts…</p> : rows.length ? (
        <div className="table-scroll"><table>
          <thead><tr><th>Name</th><th>Site</th><th>Profile</th><th>Controls</th><th>Status</th></tr></thead>
          <tbody>{rows.map((cohort) => (
            <tr key={cohort.id}>
              <td><Link to={`/admin/harmonization/cohorts/${cohort.id}`}>{cohort.name}</Link></td>
              <td>{cohort.site_code}</td>
              <td>{cohort.profile_code} v{cohort.profile_version}</td>
              <td>{countFor(cohort, "eligible")} / {countFor(cohort, "total")} eligible</td>
              <td><Badge status={cohort.status} /></td>
            </tr>
          ))}</tbody>
        </table></div>
      ) : <p className="empty-state">No harmonization cohorts have been created.</p>}
    </section>
  );
}
