import React from "react";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  reviewStudy: vi.fn(),
  me: vi.fn(),
  adjudicate: vi.fn(),
  requestCaseReport: vi.fn(),
  frameUrl: vi.fn((runId, name) => `/api/runs/${runId}/frames/${name}`),
}));

vi.mock("../api.js", () => ({ api: mocks }));

import Review from "./Review.jsx";

const warning = "UNHARMONIZED RESEARCH RESULT: no scanner/protocol harmonization profile was applied.";

function payload() {
  const run = (id, detector, harmonization = { mode: "harmonized", applied: true,
    profile: { code: "HMRI7T", version: 1, method: "test" } }) => ({
    run: { id, detector_id: detector, source_role: detector === "hippunfold" ? "t2" : "t1_uni",
      status: "review_ready", harmonization,
      warnings: harmonization.mode === "unharmonized" ? [warning] : [] },
    result: { orthanc_study_uid: "2.25.200", metric_schema: {}, derived_series: [],
      has_report: true },
    clusters: [], frames: [], adjudications: [],
  });
  return {
    case: { id: "case-1", pseudonym: "HMRI-001", status: "review_ready" },
    source_series: [
      { id: "s1", orthanc_series_uid: "1.2.3.1", series_description: "MP2RAGE UNI",
        modality: "MR", instance_count: 240, confirmed_role: "t1_uni" },
      { id: "s2", orthanc_series_uid: "1.2.3.2", series_description: "T2 SPACE",
        modality: "MR", instance_count: 220, confirmed_role: "t2" },
      { id: "s3", orthanc_series_uid: "1.2.3.3", series_description: "Original localizer",
        modality: "MR", instance_count: 3, proposed_role: "unknown", active: false },
    ],
    viewer_studies: [
      { study_uid: "1.2.3", kind: "source", label: "Uploaded source scans" },
      { study_uid: "2.25.200", kind: "derived",
        label: "Combined MAP / MELD / HS derived outputs",
        detector_ids: ["map", "meld_fcd", "hippunfold"] },
    ],
    warnings: [warning],
    reports: [{ id: "report-1", kind: "preliminary", version: 1, status: "ready",
      snapshot_sha256: "a".repeat(64), download_url: "/api/report.pdf" }],
    runs: [
      run("run-map", "map", { mode: "unharmonized", applied: false, profile: null }),
      run("run-meld", "meld_fcd"),
      run("run-hs", "hippunfold", { mode: "not_applicable", applied: false, profile: null }),
    ],
  };
}

function renderPage() {
  return render(<MemoryRouter initialEntries={["/cases/case-1/review"]}>
    <Routes><Route path="/cases/:id/review" element={<Review />} /></Routes>
  </MemoryRouter>);
}

describe("Review Study", () => {
  beforeEach(() => {
    mocks.reviewStudy.mockResolvedValue(payload());
    mocks.me.mockResolvedValue({ subject: "researcher", roles: ["submitter"] });
  });

  it("combines every source scan, detector family, warning, and report", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "HMRI-001" })).toBeInTheDocument();
    expect(screen.getByText("MP2RAGE UNI")).toBeInTheDocument();
    expect(screen.getByText("T2 SPACE")).toBeInTheDocument();
    expect(screen.getByText("Original localizer")).toBeInTheDocument();
    expect(screen.getByText("No longer present")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Combined MAP \/ MELD \/ HS derived outputs/i }))
      .toBeInTheDocument();
    const detectors = within(screen.getByRole("tablist", { name: "Detector results" }));
    expect(detectors.getByRole("tab", { name: /MAP/ })).toBeInTheDocument();
    expect(detectors.getByRole("tab", { name: /MELD FCD/ })).toBeInTheDocument();
    expect(detectors.getByRole("tab", { name: /HippUnfold HS/ })).toBeInTheDocument();
    expect(screen.getAllByText(/UNHARMONIZED RESEARCH RESULT/).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /Open PDF/ })).toHaveAttribute(
      "href", "/api/report.pdf");
    expect(screen.queryByRole("link", { name: /Detector-native report/ }))
      .not.toBeInTheDocument();
    expect(screen.getByText(/Recording an adjudication requires the reviewer role/))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate preliminary" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate final" })).not.toBeInTheDocument();
  });

  it("allows reviewers, but not read-only users, to request a new report version", async () => {
    mocks.me.mockResolvedValueOnce({ subject: "reviewer", roles: ["reviewer"] });
    renderPage();
    expect(await screen.findByRole("button", { name: "Generate preliminary" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate final" })).toBeInTheDocument();
  });

  it("does not present a pending plan slot as a negative detector result", async () => {
    const data = payload();
    data.warnings = [];
    data.runs[0] = {
      ...data.runs[0], result: null, clusters: [],
      run: { ...data.runs[0].run, status: "pending", warnings: [] },
    };
    mocks.reviewStudy.mockResolvedValueOnce(data);
    renderPage();
    expect(await screen.findByText(/declared in the processing plan but was not run/i))
      .toBeInTheDocument();
    expect(screen.queryByText(/No findings above this detector/)).not.toBeInTheDocument();
    expect(screen.getByText(/no detector result to adjudicate/i)).toBeInTheDocument();
  });
});
