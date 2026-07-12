// Thin API client — same-origin /api (Caddy proxies to FastAPI; §3, §5).
const base = "/api";

async function responseBody(res) {
  if (res.status === 204) return null;
  const text = await res.text();
  if (!text) return null;
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try { return JSON.parse(text); } catch { /* report the original response below */ }
  }
  return text;
}

async function req(method, path, body, options = {}) {
  const hasBody = body !== undefined && body !== null;
  const res = await fetch(base + path, {
    method,
    headers: {
      ...(hasBody ? { "Content-Type": options.contentType || "application/json" } : {}),
      ...(!["GET", "HEAD", "OPTIONS"].includes(method) ? { "X-MELD-CSRF": "1" } : {}),
      ...(options.headers || {}),
    },
    body: hasBody && !options.raw ? JSON.stringify(body) : (hasBody ? body : undefined),
  });
  const payload = await responseBody(res);
  if (!res.ok) {
    const rawDetail = payload && typeof payload === "object"
      ? (payload.detail || payload.message || payload) : payload;
    const detail = typeof rawDetail === "string" ? rawDetail
      : (rawDetail ? JSON.stringify(rawDetail) : "");
    throw new Error(`${method} ${path} → ${res.status}${detail ? ` ${detail}` : ""}`);
  }
  return payload;
}

export const api = {
  system: () => req("GET", "/system"),
  queue: () => req("GET", "/queue"),
  auditVerify: () => req("POST", "/audit/verify"),
  me: () => req("GET", "/me"),
  branding: () => req("GET", "/branding"),
  pause: () => req("POST", "/admin/pause"),
  resume: () => req("POST", "/admin/resume"),

  listCases: () => req("GET", "/cases"),
  getCase: (id) => req("GET", `/cases/${id}`),
  createCase: (body) => req("POST", "/cases", body),
  createCaseUpload: (body) => req("POST", "/case-uploads", body),
  getCaseUpload: (id) => req("GET", `/case-uploads/${id}`),
  listCaseUploads: () => req("GET", "/case-uploads"),
  uploadCaseChunk: (id, offset, chunk) => req(
    "PUT", `/case-uploads/${id}?offset=${offset}`, chunk,
    { raw: true, contentType: "application/octet-stream" }),
  completeCaseUpload: (id) => req("POST", `/case-uploads/${id}/complete`),
  assignCase: (id, subject) => req("POST", `/admin/cases/${id}/assign`, { subject }),
  syncSeries: (id) => req("POST", `/cases/${id}/series/sync`),
  listSeries: (id) => req("GET", `/cases/${id}/series`),
  confirmSeries: (id, roles) => req("POST", `/cases/${id}/series/confirm`, { roles }),
  buildRecipe: (id, workup, options = {}) => req("POST", `/cases/${id}/recipe`, { workup, ...options }),
  getRecipe: (id) => req("GET", `/cases/${id}/recipe`),
  confirmRecipe: (id) => req("POST", `/cases/${id}/recipe/confirm`),
  listRuns: (id) => req("GET", `/cases/${id}/runs`),
  getRun: (id) => req("GET", `/runs/${id}`),
  adjudicate: (runId, body) => req("POST", `/runs/${runId}/adjudication`, body),
  summary: (id) => req("GET", `/cases/${id}/summary`),
  reviewStudy: (id) => req("GET", `/cases/${id}/review`),
  listCaseReports: (id) => req("GET", `/cases/${id}/reports`),
  requestCaseReport: (id, kind) => req("POST", `/cases/${id}/reports/${kind}`),
  concordance: (id) => req("GET", `/cases/${id}/concordance`),
  harmonizationProfiles: (status = "active") => req("GET", status
    ? `/harmonization/profiles?status=${encodeURIComponent(status)}` : "/harmonization/profiles"),
  harmonizationCandidates: (id) => req("GET", `/cases/${id}/harmonization/candidates`),
  harmonizationAssignments: (id) => req("GET", `/cases/${id}/harmonization/assignments`),
  harmonizationCoverage: () => req("GET", "/harmonization/coverage"),
  assignHarmonization: (id, body) => req("POST", `/cases/${id}/harmonization/assign`, body),
  createHarmonizationProfile: (body) => req("POST", "/harmonization/profiles", body),
  validateHarmonizationProfile: (id) => req("POST", `/harmonization/profiles/${id}/validate`),
  activateHarmonizationProfile: (id) => req("POST", `/harmonization/profiles/${id}/activate`),
  retireHarmonizationProfile: (id) => req("POST", `/harmonization/profiles/${id}/retire`),
  listHarmonizationCohorts: () => req("GET", "/harmonization/cohorts"),
  getHarmonizationCohort: (id) => req("GET", `/harmonization/cohorts/${id}`),
  createHarmonizationCohort: (body) => req("POST", "/harmonization/cohorts", body),
  importHarmonizationStudies: (id, studies) => req(
    "POST", `/harmonization/cohorts/${id}/studies/import`, { studies }),
  decideHarmonizationStudy: (cohortId, studyId, included, exclusionReason = null) => req(
    "POST", `/harmonization/cohorts/${cohortId}/studies/${studyId}/decision`,
    { included, exclusion_reason: exclusionReason }),
  submitHarmonizationDemographics: (id, csv) => req(
    "POST", `/harmonization/cohorts/${id}/demographics`, csv,
    { raw: true, contentType: "text/csv; charset=utf-8" }),
  freezeHarmonizationCohort: (id) => req("POST", `/harmonization/cohorts/${id}/freeze`),
  createHarmonizationUpload: (cohortId, body) => req(
    "POST", `/harmonization/cohorts/${cohortId}/uploads`, body),
  getHarmonizationUpload: (cohortId, uploadId) => req(
    "GET", `/harmonization/cohorts/${cohortId}/uploads/${uploadId}`),
  uploadHarmonizationChunk: (cohortId, uploadId, offset, chunk) => req(
    "PUT", `/harmonization/cohorts/${cohortId}/uploads/${uploadId}?offset=${offset}`,
    chunk, { raw: true, contentType: "application/octet-stream" }),
  completeHarmonizationUpload: (cohortId, uploadId) => req(
    "POST", `/harmonization/cohorts/${cohortId}/uploads/${uploadId}/complete`),
  getHarmonizationUploadRollbackEvidence: (cohortId, uploadId) => req(
    "GET", `/harmonization/cohorts/${cohortId}/uploads/${uploadId}/rollback-evidence`),
  resolveHarmonizationUploadRollback: (cohortId, uploadId, action, reason, evidenceSha256) => req(
    "POST", `/harmonization/cohorts/${cohortId}/uploads/${uploadId}/rollback-resolution`,
    { action, reason, evidence_sha256: evidenceSha256 }),
  createHarmonizationBuild: (cohortId, body) => req(
    "POST", `/harmonization/cohorts/${cohortId}/builds`, body),
  getHarmonizationBuild: (id) => req("GET", `/harmonization/builds/${id}`),
  getHarmonizationBuildQc: (id) => req("GET", `/harmonization/builds/${id}/qc`),
  cancelHarmonizationBuild: (id) => req("POST", `/harmonization/builds/${id}/cancel`),
  validateHarmonizationBuild: (id, scientificValidation) => req(
    "POST", `/harmonization/builds/${id}/validate`,
    { scientific_validation: scientificValidation }),
  rejectHarmonizationBuild: (id, reason, evidenceSha256) => req(
    "POST", `/harmonization/builds/${id}/reject`,
    { reason, evidence_sha256: evidenceSha256 }),
  activateHarmonizationBuild: (id) => req("POST", `/harmonization/builds/${id}/activate`),
  exportHarmonizationBuild: (id) => req(
    "GET", `/harmonization/builds/${id}/release-export`),
  frameUrl: (runId, name) => `/api/runs/${runId}/frames/${name}`,
};

export const SERIES_ROLES = [
  "t1_uni", "t1_inv1", "t1_inv2", "t1_mprage", "flair", "t2", "unknown",
];
export const WORKUPS = ["fcd", "hs", "both"];
