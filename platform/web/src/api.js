// Thin API client — same-origin /api (Caddy proxies to FastAPI; §3, §5).
const base = "/api";

async function req(method, path, body) {
  const res = await fetch(base + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status} ${await res.text()}`);
  return res.status === 204 ? null : res.json();
}

export const api = {
  system: () => req("GET", "/system"),
  queue: () => req("GET", "/queue"),
  auditVerify: () => req("GET", "/audit/verify"),
  pause: () => req("POST", "/admin/pause"),
  resume: () => req("POST", "/admin/resume"),

  listCases: () => req("GET", "/cases"),
  getCase: (id) => req("GET", `/cases/${id}`),
  createCase: (body) => req("POST", "/cases", body),
  syncSeries: (id) => req("POST", `/cases/${id}/series/sync`),
  listSeries: (id) => req("GET", `/cases/${id}/series`),
  confirmSeries: (id, roles) => req("POST", `/cases/${id}/series/confirm`, { roles }),
  buildRecipe: (id, workup) => req("POST", `/cases/${id}/recipe`, { workup }),
  getRecipe: (id) => req("GET", `/cases/${id}/recipe`),
  confirmRecipe: (id) => req("POST", `/cases/${id}/recipe/confirm`),
  listRuns: (id) => req("GET", `/cases/${id}/runs`),
  getRun: (id) => req("GET", `/runs/${id}`),
  adjudicate: (runId, body) => req("POST", `/runs/${runId}/adjudication`, body),
  summary: (id) => req("GET", `/cases/${id}/summary`),
  concordance: (id) => req("GET", `/cases/${id}/concordance`),
  frameUrl: (runId, name) => `/api/runs/${runId}/frames/${name}`,
};

export const SERIES_ROLES = [
  "t1_uni", "t1_inv1", "t1_inv2", "t1_mprage", "flair", "t2", "unknown",
];
export const WORKUPS = ["fcd", "hs", "both"];
