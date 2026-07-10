import React from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Routes, Route, NavLink, Navigate } from "react-router-dom";
import "./styles.css";
import Dashboard from "./pages/Dashboard.jsx";
import Submit from "./pages/Submit.jsx";
import CaseView from "./pages/CaseView.jsx";
import Review from "./pages/Review.jsx";
import Admin from "./pages/Admin.jsx";

function Shell() {
  return (
    <HashRouter>
      <header className="topbar">
        <div className="brand">MELD&nbsp;7T <span>Platform</span></div>
        <nav>
          <NavLink to="/dashboard">Dashboard</NavLink>
          <NavLink to="/submit">Submit</NavLink>
          <NavLink to="/admin">Admin</NavLink>
        </nav>
        <div className="disclaimer">Research / hypothesis-generating — not diagnostic</div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/submit" element={<Submit />} />
          <Route path="/cases/:id" element={<CaseView />} />
          <Route path="/runs/:runId/review" element={<Review />} />
          <Route path="/admin" element={<Admin />} />
        </Routes>
      </main>
    </HashRouter>
  );
}

createRoot(document.getElementById("root")).render(<Shell />);
