import React from "react";
import { Navigate, useParams } from "react-router-dom";
import { api } from "../api.js";
import { useAsync, ErrorBox } from "../components.jsx";

export default function RunReviewRedirect() {
  const { runId } = useParams();
  const run = useAsync(() => api.getRun(runId), [runId]);
  if (run.error) return <ErrorBox error={run.error} />;
  if (!run.data?.run) return <div className="panel muted">Loading review study…</div>;
  return <Navigate replace to={`/cases/${run.data.run.case_id}/review?run=${runId}`} />;
}
