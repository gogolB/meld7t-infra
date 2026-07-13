import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Routes, Route, NavLink, Navigate } from "react-router-dom";
import "./styles.css";
import "./design-system.css";
import Dashboard from "./pages/Dashboard.jsx";
import Submit from "./pages/Submit.jsx";
import CaseView from "./pages/CaseView.jsx";
import Review from "./pages/Review.jsx";
import RunReviewRedirect from "./pages/RunReviewRedirect.jsx";
import MDT from "./pages/MDT.jsx";
import Admin from "./pages/Admin.jsx";
import HarmonizationCohort from "./pages/HarmonizationCohort.jsx";
import { api } from "./api.js";
import { BrandIdentity, BrandingProvider, useBranding } from "./branding.jsx";
import { ThemeControl, ThemeProvider } from "./theme.jsx";

const RESEARCH_NOTICE = "Research use only — not for diagnosis or treatment";

function Shell() {
  const [me, setMe] = useState(null);
  const branding = useBranding();
  useEffect(() => { api.me().then(setMe).catch(() => setMe({ roles: [] })); }, []);
  const isAdmin = me?.roles?.includes("admin");
  const canSubmit = isAdmin || me?.roles?.includes("submitter");
  return (
    <HashRouter>
      <a className="skip-link" href="#main-content" onClick={(event) => {
        event.preventDefault(); document.getElementById("main-content")?.focus();
      }}>Skip to main content</a>
      <header className="site-header">
        <div className="header-inner">
          <NavLink className="brand-link" to="/dashboard" aria-label={`${branding.productName} dashboard`}>
            <BrandIdentity />
          </NavLink>
          <nav className="primary-nav" aria-label="Primary navigation">
            <NavLink to="/dashboard">Dashboard</NavLink>
            {canSubmit && <NavLink to="/submit">Intake</NavLink>}
            {isAdmin && <NavLink to="/admin">Admin</NavLink>}
          </nav>
          <div className="header-actions">
            <span className="research-label">Research only</span>
            <ThemeControl />
          </div>
        </div>
      </header>
      <main id="main-content" tabIndex="-1">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/submit" element={canSubmit ? <Submit /> :
            (me ? <Navigate to="/dashboard" replace /> : null)} />
          <Route path="/cases/:id" element={<CaseView />} />
          <Route path="/cases/:id/mdt" element={<MDT />} />
          <Route path="/cases/:id/review" element={<Review />} />
          <Route path="/runs/:runId/review" element={<RunReviewRedirect />} />
          <Route path="/admin" element={isAdmin ? <Admin /> :
            (me ? <Navigate to="/dashboard" replace /> : null)} />
          <Route path="/admin/harmonization/cohorts/:id" element={isAdmin ?
            <HarmonizationCohort /> : (me ? <Navigate to="/dashboard" replace /> : null)} />
        </Routes>
      </main>
      <footer className="site-footer">
        <div><strong>{branding.institutionName}</strong><span>{branding.footerText}</span></div>
        <span>{RESEARCH_NOTICE}</span>
      </footer>
    </HashRouter>
  );
}

createRoot(document.getElementById("root")).render(
  <BrandingProvider>
    <ThemeProvider><Shell /></ThemeProvider>
  </BrandingProvider>,
);
