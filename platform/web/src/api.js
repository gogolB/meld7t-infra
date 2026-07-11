// Thin API client — same-origin /api (Caddy proxies to FastAPI; §3, §5).
const base = "/api";

async function req(method, path, body) {
  const res = await fetch(base + path, {
    method,
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(!["GET", "HEAD", "OPTIONS"].includes(method) ? { "X-MELD-CSRF": "1" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status} ${await res.text()}`);
  return res.status === 204 ? null : res.json();
}

export const api = {
  system: () => req("GET", "/system"),
  queue: () => req("GET", "/queue"),
  auditVerify: () => req("POST", "/audit/verify"),
  me: () => req("GET", "/me"),
  pause: () => req("POST", "/admin/pause"),
  resume: () => req("POST", "/admin/resume"),

  listCases: () => req("GET", "/cases"),
  getCase: (id) => req("GET", `/cases/${id}`),
  createCase: (body) => req("POST", "/cases", body),
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
  concordance: (id) => req("GET", `/cases/${id}/concordance`),
  harmonizationProfiles: (status = "active") => req("GET", status
    ? `/harmonization/profiles?status=${encodeURIComponent(status)}` : "/harmonization/profiles"),
  harmonizationCandidates: (id) => req("GET", `/cases/${id}/harmonization/candidates`),
  harmonizationAssignments: (id) => req("GET", `/cases/${id}/harmonization/assignments`),
  assignHarmonization: (id, body) => req("POST", `/cases/${id}/harmonization/assign`, body),
  createHarmonizationProfile: (body) => req("POST", "/harmonization/profiles", body),
  validateHarmonizationProfile: (id) => req("POST", `/harmonization/profiles/${id}/validate`),
  activateHarmonizationProfile: (id) => req("POST", `/harmonization/profiles/${id}/activate`),
  retireHarmonizationProfile: (id) => req("POST", `/harmonization/profiles/${id}/retire`),
  frameUrl: (runId, name) => `/api/runs/${runId}/frames/${name}`,
};

export const SERIES_ROLES = [
  "t1_uni", "t1_inv1", "t1_inv2", "t1_mprage", "flair", "t2", "unknown",
];
export const WORKUPS = ["fcd", "hs", "both"];
