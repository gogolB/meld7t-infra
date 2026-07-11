import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Routes, Route, NavLink, Navigate } from "react-router-dom";
import "./styles.css";
import Dashboard from "./pages/Dashboard.jsx";
import Submit from "./pages/Submit.jsx";
import CaseView from "./pages/CaseView.jsx";
import Review from "./pages/Review.jsx";
import MDT from "./pages/MDT.jsx";
import Admin from "./pages/Admin.jsx";
import { api } from "./api.js";

function Shell() {
  const [me, setMe] = useState(null);
  useEffect(() => { api.me().then(setMe).catch(() => setMe({ roles: [] })); }, []);
  const isAdmin = me?.roles?.includes("admin");
  return (
    <HashRouter>
      <header className="topbar">
        <div className="brand">MELD&nbsp;7T <span>Platform</span></div>
        <nav>
          <NavLink to="/dashboard">Dashboard</NavLink>
          {isAdmin && <NavLink to="/submit">Intake</NavLink>}
          {isAdmin && <NavLink to="/admin">Admin</NavLink>}
        </nav>
        <div className="disclaimer">Research use only — not for diagnosis or treatment</div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/submit" element={isAdmin ? <Submit /> :
            (me ? <Navigate to="/dashboard" replace /> : null)} />
          <Route path="/cases/:id" element={<CaseView />} />
          <Route path="/cases/:id/mdt" element={<MDT />} />
          <Route path="/runs/:runId/review" element={<Review />} />
          <Route path="/admin" element={isAdmin ? <Admin /> :
            (me ? <Navigate to="/dashboard" replace /> : null)} />
        </Routes>
      </main>
    </HashRouter>
  );
}

createRoot(document.getElementById("root")).render(<Shell />);
