import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getCase: vi.fn(),
  listSeries: vi.fn(),
  harmonizationCandidates: vi.fn(),
  listRuns: vi.fn(),
  getRecipe: vi.fn(),
  syncSeries: vi.fn(),
  confirmSeries: vi.fn(),
  buildRecipe: vi.fn(),
  confirmRecipe: vi.fn(),
  assignHarmonization: vi.fn(),
}));

vi.mock("../api.js", () => ({
  api: mocks,
  SERIES_ROLES: ["t1_mprage", "unknown"],
  WORKUPS: ["fcd", "hs", "both"],
}));

import CaseView from "./CaseView.jsx";

function renderPage() {
  return render(<MemoryRouter initialEntries={["/cases/case-1"]}>
    <Routes><Route path="/cases/:id" element={<CaseView />} /></Routes>
  </MemoryRouter>);
}

describe("Case processing-plan permissions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getCase.mockResolvedValue({
      id: "case-1", pseudonym: "HMRI-001", status: "queued",
      orthanc_study_uid: "1.2.3", permissions: { can_mutate: false },
    });
    mocks.listSeries.mockResolvedValue([{
      id: "series-1", orthanc_series_uid: "1.2.3.1",
      series_description: "T1 MPRAGE", modality: "MR",
      proposed_role: "t1_mprage", confirmed_role: "t1_mprage",
    }]);
    mocks.harmonizationCandidates.mockResolvedValue({ targets: [{
      detector_id: "map", source_series_uid: "1.2.3.1", source_role: "t1_mprage",
      fingerprint: "a".repeat(64), assignment: null, ambiguous_top: false,
      candidates: [{ profile: { id: "profile-1", code: "HMRI7T", version: 1 },
        score: 10, reasons: ["scanner match"] }],
    }] });
    mocks.listRuns.mockResolvedValue([]);
    mocks.getRecipe.mockResolvedValue({
      summary: { will_run: 1, pending: 0, blocked: 0, tandem: false, unharmonized: 1 },
      recipe: { confirmed_at: "2026-07-12T12:00:00Z", spec: [{
        detector_label: "MAP", status: "created", note: "",
        inputs: [{ study_uid: "1.2.3", role: "t1_mprage", series_uid: "1.2.3.1" }],
        harmonization: { mode: "unharmonized" },
      }] },
    });
  });

  it("shows the current exact plan but no mutation affordances to universal readers", async () => {
    renderPage();
    expect(await screen.findByText(/Read-only case access/)).toBeInTheDocument();
    expect(screen.getByText(/Study Instance UID: 1\.2\.3/)).toBeInTheDocument();
    expect(screen.getByText(/Best available: HMRI7T v1/)).toBeInTheDocument();
    expect(screen.getByText(/are in this plan/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sync from Orthanc" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm series" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm profile" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Build processing plan" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirm plan/ })).not.toBeInTheDocument();
  });

  it("shows processing controls when the server grants the case capability", async () => {
    mocks.getCase.mockResolvedValueOnce({
      id: "case-1", pseudonym: "HMRI-001", status: "series_confirmed",
      orthanc_study_uid: "1.2.3", permissions: { can_mutate: true },
    });
    renderPage();
    expect(await screen.findByRole("button", { name: "Sync from Orthanc" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm series" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm profile" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build processing plan" })).toBeInTheDocument();
  });

  it("can confirm an unconfirmed persisted plan after a reload", async () => {
    mocks.getCase.mockResolvedValueOnce({
      id: "case-1", pseudonym: "HMRI-001", status: "recipe_built",
      orthanc_study_uid: "1.2.3", permissions: { can_mutate: true },
    });
    mocks.getRecipe.mockResolvedValueOnce({
      summary: { will_run: 1, pending: 0, blocked: 0, tandem: false, unharmonized: 1 },
      recipe: { confirmed_at: null, spec: [{
        detector_label: "MAP", status: "created", note: "",
        inputs: [{ role: "t1_mprage", series_uid: "1.2.3.1" }],
        harmonization: { mode: "unharmonized" },
      }] },
    });
    mocks.confirmRecipe.mockResolvedValueOnce({});
    renderPage();
    const confirm = await screen.findByRole("button", { name: /Confirm plan/ });
    fireEvent.click(confirm);
    await waitFor(() => expect(mocks.confirmRecipe).toHaveBeenCalledWith("case-1"));
  });
});
