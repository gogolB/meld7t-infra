import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listHarmonizationCohorts: vi.fn(),
  createHarmonizationCohort: vi.fn(),
}));

vi.mock("../api.js", () => ({
  api: mocks,
  SERIES_ROLES: ["t1_uni", "t1_inv1", "t1_inv2", "t1_mprage", "flair", "t2", "unknown"],
}));

import HarmonizationCohorts from "./HarmonizationCohorts.jsx";

function renderPage() {
  return render(<MemoryRouter initialEntries={["/admin"]}>
    <Routes>
      <Route path="/admin" element={<HarmonizationCohorts />} />
      <Route path="/admin/harmonization/cohorts/:id" element={<div>Cohort detail route</div>} />
    </Routes>
  </MemoryRouter>);
}

describe("HarmonizationCohorts", () => {
  beforeEach(() => {
    mocks.listHarmonizationCohorts.mockResolvedValue([]);
    mocks.createHarmonizationCohort.mockResolvedValue({ id: "cohort-1" });
  });

  it("creates a scanner/protocol-specific cohort contract", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("No harmonization cohorts have been created.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New cohort" }));
    await user.type(screen.getByLabelText("Cohort name *"), "Site A MP2RAGE controls");
    await user.type(screen.getByLabelText("Site code *"), "SITE_A");
    await user.type(screen.getByLabelText("Profile code *"), "H_SITE_A_MP2RAGE");
    await user.type(screen.getByLabelText("Scanner manufacturer *"), "Siemens");
    await user.type(screen.getByLabelText("Scanner model *"), "Terra");
    await user.type(screen.getByLabelText("Scanner station name *"), "MRI7T-A");
    await user.type(screen.getByLabelText("Software version(s) *"), "XA60");
    await user.type(screen.getByLabelText("Protocol name *"), "Research MP2RAGE");
    await user.selectOptions(screen.getByLabelText("Source role *"), "t1_uni");
    await user.click(screen.getByRole("button", { name: "Create cohort" }));

    await waitFor(() => expect(mocks.createHarmonizationCohort).toHaveBeenCalledWith({
      name: "Site A MP2RAGE controls",
      site_code: "SITE_A",
      profile_code: "H_SITE_A_MP2RAGE",
      profile_version: 1,
      source_role: "t1_uni",
      selector: {
        roles: ["t1_uni"],
        acquisition: {
          manufacturer: "Siemens",
          model: "Terra",
          station_name: "MRI7T-A",
          protocol_name: "Research MP2RAGE",
          field_strength_t: 7,
          software_versions: { eq: ["XA60"] },
        },
      },
      min_controls: 20,
      cv_folds: 5,
    }));
    expect(await screen.findByText("Cohort detail route")).toBeInTheDocument();
  });

  it("shows cohort eligibility and lifecycle state", async () => {
    mocks.listHarmonizationCohorts.mockResolvedValue({ cohorts: [{
      id: "cohort-2", name: "Protocol B", site_code: "SITE_B", profile_code: "H_B",
      profile_version: 2, status: "cohort_ready", counts: { studies: 24, included: 22 },
    }] });
    renderPage();
    expect(await screen.findByRole("link", { name: "Protocol B" })).toHaveAttribute(
      "href", "/admin/harmonization/cohorts/cohort-2");
    expect(screen.getByText("22 / 24 eligible")).toBeInTheDocument();
    expect(screen.getByText("cohort ready")).toBeInTheDocument();
  });
});
